"""Real test calls from the Studio — ``originate`` over the FreeSWITCH event socket.

The Testing tab's "Real call" option dials a tester's own mobile number
through the dialer's FreeSWITCH and lets the bot answer it exactly like a
campaign call. The browser never touches the event socket: the backend
builds the command (shared.esl_originate), runs it as a background job
(voice_runtime.freeswitch.ESLClient.originate) and records the outcome in
Redis for the Studio to poll.

Route family (all under /bots/{bot_id}/testing/real-call):
- ``GET  …/defaults``        what the dialog pre-fills (mandatory ESL
                             parameters, customer data from the bot's
                             runtime-context test JSON, blockers)
- ``POST …/preview``         the effective channel variables + command for
                             the tester's inputs — nothing is dialed
- ``POST …``                 place the call (202; poll the job)
- ``GET  …/{job_id}``        job status + the conversation the answered
                             call produced, when the platform has it
- ``POST …/{job_id}/hangup`` end the test call (``uuid_kill``)

Gates: the same ones the telephony webhook applies when the answered leg
comes back (bot published, not archived, voice channel not disabled), so a
real person's phone never rings for a call the platform would then refuse.
ESL credentials stay in settings; responses carry the command without them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

import voice_runtime.freeswitch as fs
from backend.core.audit import record_audit
from backend.core.bot_lifecycle import is_archived
from backend.core.deps import assert_tenant_access, get_current_user, require_permission
from backend.core.responses import ok
from shared.config import get_settings
from shared.db.mysql import get_db
from shared.db.redis import get_redis
from shared.errors import ApiError, NotFoundError
from shared.esl_originate import (
    BOT_ID_PARAM,
    MANDATORY_ESL_PARAMS,
    NUMBER_PARAM,
    ORIGINATION_UUID_PARAM,
    SYSTEM_PARAMS,
    TENANT_PARAM,
    OriginatePlan,
    OriginateValidationError,
    build_originate_plan,
    customer_variable_name,
)
from shared.ids import new_id
from shared.models import (
    ChannelConfig,
    ConversationSession,
    RuntimeContextSchema,
    User,
    VoiceBot,
)
from shared.providers.base import ProviderError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Testing"])

JOB_TTL_SECONDS = 3600
_JOB_KEY = "real_call:{job_id}"
_MAX_WAIT_CAP_SECONDS = 180.0
_STATUS_PROBE_TIMEOUT_SECONDS = 2.0
_background_jobs: set[asyncio.Task] = set()

# Built-in values that mirror the demo dialer's voicebot originate
# (qabackend providerAdaptor.dialPhoneForVoicebot) where the value is not a
# deployment address; those come from FREESWITCH_ORIGINATE_DEFAULTS. Values
# the dialplan never reads (callMode, leadtype, campaignId, basketId) name
# the Studio so the call is recognisable as a test. Recording is off for
# tests: the dialplan script writes the WAV on the FreeSWITCH host.
_BUILTIN_DEFAULTS: dict[str, str] = {
    "ignore_early_media": "true",
    "recording": "false",
    "recordDualStereo": "false",
    "amdEnabled": "false",
    "dialTimeout": "45",
    "callType": "voiceBot",
    "callMode": "studio_test",
    "leadtype": "studio_test",
    "serverType": "qa",
    "campaignId": "echosphere-studio",
    "basketId": "echosphere-studio",
    "operatorId": "",
    "operatorPrefix": "+91",
    "domain_name": "kam-freeswitch.edas.tech",
}


# ── helpers ──────────────────────────────────────────────────────────────────


def _bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


def _voice_channel(db: Session, bot: VoiceBot) -> ChannelConfig | None:
    return db.scalar(
        select(ChannelConfig).where(
            ChannelConfig.bot_id == bot.id,
            ChannelConfig.type == "voice",
            ChannelConfig.is_deleted.is_(False),
        )
    )


def _blockers(db: Session, bot: VoiceBot, channel: ChannelConfig | None) -> list[str]:
    """Why a call must not be placed right now (mirrors the webhook gates)."""
    out: list[str] = []
    if not fs.esl_configured():
        # Deployment detail stays in the docs/logs; the Studio user only
        # needs to know whom to ask.
        out.append(
            "Real calls are not configured on this server (FreeSWITCH event "
            "socket). Ask your platform administrator."
        )
    if is_archived(bot):
        out.append("This bot is archived. Restore it to place test calls.")
    elif bot.status != "published":
        out.append(
            "The bot is not published: the dialer's webhook would reject the "
            "answered call. Publish a release first."
        )
    if channel is not None and not channel.enabled:
        out.append("The voice channel is deactivated: the answered call would be refused.")
    return out


def _configured_defaults(settings) -> tuple[dict[str, str], list[str]]:
    raw = (settings.freeswitch_originate_defaults or "").strip()
    if not raw:
        return {}, []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}, ["FREESWITCH_ORIGINATE_DEFAULTS is not valid JSON; configured defaults ignored."]
    if not isinstance(parsed, dict):
        return {}, ["FREESWITCH_ORIGINATE_DEFAULTS must be a JSON object; ignored."]
    return {str(k): ("" if v is None else str(v)) for k, v in parsed.items()}, []


def _new_lead_id() -> str:
    return f"test-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4]}"


def _base_params(
    settings, bot: VoiceBot, channel: ChannelConfig | None
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Effective defaults for the ESL parameters and where each came from.

    Precedence: built-in test semantics < configured defaults (deployment
    addresses) < bot-derived values (its DID as caller id, its tenant).
    """
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for name, value in _BUILTIN_DEFAULTS.items():
        values[name] = value
        sources[name] = "default"
    configured, warnings = _configured_defaults(settings)
    for name, value in configured.items():
        values[name] = value
        sources[name] = "config"
    for name in ("kamailioHost", "freeswitchHost"):
        if not values.get(name):
            values[name] = settings.freeswitch_host
            sources[name] = "default"
    did = ""
    if channel is not None and isinstance(channel.config, dict):
        did = str(channel.config.get("phoneNumber") or "").strip()
    if did:
        for name in ("origination_caller_id_number", "cli"):
            values[name] = did
            sources[name] = "bot"
    values["leadId"] = _new_lead_id()
    sources["leadId"] = "generated"
    values[TENANT_PARAM] = bot.tenant_id
    sources[TENANT_PARAM] = "bot"
    # Filled from the dialed number at preview/start time.
    values[NUMBER_PARAM] = ""
    sources[NUMBER_PARAM] = "bot"
    for name in MANDATORY_ESL_PARAMS:
        if name not in values:
            values[name] = ""
            sources[name] = "missing"
    return values, sources, warnings


