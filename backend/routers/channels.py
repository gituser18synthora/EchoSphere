"""Channel management: per-bot deployment channels (voice, WhatsApp, web,
mobile, SMS) with provider configuration, real connection tests, traffic
gating and webhook endpoints.

Security model:
- Mutations require the `manage_channels` permission; reads any tenant member.
- Provider secrets are NEVER stored or returned: secret fields accept only
  `env:VAR_NAME` references (validated), resolved at use time.
- Status is server-derived (save → configured, test → live/failed); clients
  cannot claim a channel is live.
- The `enabled` flag gates traffic: telephony/WhatsApp webhooks reject
  disabled channels with sanitized errors.
"""

import asyncio
import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    assert_tenant_access,
    get_current_user,
    require_permission,
    require_super_admin,
)
from backend.core.responses import ok
from backend.core.softdelete import soft_delete
from backend.serializers import serialize_channel
from shared.config import get_settings
from shared.db.mysql import get_db
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from shared.models import (
    ChannelConfig,
    KnowledgeSource,
    PhoneNumber,
    Prompt,
    User,
    VoiceBot,
    VoiceBotSetting,
)
from shared.readiness import refresh_readiness
from shared.telephony import SUPPORTED_PROVIDERS

router = APIRouter(tags=["Channels"])

CHANNEL_TYPES = ("voice", "whatsapp", "web", "mobile", "sms")

_REFERENCE_RE = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$")
_SENDER_ID_RE = re.compile(r"^([A-Za-z0-9]{3,15}|\+[1-9]\d{6,14})$")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_BUNDLE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")

_REFERENCE_HINT = (
    "must be an environment reference like env:VAR_NAME — raw secrets are never stored"
)


def _norm_phone(number: str) -> str:
    return re.sub(r"[\s().-]", "", number or "")


def _check_reference(value: str, field: str, *, required: bool) -> None:
    if not value:
        if required:
            raise ApiError(f"{field} is required.", 422,
                           errors=[{"field": field, "message": "Required."}])
        return
    if not _REFERENCE_RE.match(value):
        raise ApiError(f"{field} {_REFERENCE_HINT}.", 422,
                       errors=[{"field": field, "message": _REFERENCE_HINT + "."}])


# ── Provider-specific configuration schemas ───────────────────────────────────


