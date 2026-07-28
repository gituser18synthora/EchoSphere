"""Telephony webhooks and provider catalog.

POST /telephony/webhook/{provider} answers an inbound call: verify the
webhook signature → resolve the dialed number to a tenant/bot (trusted
mapping) → issue a voice session → return the provider's connect payload
that points its media stream at the voice worker.
"""

import logging
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from shared.config import get_settings
from backend.core.deps import require_tenant_member
from shared.errors import ApiError
from backend.core.responses import ok
from shared.models import User
from shared.telephony import (
    SUPPORTED_PROVIDERS,
    TelephonyProviderConfig,
    connect_instructions,
)
from backend.telephony.webhooks import (
    check_replay,
    verify_generic_signature,
    verify_twilio_signature,
)
from shared.bot_config import resolve_bot_for_phone_number
from shared.voice_sessions import create_voice_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Telephony"])


def _assert_voice_channel_enabled(bot_id: str) -> None:
    """Reject inbound calls for bots whose voice channel is deactivated.

    A bot without any voice ChannelConfig row is treated as implicitly enabled
    (legacy numbers provisioned before channel management existed)."""
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import ChannelConfig

    session = get_sessionmaker()()
    try:
        row = session.scalar(
            select(ChannelConfig).where(
                ChannelConfig.bot_id == bot_id,
                ChannelConfig.type == "voice",
                ChannelConfig.is_deleted.is_(False),
            )
        )
        if row is not None and not row.enabled:
            # Sanitized: reveal nothing about tenant/bot/channel internals.
            raise ApiError("This number is not accepting calls.", status_code=403)
    finally:
        session.close()


def _public_ws_base(settings, request: Request) -> str:
    """Base URL providers use to reach the voice worker's media WebSocket.

    The explicit TELEPHONY_PUBLIC_WS_BASE setting wins — the API and the voice
    worker are separate processes, so the webhook's own host:port is only
    correct behind a proxy that routes /ws/telephony/* to the worker. Without
    the setting, the historical behavior (derive from the request) is kept.
    """
    configured = (settings.telephony_public_ws_base or "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).replace("http", "ws", 1).rstrip("/")


_VARIABLE_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_MAX_VARIABLES = 20
_MAX_VARIABLE_CHARS = 200


def _sanitize_variables(raw: object) -> dict[str, str]:
    """Bound and normalize dialer-supplied per-call variables.

    The webhook is HMAC-signed, so the sender is trusted — but values still
    get size/shape limits before they reach Redis, logs, or the LLM context.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if len(out) >= _MAX_VARIABLES:
            break
        key = str(key).strip()
        if not _VARIABLE_KEY.match(key):
            continue
        if isinstance(value, (str, int, float, bool)):
            out[key] = str(value)[:_MAX_VARIABLE_CHARS]
    return out


async def _extract_call_details(
    provider: str, request: Request
) -> tuple[str, str | None, str | None, dict[str, str]]:
    """Returns (called_number, caller_number, call_id, variables)."""
    if provider == "twilio":
        form = await request.form()
        return form.get("To", ""), form.get("From"), form.get("CallSid"), {}
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        form = await request.form()
        body = dict(form)
    called = (
        body.get("To") or body.get("to") or body.get("CallTo")
        or body.get("called_number") or ""
    )
    caller = body.get("From") or body.get("from") or body.get("caller_number")
    call_id = (
        body.get("CallSid") or body.get("callId") or body.get("call_id") or None
    )
    variables = _sanitize_variables(body.get("variables"))
    return called, caller, (str(call_id)[:64] if call_id else None), variables


@router.post("/telephony/webhook/{provider}")
async def inbound_call_webhook(provider: str, request: Request):
    if provider not in SUPPORTED_PROVIDERS:
        raise ApiError(f"Unsupported telephony provider '{provider}'", status_code=404)
    settings = get_settings()
    raw_body = await request.body()

    # ── signature verification + replay protection ────────────────────────
    if provider == "twilio":
        signature = request.headers.get("x-twilio-signature", "")
        auth_token = settings.resolve_secret(settings.telephony_webhook_secret_reference)
        if not auth_token:
            raise ApiError("Telephony webhook secret not configured", status_code=503)
        form = await request.form()
        verify_twilio_signature(
            url=str(request.url), params=dict(form), signature=signature,
            auth_token=auth_token,
        )
        await check_replay(signature + form.get("CallSid", ""))
    else:
        signature = request.headers.get("x-webhook-signature", "")
        timestamp = request.headers.get("x-webhook-timestamp", "")
        verify_generic_signature(body=raw_body, signature=signature, timestamp=timestamp)
        await check_replay(signature)

    # ── trusted routing: number → tenant → bot → published config ─────────
    called, caller, call_id, variables = await _extract_call_details(provider, request)
    if not called:
        raise ApiError("Webhook payload missing the dialed number", status_code=422)
    config = await resolve_bot_for_phone_number(called)

    # A deactivated voice channel must not receive calls (sanitized error).
    _assert_voice_channel_enabled(config.bot_id)

    session = await create_voice_session(
        tenant_id=config.tenant_id,
        bot_id=config.bot_id,
        user_id=None,
        channel="phone",
        caller=caller,
        call_id=call_id,
        variables=variables,
    )
    logger.info(
        "telephony.inbound provider=%s called=%s tenant=%s bot=%s session=%s "
        "call_id=%s variables=%d",
        provider, called, config.tenant_id, config.bot_id, session["session_id"],
        call_id, len(variables),
    )

    provider_config = TelephonyProviderConfig(
        provider=provider,
        auth_token_reference=settings.telephony_webhook_secret_reference,
        public_ws_base=_public_ws_base(settings, request),
    )
    instructions = connect_instructions(provider, provider_config, session["session_id"])
    return Response(content=instructions.body, media_type=instructions.content_type)


@router.get("/providers/voice-catalog")
def provider_catalog(user: User = Depends(require_tenant_member)):
    """Available STT/TTS/LLM providers for the studio configuration UI.

    Sourced from the provider_defs catalog (active rows with a registered
    adapter) — no hardcoded provider lists.
    """
    from sqlalchemy import select as sa_select

    from shared.db.mysql import get_sessionmaker
    from shared.models import ProviderDef
    from shared.providers.factory import _REGISTRY

    catalog: dict[str, list[str]] = {"stt": [], "tts": [], "llm": []}
    session = get_sessionmaker()()
    try:
        rows = session.execute(
            sa_select(ProviderDef.kind, ProviderDef.code)
            .where(
                ProviderDef.kind.in_(("stt", "tts", "llm")),
                ProviderDef.status == "active",
                ProviderDef.is_deleted.is_(False),
            )
            .order_by(ProviderDef.sort_order)
        ).all()
    finally:
        session.close()
    for kind, code in rows:
        if code != "mock" and (kind, code) in _REGISTRY and code not in catalog[kind]:
            catalog[kind].append(code)
    defaults = get_settings()
    return ok(
        {
            "providers": catalog,
            "defaults": {
                "stt": {"provider": defaults.stt_provider, "model": defaults.stt_model},
                "tts": {"provider": defaults.tts_provider, "model": defaults.tts_model,
                        "voice": defaults.tts_voice},
                "llm": {"provider": defaults.llm_provider, "model": defaults.llm_model},
            },
            "telephonyProviders": list(SUPPORTED_PROVIDERS),
        }
    )