def _customer_defaults(db: Session, bot: VoiceBot) -> tuple[dict, dict]:
    """The bot's runtime-context test JSON (the APIs tab's saved customer
    data) plus the declared fields, for the dialog's customer-data editor."""
    schema = db.scalar(
        select(RuntimeContextSchema).where(
            RuntimeContextSchema.bot_id == bot.id,
            RuntimeContextSchema.is_deleted.is_(False),
        )
    )
    if schema is None:
        return {}, {"configured": False, "sourceMode": "manual", "fields": []}
    payload = schema.test_payload if isinstance(schema.test_payload, dict) else {}
    fields = [
        {"key": f.get("key"), "label": f.get("label") or f.get("key"), "type": f.get("type", "string")}
        for f in (schema.fields or []) if isinstance(f, dict) and f.get("key")
    ]
    return payload, {
        "configured": True,
        "sourceMode": schema.source_mode,
        "fields": fields,
        "allowAdditional": bool(schema.allow_additional),
    }


def _plan_from_request(
    db: Session, settings, bot: VoiceBot, channel: ChannelConfig | None, body: "RealCallRequest"
) -> tuple[OriginatePlan, list[str], list[str]]:
    base, _sources, warnings = _base_params(settings, bot, channel)
    customer_data = body.customer_data
    if customer_data is None:
        customer_data, _schema = _customer_defaults(db, bot)
    overrides: dict[str, object] = {}
    notes: list[str] = []
    for name, value in (body.params or {}).items():
        if name in (NUMBER_PARAM, TENANT_PARAM, BOT_ID_PARAM, ORIGINATION_UUID_PARAM):
            notes.append(f"{name} is set by the platform and was not overridden.")
            continue
        overrides[name] = "" if value is None else value
    try:
        plan = build_originate_plan(
            phone_number=body.phone_number,
            bot_id=bot.id,
            tenant_id=bot.tenant_id,
            base_params=base,
            param_overrides=overrides,
            customer_data=customer_data,
            dial_template=body.dial_template or settings.freeswitch_originate_dial_template,
            application=body.application or settings.freeswitch_originate_application,
            origination_uuid=body.origination_uuid,
        )
    except OriginateValidationError as exc:
        raise ApiError("Some values cannot be sent to FreeSWITCH.", 422, errors=exc.errors)
    for item in plan.skipped_customer_fields:
        reason = item["reason"]
        if reason == "system":
            notes.append(
                f"Customer field '{item['key']}' names a system parameter; "
                "edit it in the ESL parameters instead."
            )
        elif reason == "nested":
            notes.append(f"Customer field '{item['key']}' is nested JSON and was skipped.")
        elif reason == "null":
            notes.append(f"Customer field '{item['key']}' is null and was skipped.")
        else:
            notes.append(f"Customer field '{item['key']}' was skipped ({reason}).")
    return plan, warnings, notes


