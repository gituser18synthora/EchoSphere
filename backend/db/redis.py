"""Redis — async client used for voice-session state, routing/config cache,
rate limiting and distributed locks. Keys are always tenant-scoped by callers.
"""

import redis.asyncio as aioredis

from backend.config import get_settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            get_settings().redis_url, decode_responses=True
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def redis_health_check() -> dict:
    try:
        pong = await get_redis().ping()
        return {"ok": bool(pong)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": exc.__class__.__name__}