class VoiceChannelConfigModel(BaseModel):
    phone_number: str = Field(alias="phoneNumber", min_length=7, max_length=30)
    telephony_provider: str = Field(alias="telephonyProvider")
    public_ws_base: str = Field(default="", alias="publicWsBase", max_length=300)
    auth_token_reference: str = Field(default="", alias="authTokenReference", max_length=120)
    language: str = Field(default="", max_length=15)
    voice_id: str = Field(default="", alias="voiceId", max_length=40)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("telephony_provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        if v not in SUPPORTED_PROVIDERS:
            raise ValueError(f"must be one of: {', '.join(SUPPORTED_PROVIDERS)}")
        return v

    def check(self) -> None:
        if not _E164_RE.match(_norm_phone(self.phone_number)):
            raise ApiError("phoneNumber must be an E.164 number, e.g. +14155550119.", 422,
                           errors=[{"field": "phoneNumber", "message": "Invalid E.164 number."}])
        if self.telephony_provider in ("twilio", "telnyx", "plivo", "exotel"):
            if not self.public_ws_base:
                raise ApiError(
                    "publicWsBase (wss://…) is required for cloud telephony media streaming.",
                    422, errors=[{"field": "publicWsBase", "message": "Required for this provider."}])
            if not self.public_ws_base.startswith(("ws://", "wss://")):
                raise ApiError("publicWsBase must start with ws:// or wss://.", 422,
                               errors=[{"field": "publicWsBase", "message": "Must be a ws(s):// URL."}])
        _check_reference(self.auth_token_reference, "authTokenReference",
                         required=self.telephony_provider == "twilio")


class WhatsAppChannelConfigModel(BaseModel):
    whatsapp_number: str = Field(alias="whatsappNumber", min_length=7, max_length=30)
    provider: str = Field(default="meta", pattern="^(meta|twilio|pinbot)$")
    phone_number_id: str = Field(default="", alias="phoneNumberId", max_length=60)
    business_account_id: str = Field(default="", alias="businessAccountId", max_length=60)
    api_key_reference: str = Field(default="", alias="apiKeyReference", max_length=120)
    webhook_secret_reference: str = Field(default="", alias="webhookSecretReference", max_length=120)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    def check(self) -> None:
        if not _E164_RE.match(_norm_phone(self.whatsapp_number)):
            raise ApiError("whatsappNumber must be an E.164 number.", 422,
                           errors=[{"field": "whatsappNumber", "message": "Invalid E.164 number."}])
        if self.provider == "meta" and not self.phone_number_id:
            raise ApiError("phoneNumberId is required for the Meta WhatsApp Cloud API.", 422,
                           errors=[{"field": "phoneNumberId", "message": "Required for Meta."}])
        _check_reference(self.api_key_reference, "apiKeyReference", required=True)
        _check_reference(self.webhook_secret_reference, "webhookSecretReference",
                         required=self.provider == "meta")


class WebChannelConfigModel(BaseModel):
    allowed_origins: list[str] = Field(alias="allowedOrigins", min_length=1, max_length=20)
    widget_color: str = Field(default="", alias="widgetColor", max_length=7)
    language: str = Field(default="", max_length=15)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    def check(self) -> None:
        for origin in self.allowed_origins:
            if not _ORIGIN_RE.match(origin.rstrip("/")):
                raise ApiError(
                    f"'{origin}' is not a valid origin (expected https://host[:port]).", 422,
                    errors=[{"field": "allowedOrigins", "message": f"Invalid origin: {origin}"}])
        if self.widget_color and not _HEX_COLOR_RE.match(self.widget_color):
            raise ApiError("widgetColor must be a hex color like #1A73E8.", 422,
                           errors=[{"field": "widgetColor", "message": "Invalid hex color."}])


class MobileChannelConfigModel(BaseModel):
    platform: str = Field(default="both", pattern="^(ios|android|both)$")
    bundle_ids: list[str] = Field(alias="bundleIds", min_length=1, max_length=20)
    api_key_reference: str = Field(default="", alias="apiKeyReference", max_length=120)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    def check(self) -> None:
        for bundle in self.bundle_ids:
            if not _BUNDLE_ID_RE.match(bundle):
                raise ApiError(f"'{bundle}' is not a valid application bundle id.", 422,
                               errors=[{"field": "bundleIds", "message": f"Invalid bundle id: {bundle}"}])
        _check_reference(self.api_key_reference, "apiKeyReference", required=False)


class SmsChannelConfigModel(BaseModel):
    provider: str = Field(pattern="^(twilio|plivo|telnyx|exotel)$")
    sender_id: str = Field(alias="senderId", min_length=3, max_length=20)
    account_id: str = Field(default="", alias="accountId", max_length=60)
    api_key_reference: str = Field(default="", alias="apiKeyReference", max_length=120)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    def check(self) -> None:
        if not _SENDER_ID_RE.match(_norm_phone(self.sender_id) if self.sender_id.startswith("+")
                                    else self.sender_id):
            raise ApiError(
                "senderId must be a 3-15 char alphanumeric id or an E.164 number.", 422,
                errors=[{"field": "senderId", "message": "Invalid sender id."}])
        if self.provider == "twilio" and not self.account_id:
            raise ApiError("accountId (Account SID) is required for Twilio.", 422,
                           errors=[{"field": "accountId", "message": "Required for Twilio."}])
        _check_reference(self.api_key_reference, "apiKeyReference", required=True)


_CONFIG_MODELS = {
    "voice": VoiceChannelConfigModel,
    "whatsapp": WhatsAppChannelConfigModel,
    "web": WebChannelConfigModel,
    "mobile": MobileChannelConfigModel,
    "sms": SmsChannelConfigModel,
}

# Config keys that hold secret references (audited as credential updates).
_CREDENTIAL_KEYS = {"authTokenReference", "apiKeyReference", "webhookSecretReference"}


def _validate_config(channel_type: str, raw: dict) -> dict:
    """Validate + normalize a provider config; returns the canonical dict."""
    model_cls = _CONFIG_MODELS[channel_type]
    try:
        model = model_cls(**raw)
    except ApiError:
        raise
    except Exception as exc:  # pydantic ValidationError → clean 422
        errors = []
        for err in getattr(exc, "errors", lambda: [])():
            field = ".".join(str(p) for p in err.get("loc", []))
            errors.append({"field": field, "message": err.get("msg", "Invalid value.")})
        raise ApiError("Channel configuration is invalid.", 422, errors=errors or None)
    model.check()
    return model.model_dump(by_alias=True)


def _derive_detail(channel_type: str, config: dict) -> str:
    if channel_type == "voice":
        return f"{config['phoneNumber']} · {config['telephonyProvider']}"
    if channel_type == "whatsapp":
        return f"{config['whatsappNumber']} · {config['provider']}"
    if channel_type == "web":
        origins = config["allowedOrigins"]
        return origins[0] + (f" +{len(origins) - 1}" if len(origins) > 1 else "")
    if channel_type == "mobile":
        return f"{config['platform']} · {config['bundleIds'][0]}"
    if channel_type == "sms":
        return f"{config['senderId']} · {config['provider']}"
    return ""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


def _get_channel(db: Session, bot: VoiceBot, channel_type: str) -> ChannelConfig | None:
    if channel_type not in CHANNEL_TYPES:
        raise NotFoundError("Channel type")
    return db.scalar(
        select(ChannelConfig).where(
            ChannelConfig.bot_id == bot.id, ChannelConfig.type == channel_type,
            ChannelConfig.is_deleted.is_(False),
        )
    )


def _get_archived_channel(db: Session, bot: VoiceBot, channel_type: str) -> ChannelConfig | None:
    """The archived row for this (bot, type), if any.

    Archiving is a soft delete, so the row keeps occupying the
    `uq_channel_bot_type` unique key. Re-configuring the channel must revive
    that row — inserting a second one raises a duplicate-key IntegrityError,
    which the global handler turns into an opaque 409.
    """
    return db.scalar(
        select(ChannelConfig).where(
            ChannelConfig.bot_id == bot.id, ChannelConfig.type == channel_type,
            ChannelConfig.is_deleted.is_(True),
        )
    )


def _binding(db: Session, bot: VoiceBot) -> dict:
    """What this channel routes to: tenant, bot, published config, prompt,
    knowledge access and language/voice settings."""
    vbs = db.scalar(select(VoiceBotSetting).where(VoiceBotSetting.bot_id == bot.id))
    prompt_published = db.scalar(
        select(func.count()).select_from(Prompt).where(
            Prompt.bot_id == bot.id, Prompt.type == "system",
            Prompt.published_version.isnot(None), Prompt.is_deleted.is_(False),
        )
    )
    kb_count = db.scalar(
        select(func.count()).select_from(KnowledgeSource).where(
            KnowledgeSource.is_deleted.is_(False),
            KnowledgeSource.status.in_(("indexed", "stale")),
            (
                (KnowledgeSource.bot_id == bot.id)
                | ((KnowledgeSource.tenant_id == bot.tenant_id)
                   & (KnowledgeSource.scope == "tenant"))
                | (KnowledgeSource.scope == "global")
            ),
        )
    )
    settings = get_settings()
    return {
        "tenantId": bot.tenant_id,
        "botId": bot.id,
        "botName": bot.name,
        "botStatus": bot.status,
        "publishedVersion": bot.live_version,
        "systemPromptPublished": bool(prompt_published),
        "knowledgeBases": int(kb_count or 0),
        "language": ((vbs.language_voice_map or {}).get("default") if vbs else None) or "en",
        "voiceId": vbs.voice_id if vbs else None,
        "sttProvider": (vbs.stt_provider if vbs and vbs.stt_provider else settings.stt_provider),
        "ttsProvider": (vbs.tts_provider if vbs and vbs.tts_provider else settings.tts_provider),
        "llmProvider": (vbs.llm_provider if vbs and vbs.llm_provider else settings.llm_provider),
    }


def _release_phone_number(db: Session, bot: VoiceBot, config: dict | None) -> None:
    """Unassign the channel's phone number if it is assigned to this bot."""
    number = (config or {}).get("phoneNumber")
    if not number:
        return
    row = _find_phone_number(db, number)
    if row is not None and row.bot_id == bot.id:
        row.bot_id = None
        row.status = "available"


def _find_phone_number(db: Session, number: str) -> PhoneNumber | None:
    row = db.scalar(select(PhoneNumber).where(PhoneNumber.number == number))
    if row is not None:
        return row
    normalized = _norm_phone(number)
    for candidate in db.scalars(select(PhoneNumber).where(PhoneNumber.is_deleted.is_(False))):
        if _norm_phone(candidate.number) == normalized:
            return candidate
    return None


def _assign_phone_number(db: Session, bot: VoiceBot, config: dict, user: User) -> None:
    """Claim the voice channel's number for this bot (trusted inbound routing).

    The phone_numbers table is what the telephony webhook trusts to map a
    dialed number to a tenant/bot, so a number can only be claimed if it is
    unassigned or already belongs to this tenant and is not serving another
    bot."""
    number = config["phoneNumber"]
    row = _find_phone_number(db, number)
    if row is None:
        db.add(PhoneNumber(
            id=new_id("pn"), number=number, tenant_id=bot.tenant_id, bot_id=bot.id,
            provider=config.get("telephonyProvider"), status="assigned",
            created_by=user.id,
        ))
        return
    if row.bot_id and row.bot_id != bot.id:
        # Sanitized: do not reveal which tenant/bot holds the number.
        raise ApiError("This phone number is already assigned to another channel.", 409,
                       errors=[{"field": "phoneNumber", "message": "Number already in use."}])
    if row.tenant_id and row.tenant_id != bot.tenant_id:
        raise ApiError("This phone number is already assigned to another channel.", 409,
                       errors=[{"field": "phoneNumber", "message": "Number already in use."}])
    if not row.is_active and row.bot_id != bot.id:
        # New claims on a deactivated number are rejected; a bot re-saving the
        # channel that already holds the number keeps working (deactivation
        # must never break an existing deployment).
        raise ApiError("This phone number is deactivated and cannot take new "
                       "assignments. Ask a platform admin to activate it.", 409,
                       errors=[{"field": "phoneNumber", "message": "Number is inactive."}])
    row.tenant_id = bot.tenant_id
    row.bot_id = bot.id
    row.status = "assigned"
    row.provider = config.get("telephonyProvider") or row.provider
    row.updated_by = user.id
    if row.is_deleted:
        # Claiming a soft-deleted row must undelete it: inbound routing resolves
        # numbers with is_deleted = false, so leaving the flag set would save a
        # channel that passes its test but drops every real call.
        row.is_deleted = False
        row.deleted_at = None
        row.deleted_by = None


def _refresh_readiness(db: Session, bot: VoiceBot) -> None:
    refresh_readiness(db, bot, keys=("r6",))


# ── Read endpoints ────────────────────────────────────────────────────────────


@router.get("/bots/{bot_id}/channels")
def list_bot_channels(
    bot_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    bot = _bot_checked(db, bot_id, user)
    rows = db.scalars(
        select(ChannelConfig).where(
            ChannelConfig.bot_id == bot.id, ChannelConfig.is_deleted.is_(False)
        )
    ).all()
    by_type = {c.type: c for c in rows}
    binding = _binding(db, bot)
    out = []
    for ctype in CHANNEL_TYPES:
        if ctype in by_type:
            out.append(serialize_channel(by_type[ctype], binding=binding))
        else:
            out.append({
                "id": None, "type": ctype, "botId": bot.id, "status": "not_configured",
                "enabled": False, "detail": "", "workflow": "—", "lastTest": None,
                "config": None, "updatedAt": None, "binding": binding,
            })
    return ok(out)


@router.get("/bots/{bot_id}/channels/{channel_type}")
def get_bot_channel(
    bot_id: str, channel_type: str,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    row = _get_channel(db, bot, channel_type)
    if row is None:
        raise NotFoundError("Channel")
    return ok(serialize_channel(row, binding=_binding(db, bot)))


# ── Create / update ───────────────────────────────────────────────────────────


class ChannelUpsertRequest(BaseModel):
    config: dict = Field(default_factory=dict)
    workflow_name: str | None = Field(default=None, alias="workflowName", max_length=200)

    model_config = {"populate_by_name": True}


@router.put("/bots/{bot_id}/channels/{channel_type}")
def upsert_channel(
    bot_id: str,
    channel_type: str,
    body: ChannelUpsertRequest,
    request: Request,
    user: User = Depends(require_permission("manage_channels")),
    db: Session = Depends(get_db),
):
    """Create or update a channel's provider configuration. Status is
    server-derived: a saved channel is `configured` until a connection test
    promotes it to `live` (or demotes it to `failed`)."""
    bot = _bot_checked(db, bot_id, user)
    if channel_type not in CHANNEL_TYPES:
        raise NotFoundError("Channel type")
    config = _validate_config(channel_type, body.config or {})

    row = _get_channel(db, bot, channel_type)
    created = row is None
    # An archived channel still holds the (bot, type) unique key, so a re-create
    # revives it instead of inserting a colliding row.
    archived = _get_archived_channel(db, bot, channel_type) if created else None
    previous_config = dict((row or archived).config or {}) if (row or archived) else {}

    if channel_type == "voice":
        old_number = previous_config.get("phoneNumber")
        if old_number and _norm_phone(old_number) != _norm_phone(config["phoneNumber"]):
            _release_phone_number(db, bot, previous_config)
        _assign_phone_number(db, bot, config, user)

    if created:
        if archived is not None:
            row = archived
            row.is_deleted = False
            row.deleted_at = None
            row.deleted_by = None
            row.enabled = True
        else:
            row = ChannelConfig(
                id=new_id("ch"), tenant_id=bot.tenant_id, bot_id=bot.id,
                type=channel_type, created_by=user.id, enabled=True,
            )
            db.add(row)
    row.config = config
    row.detail = _derive_detail(channel_type, config)
    row.status = "configured"
    if body.workflow_name is not None:
        row.workflow_name = body.workflow_name
    row.updated_by = user.id
    _refresh_readiness(db, bot)

    credentials_changed = any(
        previous_config.get(k) != config.get(k) for k in _CREDENTIAL_KEYS
        if k in config or k in previous_config
    )
    action = ("Created channel" if created
              else "Updated channel credentials" if credentials_changed
              else "Updated channel")
    record_audit(
        db, user=user, action=action, entity_type="channel",
        entity_id=row.id, target_label=f"{bot.name} · {channel_type}",
        tenant_id=bot.tenant_id,
        previous_value={"detail": previous_config and _derive_detail(channel_type, previous_config) or None},
        new_value={"detail": row.detail, "credentialsChanged": credentials_changed},
        request=request,
    )
    db.commit()
    return ok(serialize_channel(row, binding=_binding(db, bot)))


# ── Activate / deactivate / archive ──────────────────────────────────────────


def _load_configured_channel(db: Session, bot: VoiceBot, channel_type: str) -> ChannelConfig:
    row = _get_channel(db, bot, channel_type)
    if row is None:
        raise NotFoundError("Channel")
    return row


@router.post("/bots/{bot_id}/channels/{channel_type}/activate")
def activate_channel(
    bot_id: str, channel_type: str, request: Request,
    user: User = Depends(require_permission("manage_channels")),
    db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    row = _load_configured_channel(db, bot, channel_type)
    if not row.config:
        raise ApiError("Configure the channel before activating it.", 422)
    # Live phone/WhatsApp/SMS traffic pins the published bot configuration.
    if channel_type in ("voice", "whatsapp", "sms") and bot.status != "published":
        raise ApiError(
            "Publish the bot before activating this channel — live calls and "
            "messages always run the published configuration.", 422)
    row.enabled = True
    row.updated_by = user.id
    record_audit(
        db, user=user, action="Activated channel", entity_type="channel",
        entity_id=row.id, target_label=f"{bot.name} · {channel_type}",
        tenant_id=bot.tenant_id, new_value={"enabled": True}, request=request,
    )
    db.commit()
    return ok(serialize_channel(row, binding=_binding(db, bot)))


@router.post("/bots/{bot_id}/channels/{channel_type}/deactivate")
def deactivate_channel(
    bot_id: str, channel_type: str, request: Request,
    user: User = Depends(require_permission("manage_channels")),
    db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    row = _load_configured_channel(db, bot, channel_type)
    row.enabled = False
    if row.status == "live":
        row.status = "configured"
    row.updated_by = user.id
    record_audit(
        db, user=user, action="Deactivated channel", entity_type="channel",
        entity_id=row.id, target_label=f"{bot.name} · {channel_type}",
        tenant_id=bot.tenant_id, new_value={"enabled": False}, request=request,
    )
    db.commit()
    return ok(serialize_channel(row, binding=_binding(db, bot)))


@router.delete("/bots/{bot_id}/channels/{channel_type}")
def archive_channel(
    bot_id: str, channel_type: str, request: Request,
    user: User = Depends(require_permission("manage_channels")),
    db: Session = Depends(get_db),
):
    """Archive (soft-delete) a channel and release its phone number."""
    bot = _bot_checked(db, bot_id, user)
    row = _load_configured_channel(db, bot, channel_type)
    if channel_type == "voice":
        _release_phone_number(db, bot, row.config)
    row.enabled = False
    soft_delete(row, user)
    _refresh_readiness(db, bot)
    record_audit(
        db, user=user, action="Archived channel", entity_type="channel",
        entity_id=row.id, target_label=f"{bot.name} · {channel_type}",
        tenant_id=bot.tenant_id, request=request,
    )
    db.commit()
    return ok({"archived": True})


# ── Connection testing ────────────────────────────────────────────────────────


def _voice_worker_base() -> str:
    settings = get_settings()
    host = settings.voice_worker_host
    if host in ("0.0.0.0", "", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{settings.voice_worker_port}"


async def _check_voice_worker(checks: list[dict]) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{_voice_worker_base()}/health")
        payload = response.json()
        healthy = response.status_code == 200 and payload.get("status") == "up"
        checks.append({"name": "Voice runtime reachable", "ok": healthy,
                       "message": f"{_voice_worker_base()}/health → {response.status_code}"})
        return healthy
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "Voice runtime reachable", "ok": False,
                       "message": f"Voice runtime is not reachable ({exc.__class__.__name__})."})
        return False


def _check_secret_resolves(reference: str, name: str, checks: list[dict],
                           *, required: bool) -> bool:
    if not reference:
        checks.append({"name": name, "ok": not required,
                       "message": "No reference configured."})
        return not required
    value = get_settings().resolve_secret(reference)
    if value:
        checks.append({"name": name, "ok": True, "message": f"{reference} resolves."})
        return True
    checks.append({"name": name, "ok": False,
                   "message": f"{reference} does not resolve — set the environment variable."})
    return False


async def _run_channel_test(db: Session, bot: VoiceBot, row: ChannelConfig) -> dict:
    """Real, provider-aware connectivity checks. No fabricated successes:
    every check either verifies something concrete or fails honestly."""
    checks: list[dict] = []
    config = row.config or {}
    ok_all = True

    if not config:
        return {"ok": False, "message": "Channel has no configuration yet.", "checks": []}

    # Re-validate stored config against the current schema.
    try:
        _validate_config(row.type, config)
        checks.append({"name": "Configuration valid", "ok": True, "message": ""})
    except ApiError as exc:
        checks.append({"name": "Configuration valid", "ok": False, "message": exc.message})
        return {"ok": False, "message": exc.message, "checks": checks}

    if row.type in ("voice", "whatsapp", "sms") and bot.status != "published":
        ok_all = False
        checks.append({"name": "Published release", "ok": False,
                       "message": "Bot has no published release — live traffic would be refused."})
    elif row.type in ("voice", "whatsapp", "sms"):
        checks.append({"name": "Published release", "ok": True,
                       "message": f"v{bot.live_version or bot.version}"})

    if row.type == "voice":
        number_row = _find_phone_number(db, config["phoneNumber"])
        mapped = (number_row is not None and number_row.bot_id == bot.id
                  and number_row.status == "assigned")
        ok_all &= mapped
        checks.append({"name": "Phone number mapping", "ok": mapped,
                       "message": "Number is assigned to this bot in the routing table."
                       if mapped else "Number is not assigned to this bot."})
        ok_all &= await _check_voice_worker(checks)
        provider = config["telephonyProvider"]
        if provider == "freeswitch":
            settings = get_settings()
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(settings.freeswitch_host, settings.freeswitch_port),
                    timeout=4,
                )
                writer.close()
                checks.append({"name": "FreeSWITCH event socket", "ok": True,
                               "message": f"{settings.freeswitch_host}:{settings.freeswitch_port} reachable."})
            except Exception:  # noqa: BLE001
                ok_all = False
                checks.append({"name": "FreeSWITCH event socket", "ok": False,
                               "message": "Event socket is not reachable."})
        elif provider == "twilio":
            ok_all &= _check_secret_resolves(config.get("authTokenReference", ""),
                                             "Twilio auth token", checks, required=True)

    elif row.type == "whatsapp":
        resolvable = _check_secret_resolves(config.get("apiKeyReference", ""),
                                            "API key", checks, required=True)
        ok_all &= resolvable
        if config.get("webhookSecretReference"):
            ok_all &= _check_secret_resolves(config["webhookSecretReference"],
                                             "Webhook secret", checks,
                                             required=config.get("provider") == "meta")
        if resolvable and config.get("provider") == "meta":
            token = get_settings().resolve_secret(config["apiKeyReference"])
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    response = await client.get(
                        f"https://graph.facebook.com/v20.0/{config['phoneNumberId']}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                api_ok = response.status_code == 200
                ok_all &= api_ok
                checks.append({"name": "Meta Cloud API", "ok": api_ok,
                               "message": f"GET /{config['phoneNumberId']} → {response.status_code}"})
            except Exception as exc:  # noqa: BLE001
                ok_all = False
                checks.append({"name": "Meta Cloud API", "ok": False,
                               "message": f"Request failed ({exc.__class__.__name__})."})
        elif resolvable:
            checks.append({"name": "Provider API", "ok": True,
                           "message": "Credential reference resolves; live API check is "
                                      "only implemented for the Meta Cloud API."})

    elif row.type == "sms":
        resolvable = _check_secret_resolves(config.get("apiKeyReference", ""),
                                            "API key", checks, required=True)
        ok_all &= resolvable
        if resolvable and config.get("provider") == "twilio":
            token = get_settings().resolve_secret(config["apiKeyReference"])
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    response = await client.get(
                        f"https://api.twilio.com/2010-04-01/Accounts/{config['accountId']}.json",
                        auth=(config["accountId"], token),
                    )
                api_ok = response.status_code == 200
                ok_all &= api_ok
                checks.append({"name": "Twilio API", "ok": api_ok,
                               "message": f"GET account → {response.status_code}"})
            except Exception as exc:  # noqa: BLE001
                ok_all = False
                checks.append({"name": "Twilio API", "ok": False,
                               "message": f"Request failed ({exc.__class__.__name__})."})
        elif resolvable:
            checks.append({"name": "Provider API", "ok": True,
                           "message": "Credential reference resolves; live API check is "
                                      "only implemented for Twilio."})

    elif row.type in ("web", "mobile"):
        # Web chat + mobile SDK sessions run on the voice runtime WebSocket.
        ok_all &= await _check_voice_worker(checks)
        if row.type == "mobile":
            ok_all &= _check_secret_resolves(config.get("apiKeyReference", ""),
                                             "SDK API key", checks, required=False)

    failed = [c for c in checks if not c["ok"]]
    message = ("All checks passed." if ok_all
               else f"{len(failed)} of {len(checks)} checks failed: {failed[0]['message']}")
    return {"ok": ok_all, "message": message, "checks": checks}


@router.post("/bots/{bot_id}/channels/{channel_type}/test")
async def test_channel(
    bot_id: str, channel_type: str, request: Request,
    user: User = Depends(require_permission("manage_channels")),
    db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    row = _load_configured_channel(db, bot, channel_type)
    result = await _run_channel_test(db, bot, row)
    row.last_test = {"at": datetime.now(timezone.utc).isoformat() + "Z",
                     "ok": result["ok"], "message": result["message"],
                     "checks": result["checks"]}
    if result["ok"]:
        row.status = "live" if row.enabled else "configured"
    else:
        row.status = "failed"
    row.updated_by = user.id
    record_audit(
        db, user=user, action="Tested channel connection", entity_type="channel",
        entity_id=row.id, target_label=f"{bot.name} · {channel_type}",
        tenant_id=bot.tenant_id,
        new_value={"ok": result["ok"], "message": result["message"]},
        request=request,
    )
    db.commit()
    return ok(serialize_channel(row, binding=_binding(db, bot)))


# ── WhatsApp inbound webhook (Meta Cloud API style) ──────────────────────────


def _whatsapp_channel_or_404(db: Session, channel_id: str) -> ChannelConfig:
    row = db.get(ChannelConfig, channel_id)
    if (row is None or row.is_deleted or row.type != "whatsapp" or not row.config):
        raise ApiError("Unknown channel.", 404)
    return row


def _whatsapp_secret(row: ChannelConfig) -> str:
    secret = get_settings().resolve_secret(
        (row.config or {}).get("webhookSecretReference", ""))
    if not secret:
        raise ApiError("Channel webhook secret is not configured.", 503)
    return secret


@router.get("/channels/whatsapp/webhook/{channel_id}")
def whatsapp_webhook_verify(
    channel_id: str,
    db: Session = Depends(get_db),
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
):
    """Meta webhook subscription handshake: echo the challenge only when the
    verify token matches this channel's configured secret."""
    import hmac as _hmac

    row = _whatsapp_channel_or_404(db, channel_id)
    secret = _whatsapp_secret(row)
    if hub_mode != "subscribe" or not _hmac.compare_digest(hub_verify_token, secret):
        raise ApiError("Verification failed.", 403)
    return PlainTextResponse(hub_challenge)


@router.post("/channels/whatsapp/webhook/{channel_id}")
async def whatsapp_webhook(
    channel_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Inbound WhatsApp events. Verifies the Meta `X-Hub-Signature-256` HMAC,
    rejects replays, validates the tenant/bot mapping and refuses disabled
    channels — always with sanitized errors."""
    import hashlib
    import hmac as _hmac

    from backend.telephony.webhooks import WebhookVerificationError, check_replay

    row = _whatsapp_channel_or_404(db, channel_id)
    secret = _whatsapp_secret(row)

    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256", "")
    if not signature.startswith("sha256="):
        raise WebhookVerificationError("Missing signature header")
    expected = _hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(f"sha256={expected}", signature.strip()):
        raise WebhookVerificationError()
    await check_replay(signature + hashlib.sha256(raw_body).hexdigest())

    if not row.enabled:
        raise ApiError("This channel is not accepting messages.", 403)
    bot = db.get(VoiceBot, row.bot_id)
    if bot is None or bot.is_deleted or bot.tenant_id != row.tenant_id:
        raise ApiError("Unknown channel.", 404)
    if bot.status != "published":
        raise ApiError("This channel is not accepting messages.", 403)
    # Signature, replay, mapping and enablement verified. Message-processing
    # (conversation turns over WhatsApp) is a separate pipeline; events are
    # acknowledged so the provider does not retry forever.
    return ok({"received": True})


# ── Platform summary ──────────────────────────────────────────────────────────


@router.get("/channels/summary")
def channels_summary(user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    """Platform-wide per-channel status counts (VoicePlatform admin page)."""
    rows = db.execute(
        select(ChannelConfig.type, ChannelConfig.status, func.count())
        .where(ChannelConfig.is_deleted.is_(False))
        .group_by(ChannelConfig.type, ChannelConfig.status)
    ).all()
    summary: dict[str, dict] = {
        t: {"type": t, "live": 0, "testing": 0, "failed": 0, "configured": 0}
        for t in CHANNEL_TYPES
    }
    for ctype, status, count in rows:
        if ctype in summary and status in summary[ctype]:
            summary[ctype][status] = count
    return ok(list(summary.values()))
