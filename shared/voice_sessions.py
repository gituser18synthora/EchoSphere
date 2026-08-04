"""Redis voice-session store — the security handoff between the two services.

The platform API (``backend``) is the only authority: it authenticates the
user or verifies the telephony webhook signature, then calls
``create_voice_session`` to write the trusted session → tenant/bot mapping
into Redis (key ``voice:session:{id}``, TTL ``VOICE_SESSION_TIMEOUT``).

The voice worker (``voice_runtime``) only ever *consumes* sessions via
``load_voice_session`` — an unknown or expired id is rejected. The worker
never mints sessions and never decides tenancy itself.
"""

import json
import secrets
from datetime import datetime, timezone

from shared.config import get_settings
from shared.db.redis import get_redis

_SESSION_PREFIX = "voice:session:"


def _session_key(session_id: str) -> str:
    return f"{_SESSION_PREFIX}{session_id}"


async def create_voice_session(
    *,
    tenant_id: str,
    bot_id: str,
    user_id: str | None,
    channel: str = "browser",
    caller: str | None = None,
    call_id: str | None = None,
    variables: dict[str, str] | None = None,
    customer_context_id: str | None = None,
) -> dict:
    """Issue a session token (called from the authenticated API process).

    ``call_id`` is the telephony provider's own call identifier (log/trace
    correlation only); ``variables`` are dialer-supplied per-call values that
    arrived on a SIGNED webhook — already sanitized by the caller.
    ``customer_context_id`` pins the call to a specific customer_contexts row
    — the issuing API validated it belongs to this tenant/bot, and the worker
    re-validates on load.
    """
    settings = get_settings()
    session_id = f"vs_{secrets.token_urlsafe(18)}"
    payload = {
        "session_id": session_id,
        "tenant_id": tenant_id,
        "bot_id": bot_id,
        "user_id": user_id,
        "channel": channel,
        "caller": caller,
        "call_id": call_id,
        "variables": variables or {},
        "customer_context_id": customer_context_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "issued",
    }
    await get_redis().set(
        _session_key(session_id), json.dumps(payload), ex=settings.voice_session_timeout
    )
    return payload


async def load_voice_session(session_id: str) -> dict | None:
    """Trusted lookup used by the voice worker; returns None if expired/unknown."""
    raw = await get_redis().get(_session_key(session_id))
    return json.loads(raw) if raw else None


async def update_voice_session(session_id: str, **fields) -> None:
    redis = get_redis()
    raw = await redis.get(_session_key(session_id))
    if not raw:
        return
    payload = json.loads(raw)
    payload.update(fields)
    ttl = await redis.ttl(_session_key(session_id))
    await redis.set(_session_key(session_id), json.dumps(payload), ex=max(ttl, 60))


async def end_voice_session(session_id: str) -> None:
    await get_redis().delete(_session_key(session_id))
