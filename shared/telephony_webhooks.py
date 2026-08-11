"""Inbound-call webhook handling shared by both HTTP surfaces.

Signature verification (with replay protection), dialer routing and
voice-session issuance for ``POST …/telephony/webhook/{provider}``. The same
handler is mounted twice:

- by the platform API under ``/api/v1`` (historical path), and
- by the voice worker at the root path, so one public host:port — the
  telephony *gateway* instance (``python -m voice_runtime.gateway``) — serves
  both the webhook and the media WebSocket it points at.

Two signature schemes:
- Twilio: HMAC-SHA1 over URL + sorted POST params, base64, `X-Twilio-Signature`.
- Generic (Exotel/Plivo/Telnyx/Vaani/FreeSWITCH event socket bridges): HMAC-SHA256
  over `<timestamp>.<raw body>` with `X-Webhook-Signature` + `X-Webhook-Timestamp`.

Replay protection: signatures are single-use within their validity window
(Redis SETNX), and timestamps older than MAX_SKEW are rejected.
"""

import base64
import hashlib
import hmac
import logging
import re
import time

from fastapi import Request
from fastapi.responses import Response

from shared.bot_config import resolve_bot_for_dialer
from shared.config import get_settings
from shared.errors import ApiError
from shared.telephony import (
    SUPPORTED_PROVIDERS,
    TelephonyProviderConfig,
    connect_instructions,
)
from shared.voice_sessions import create_voice_session

logger = logging.getLogger(__name__)

MAX_SKEW_SECONDS = 300


class WebhookVerificationError(ApiError):
    def __init__(self, message: str = "Invalid webhook signature") -> None:
        super().__init__(message, status_code=403)


def _secret() -> str:
    settings = get_settings()
    secret = settings.resolve_secret(settings.telephony_webhook_secret_reference)
    if not secret:
        raise WebhookVerificationError("Webhook secret is not configured")
    return secret


def verify_generic_signature(
    *, body: bytes, signature: str, timestamp: str, secret: str | None = None
) -> None:
    """HMAC-SHA256(`<ts>.<body>`) — constant-time compare + freshness check."""
    if not signature or not timestamp:
        raise WebhookVerificationError("Missing signature headers")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("Invalid timestamp") from exc
    if abs(time.time() - ts) > MAX_SKEW_SECONDS:
        raise WebhookVerificationError("Webhook timestamp outside validity window")
    key = (secret or _secret()).encode()
    expected = hmac.new(key, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise WebhookVerificationError()


def verify_twilio_signature(
    *, url: str, params: dict[str, str], signature: str, auth_token: str
) -> None:
    """Twilio's documented scheme: HMAC-SHA1(url + sorted k+v), base64."""
    if not signature:
        raise WebhookVerificationError("Missing X-Twilio-Signature")
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    if not hmac.compare_digest(expected, signature.strip()):
        raise WebhookVerificationError()


async def check_replay(signature: str, window_seconds: int = MAX_SKEW_SECONDS * 2) -> None:
    """Reject a signature that was already accepted (single-use)."""
    from shared.db.redis import get_redis

    key = f"webhook:seen:{hashlib.sha256(signature.encode()).hexdigest()}"
    try:
        fresh = await get_redis().set(key, "1", nx=True, ex=window_seconds)
    except Exception:  # noqa: BLE001 - Redis outage: fail open but log loudly
        logger.error("replay-protection store unavailable; accepting webhook unchecked")
        return
    if not fresh:
        raise WebhookVerificationError("Webhook replay detected")


# ── inbound-call handling ────────────────────────────────────────────────


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

_BOT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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
) -> tuple[str, str | None, str | None, dict[str, str], str | None, str | None]:
    """Returns (called_number, caller_number, call_id, variables, bot_id,
    direction). ``direction`` is the dialer's own declaration when present
    ("inbound"/"outbound"); compliance policies may configure an assumption
    for payloads that omit it."""
    if provider == "twilio":
        form = await request.form()
        direction = str(form.get("Direction") or "").strip().lower() or None
        if direction and "outbound" in direction:
            direction = "outbound"
        return (form.get("To", ""), form.get("From"), form.get("CallSid"),
                {}, None, direction)
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
    direction = str(body.get("Direction") or body.get("direction") or "").strip().lower() or None
    if direction not in (None, "inbound", "outbound"):
        direction = "outbound" if "outbound" in direction else "inbound"
    # Optional per-campaign bot selection (see resolve_bot_for_dialer): the
    # dialed number stays the tenant authority; botId only picks within it.
    bot_id = body.get("botId") or body.get("bot_id") or None
    if bot_id is not None:
        bot_id = str(bot_id).strip()
        if not _BOT_ID.match(bot_id):
            raise ApiError("Invalid botId in webhook payload", status_code=422)
    return (called, caller, (str(call_id)[:64] if call_id else None),
            variables, bot_id, direction)