def _serialize_plan(plan: OriginatePlan) -> dict:
    return {
        "phoneNumber": plan.phone_number,
        "phoneMasked": plan.phone_masked,
        "originationUuid": plan.origination_uuid,
        "variables": plan.variables,
        "customerVariables": plan.customer_variables,
        "skippedCustomerFields": plan.skipped_customer_fields,
        "missingMandatory": plan.missing_mandatory,
        "dialString": plan.dial_string,
        "application": plan.application,
        "command": plan.command,
    }


# ── job store (Redis, one hour) ──────────────────────────────────────────────


async def _save_job(job: dict) -> None:
    job["updatedAt"] = datetime.now(timezone.utc).isoformat()
    await get_redis().set(
        _JOB_KEY.format(job_id=job["jobId"]), json.dumps(job), ex=JOB_TTL_SECONDS
    )


async def _load_job(job_id: str) -> dict | None:
    if not job_id or len(job_id) > 64 or not job_id.replace("-", "").isalnum():
        return None
    raw = await get_redis().get(_JOB_KEY.format(job_id=job_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _run_originate(job: dict, plan: OriginatePlan, wait_seconds: float) -> None:
    client = fs.ESLClient(timeout=10.0)
    try:
        result = await client.originate(
            plan.command, job_uuid=job["jobId"], timeout=wait_seconds
        )
        if result is None:
            job["status"] = "timeout"
            job["detail"] = (
                f"No answer within {int(wait_seconds)} s; FreeSWITCH keeps dialing "
                "until dialTimeout."
            )
        else:
            answered, detail = result
            if answered:
                job["status"] = "answered"
                job["channelUuid"] = detail or plan.origination_uuid
                job["detail"] = "Call answered; the dialplan is handing it to the bot."
            else:
                job["status"] = "failed"
                job["cause"] = detail
                job["detail"] = f"FreeSWITCH reported {detail or 'an error'}."
    except ProviderError as exc:
        job["status"] = "error"
        job["cause"] = exc.category
        job["detail"] = f"FreeSWITCH event socket: {exc}"
    except Exception as exc:  # noqa: BLE001 — a job never dies silently
        logger.exception("real call job %s crashed", job["jobId"])
        job["status"] = "error"
        job["detail"] = f"Unexpected error: {type(exc).__name__}"
    finally:
        await client.close()
    try:
        await _save_job(job)
    except Exception:  # noqa: BLE001
        logger.exception("real call job %s: could not store result", job["jobId"])


def _conversation_for(db: Session, bot: VoiceBot, call_uuid: str) -> dict | None:
    row = db.scalar(
        select(ConversationSession)
        .where(
            ConversationSession.tenant_id == bot.tenant_id,
            ConversationSession.call_id == call_uuid,
            ConversationSession.is_deleted.is_(False),
        )
        .order_by(ConversationSession.created_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {
        "id": row.id,
        "sessionId": row.session_id,
        "botId": row.bot_id,
        "status": row.status,
        "startedAt": row.started_at.isoformat() + "Z" if row.started_at else None,
        "durationSec": row.duration_sec,
        "disposition": row.disposition,
        "language": row.language,
    }


async def _refresh_answered_status(job: dict) -> None:
    """Originate success means answered, not that the call is still live.

    Prefer a finalized conversation; otherwise inspect the originated channel
    without altering it. Only an explicit missing-channel reply proves hangup:
    an unavailable ESL socket must never be interpreted as a disconnected call.
    This reconciliation is response-only so a poll cannot overwrite a concurrent
    Studio hangup/originate result in Redis.
    """
    if job.get("status") != "answered":
        return
    conversation = job.get("conversation") or {}
    ended = conversation.get("status") in {"completed", "ended", "failed", "cancelled"}
    if not ended:
        call_uuid = job.get("channelUuid") or job.get("originationUuid") or ""
        if not fs.valid_call_uuid(call_uuid) or not fs.esl_configured():
            return
        client = None
        try:
            client = fs.ESLClient(timeout=_STATUS_PROBE_TIMEOUT_SECONDS)
            reply = await asyncio.wait_for(
                client.api(f"uuid_exists {call_uuid}"),
                timeout=_STATUS_PROBE_TIMEOUT_SECONDS,
            )
            ended = reply.strip().lower() == "false"
        except (ProviderError, OSError, TimeoutError):
            logger.warning("Could not refresh real call status job=%s", job["jobId"])
        finally:
            if client is not None:
                await client.close()
    if ended:
        job["status"] = "ended"
        job["detail"] = "The call has ended."


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/bots/{bot_id}/testing/real-call/defaults")
def real_call_defaults(
    bot_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    bot = _bot_checked(db, bot_id, user)
    channel = _voice_channel(db, bot)
    values, sources, warnings = _base_params(settings, bot, channel)
    payload, schema = _customer_defaults(db, bot)
    params = [
        {
            "name": name,
            "value": values.get(name, ""),
            "mandatory": name in MANDATORY_ESL_PARAMS,
            "platform": name in (NUMBER_PARAM, TENANT_PARAM),
            "source": sources.get(name, "missing"),
        }
        for name in MANDATORY_ESL_PARAMS
    ]
    # Configured defaults beyond the mandatory list ride along, editable.
    for name, value in values.items():
        if name not in MANDATORY_ESL_PARAMS and name not in SYSTEM_PARAMS:
            params.append({
                "name": name, "value": value, "mandatory": False,
                "platform": False, "source": sources.get(name, "config"),
            })
    customer = [
        {
            "key": key,
            "variable": customer_variable_name(key),
            "value": value,
            "scalar": isinstance(value, (str, int, float, bool)),
            "source": "test_payload",
        }
        for key, value in payload.items()
    ]
    return ok({
        "botId": bot.id,
        "botName": bot.name,
        "tenantId": bot.tenant_id,
        "botStatus": bot.status,
        "eslConfigured": fs.esl_configured(),
        "eslHost": settings.freeswitch_host,
        "callerIdNumber": values.get("origination_caller_id_number", ""),
        "voiceChannel": None if channel is None else {
            "enabled": bool(channel.enabled),
            "phoneNumber": (channel.config or {}).get("phoneNumber"),
            "telephonyProvider": (channel.config or {}).get("telephonyProvider"),
        },
        "dialTemplate": settings.freeswitch_originate_dial_template,
        "application": settings.freeswitch_originate_application,
        "systemParams": [BOT_ID_PARAM, ORIGINATION_UUID_PARAM, NUMBER_PARAM, TENANT_PARAM],
        "params": params,
        "customerData": customer,
        "runtimeContext": schema,
        "warnings": warnings,
        "blockers": _blockers(db, bot, channel),
    })


class RealCallRequest(BaseModel):
    phone_number: str = Field(alias="phoneNumber", min_length=6, max_length=40)
    # ESL channel variables the tester edited; platform-owned names are ignored.
    params: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    # Customer data (key → value) mapped to vb_* channel variables. Omitted
    # (None) = the bot's saved runtime-context test data; {} = none at all.
    customer_data: dict[str, object] | None = Field(default=None, alias="customerData")
    dial_template: str | None = Field(default=None, alias="dialTemplate", max_length=300)
    application: str | None = Field(default=None, max_length=200)
    origination_uuid: str | None = Field(default=None, alias="originationUuid", max_length=64)
    # The Studio sends true only after the tester acknowledged that a real
    # telephone call is placed.
    confirm: bool = False

    model_config = {"populate_by_name": True}

    def bounded(self) -> "RealCallRequest":
        if len(self.params) > 100 or len(self.customer_data or {}) > 100:
            raise ApiError("Too many parameters (max 100 each).", 422)
        return self


@router.post("/bots/{bot_id}/testing/real-call/preview")
def real_call_preview(
    bot_id: str,
    body: RealCallRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The effective channel variables and command — nothing is dialed."""
    settings = get_settings()
    bot = _bot_checked(db, bot_id, user)
    channel = _voice_channel(db, bot)
    plan, warnings, notes = _plan_from_request(db, settings, bot, channel, body.bounded())
    blockers = _blockers(db, bot, channel)
    return ok({
        **_serialize_plan(plan),
        "warnings": warnings,
        "notes": notes,
        "blockers": blockers,
        "canStart": not blockers and not plan.missing_mandatory,
    })


@router.post("/bots/{bot_id}/testing/real-call", status_code=202)
async def start_real_call(
    bot_id: str,
    body: RealCallRequest,
    request: Request,
    user: User = Depends(require_permission("manage_testing", "bots.manage")),
    db: Session = Depends(get_db),
):
    """Place the call. A real telephone rings: the tester must confirm."""
    settings = get_settings()
    bot = _bot_checked(db, bot_id, user)
    channel = _voice_channel(db, bot)
    if not body.confirm:
        raise ApiError("Confirm that a real telephone call will be placed.", 422)
    plan, _warnings, _notes = _plan_from_request(db, settings, bot, channel, body.bounded())
    blockers = _blockers(db, bot, channel)
    if blockers:
        raise ApiError(blockers[0], 409, errors=[{"message": b} for b in blockers])
    if plan.missing_mandatory:
        raise ApiError(
            "Mandatory ESL parameters are empty: " + ", ".join(plan.missing_mandatory),
            422,
            errors=[{"field": n, "message": "Required by the dialer."} for n in plan.missing_mandatory],
        )
    try:
        dial_timeout = float(plan.variables.get("dialTimeout") or 0)
    except ValueError:
        dial_timeout = 0.0
    wait_seconds = min(
        max(settings.freeswitch_originate_max_wait_seconds, dial_timeout + 15.0),
        _MAX_WAIT_CAP_SECONDS,
    )
    job = {
        "jobId": str(uuid.uuid4()),
        "botId": bot.id,
        "tenantId": bot.tenant_id,
        "userId": user.id,
        "phoneMasked": plan.phone_masked,
        "originationUuid": plan.origination_uuid,
        "leadId": plan.variables.get("leadId", ""),
        "status": "dialing",
        "detail": "Originate sent to FreeSWITCH; waiting for the phone to answer.",
        "cause": None,
        "channelUuid": None,
        "variableNames": sorted(plan.variables),
        "customerVariables": plan.customer_variables,
        "dialString": plan.dial_string,
        "waitSeconds": wait_seconds,
        "startedAt": datetime.now(timezone.utc).isoformat(),
    }
    record_audit(
        db, user=user, action="Started real test call", entity_type="voice_bot",
        entity_id=bot.id, target_label=f"{bot.name} → {plan.phone_masked}",
        tenant_id=bot.tenant_id, request=request,
        new_value={
            "jobId": job["jobId"], "originationUuid": plan.origination_uuid,
            "phoneMasked": plan.phone_masked, "leadId": job["leadId"],
            "variables": job["variableNames"],
        },
    )
    db.commit()
    await _save_job(job)
    task = asyncio.create_task(_run_originate(dict(job), plan, wait_seconds))
    _background_jobs.add(task)
    task.add_done_callback(_background_jobs.discard)
    logger.info(
        "real call started bot=%s tenant=%s job=%s uuid=%s number=%s by=%s",
        bot.id, bot.tenant_id, job["jobId"], plan.origination_uuid, plan.phone_masked, user.id,
    )
    return ok(job)


async def _job_checked(db: Session, bot: VoiceBot, job_id: str) -> dict:
    job = await _load_job(job_id)
    if job is None or job.get("botId") != bot.id or job.get("tenantId") != bot.tenant_id:
        raise NotFoundError("Real call")
    return job


@router.get("/bots/{bot_id}/testing/real-call/{job_id}")
async def real_call_status(
    bot_id: str,
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    job = await _job_checked(db, bot, job_id)
    job["conversation"] = _conversation_for(db, bot, job.get("originationUuid") or "")
    await _refresh_answered_status(job)
    return ok(job)


@router.post("/bots/{bot_id}/testing/real-call/{job_id}/hangup")
async def real_call_hangup(
    bot_id: str,
    job_id: str,
    request: Request,
    user: User = Depends(require_permission("manage_testing", "bots.manage")),
    db: Session = Depends(get_db),
):
    """End the test call from the Studio (``uuid_kill`` on the originated leg)."""
    bot = _bot_checked(db, bot_id, user)
    job = await _job_checked(db, bot, job_id)
    call_uuid = job.get("channelUuid") or job.get("originationUuid") or ""
    if not fs.valid_call_uuid(call_uuid):
        raise ApiError("This call has no channel to hang up.", 409)
    if not fs.esl_configured():
        raise ApiError("FreeSWITCH event socket is not configured.", 409)
    client = fs.ESLClient(timeout=10.0)
    try:
        result = await client.api(f"uuid_kill {call_uuid} NORMAL_CLEARING")
    except ProviderError as exc:
        raise ApiError(f"FreeSWITCH event socket: {exc}", 502)
    finally:
        await client.close()
    if "+OK" in result:
        job["status"] = "hungup"
        job["detail"] = "Ended from the Studio."
    elif "No such channel" in result:
        job["detail"] = "The call had already ended."
        if job.get("status") in ("dialing", "answered"):
            job["status"] = "ended"
    else:
        raise ApiError(f"Hangup failed: {result[:120]}", 502)
    await _save_job(job)
    record_audit(
        db, user=user, action="Ended real test call", entity_type="voice_bot",
        entity_id=bot.id, target_label=f"{bot.name} → {job.get('phoneMasked', '')}",
        tenant_id=bot.tenant_id, request=request,
        new_value={"jobId": job["jobId"], "result": result[:120]},
    )
    db.commit()
    job["conversation"] = _conversation_for(db, bot, job.get("originationUuid") or "")
    return ok(job)
