"""Telephony webhooks and provider catalog.

POST /telephony/webhook/{provider} answers an inbound call: verify the
webhook signature → resolve the dialed number to a tenant/bot (trusted
mapping) → issue a voice session → return the provider's connect payload
that points its media stream at the voice worker.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from backend.config import get_settings
from backend.core.deps import require_tenant_member
from backend.core.errors import ApiError
from backend.core.responses import ok
from backend.models import User
from backend.telephony.providers import (
    SUPPORTED_PROVIDERS,
    TelephonyProviderConfig,
    connect_instructions,
)
from backend.telephony.webhooks import (
    check_replay,
    verify_generic_signature,
    verify_twilio_signature,
)
from backend.voice_runtime.bot_config import resolve_bot_for_phone_number
from backend.voice_runtime.session import create_voice_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Telephony"])


async def _extract_called_number(provider: str, request: Request) -> tuple[str, str | None]:
    """Returns (called_number, caller_number) from the provider payload."""
    if provider == "twilio":
        form = await request.form()
        return form.get("To", ""), form.get("From")
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
    return called, caller


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
    called, caller = await _extract_called_number(provider, request)
    if not called:
        raise ApiError("Webhook payload missing the dialed number", status_code=422)
    config = await resolve_bot_for_phone_number(called)

    session = await create_voice_session(
        tenant_id=config.tenant_id,
        bot_id=config.bot_id,
        user_id=None,
        channel="phone",
        caller=caller,
    )
    logger.info(
        "telephony.inbound provider=%s called=%s tenant=%s bot=%s session=%s",
        provider, called, config.tenant_id, config.bot_id, session["session_id"],
    )

    provider_config = TelephonyProviderConfig(
        provider=provider,
        auth_token_reference=settings.telephony_webhook_secret_reference,
        public_ws_base=str(request.base_url).replace("http", "ws", 1).rstrip("/"),
    )
    instructions = connect_instructions(provider, provider_config, session["session_id"])
    return Response(content=instructions.body, media_type=instructions.content_type)


@router.get("/providers/voice-catalog")
def provider_catalog(user: User = Depends(require_tenant_member)):
    """Available STT/TTS/LLM providers for the studio configuration UI."""
    from backend.providers.factory import _REGISTRY

    catalog: dict[str, list[str]] = {"stt": [], "tts": [], "llm": []}
    for (kind, name) in _REGISTRY:
        if name not in catalog[kind] and name != "mock":
            catalog[kind].append(name)
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