async def enforce_pre_call_compliance(
    *,
    tenant_id: str,
    bot_id: str,
    caller: str | None,
    direction: str | None,
    call_id: str | None = None,
    channel: str = "phone",
    now=None,
) -> None:
    """Deterministic pre-connect gate — the platform's 'immediately before
    dialing' checkpoint (the external dialer initiates; this is where the
    platform accepts or refuses the call, before any media flows).

    Enforces, in order: the bot's effective ``outbound_call_block`` guardrail
    (development/sandbox profiles), then every ACTIVE compliance policy's
    calling window and per-day contact limit. A refusal raises a sanitized
    403 and writes a tenant-scoped trigger row carrying the policy code and
    version — never the caller's number.
    """
    import asyncio

    from shared.compliance import (
        check_and_count_contact,
        check_calling_window,
        load_active_policies_sync,
        record_policy_trigger_sync,
    )
    from shared.guardrails import load_effective_guardrails_sync

    effective = await asyncio.to_thread(
        load_effective_guardrails_sync, tenant_id, bot_id
    )
    if effective.has("outbound_call_block"):
        await asyncio.to_thread(
            record_policy_trigger_sync,
            tenant_id=tenant_id, bot_id=bot_id, session_id=call_id,
            rule="outbound_call_block", action="block", stage="call",
            outcome="blocked", channel=channel,
            detail="telephony disabled by the bot's guardrail profile",
        )
        raise ApiError("This number is not accepting calls.", status_code=403)

    policies = await asyncio.to_thread(load_active_policies_sync, tenant_id)
    contact_counted = False
    for policy in policies:
        if not policy.applies(
            channel=channel, direction=policy.effective_direction(direction)
        ):
            continue
        decision = check_calling_window(policy, now)
        if not decision.allowed:
            await asyncio.to_thread(
                record_policy_trigger_sync,
                tenant_id=tenant_id, bot_id=bot_id, session_id=call_id,
                rule="calling_window", action="block", stage="call",
                outcome="blocked", policy=policy, channel=channel,
                detail=f"{decision.reason} (local {decision.local_time})",
            )
            raise ApiError("Calls are not permitted at this time.", status_code=403)
        if not contact_counted and (policy.contact_limits or {}).get("per_day"):
            contact_counted = True  # one shared counter — never double-count
            allowed, attempts = await check_and_count_contact(
                policy, tenant_id=tenant_id, caller=caller, now=now
            )
            if not allowed:
                await asyncio.to_thread(
                    record_policy_trigger_sync,
                    tenant_id=tenant_id, bot_id=bot_id, session_id=call_id,
                    rule="contact_limit", action="block", stage="call",
                    outcome="blocked", policy=policy, channel=channel,
                    detail=f"daily contact limit reached (attempt {attempts})",
                )
                raise ApiError(
                    "The daily contact limit for this number has been reached.",
                    status_code=403,
                )


async def handle_inbound_call_webhook(provider: str, request: Request) -> Response:
    """Answer an inbound-call webhook: verify the signature → resolve the
    tenant/bot from the dialed number (+ optional botId) → issue a voice
    session → return the provider's connect payload pointing its media
    stream at the voice worker."""
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

    # ── trusted routing: number (+ botId) → tenant → bot → published config ─
    called, caller, call_id, variables, requested_bot, direction = (
        await _extract_call_details(provider, request)
    )
    if not called:
        raise ApiError("Webhook payload missing the dialed number", status_code=422)
    config = await resolve_bot_for_dialer(called, requested_bot)

    # A deactivated voice channel must not receive calls (sanitized error).
    _assert_voice_channel_enabled(config.bot_id)

    # Deterministic compliance gate BEFORE the session exists: development
    # profiles that disable telephony, active-policy calling windows and
    # contact limits all refuse here — the dialer never gets a media URL.
    await enforce_pre_call_compliance(
        tenant_id=config.tenant_id,
        bot_id=config.bot_id,
        caller=caller,
        direction=direction,
        call_id=call_id,
    )

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
        "telephony.inbound provider=%s called=%s routed_via=%s tenant=%s bot=%s "
        "session=%s call_id=%s variables=%d",
        provider, called, "botId" if requested_bot else "number",
        config.tenant_id, config.bot_id, session["session_id"], call_id,
        len(variables),
    )

    provider_config = TelephonyProviderConfig(
        provider=provider,
        auth_token_reference=settings.telephony_webhook_secret_reference,
        public_ws_base=_public_ws_base(settings, request),
    )
    instructions = connect_instructions(provider, provider_config, session["session_id"])
    return Response(content=instructions.body, media_type=instructions.content_type)
