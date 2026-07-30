"""Telephony webhooks and provider catalog.

POST /telephony/webhook/{provider} answers an inbound call: verify the
webhook signature → resolve the dialed number (+ optional botId) to a
tenant/bot (trusted mapping) → issue a voice session → return the provider's
connect payload that points its media stream at the voice worker.

The handler itself lives in shared.telephony_webhooks so the voice worker's
telephony gateway instance can serve the identical webhook at its root path
(one public host:port for webhook + media WebSocket).
"""

import logging

from fastapi import APIRouter, Depends, Request

from shared.config import get_settings
from backend.core.deps import require_tenant_member
from backend.core.responses import ok
from shared.models import User
from shared.telephony import SUPPORTED_PROVIDERS
from shared.telephony_webhooks import (  # noqa: F401  (re-exported for tests)
    _public_ws_base,
    _sanitize_variables,
    handle_inbound_call_webhook,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Telephony"])


@router.post("/telephony/webhook/{provider}")
async def inbound_call_webhook(provider: str, request: Request):
    return await handle_inbound_call_webhook(provider, request)


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
