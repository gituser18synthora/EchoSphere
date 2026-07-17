"""Redis — async client used for voice-session state, routing/config cache,
rate limiting and distributed locks. Keys are always tenant-scoped by callers.

Clients are cached per event loop: asyncio Redis connections are bound to the
loop that created them, and test harnesses (and multi-loop hosts) would
otherwise share a connection across loops. In normal single-loop processes
this behaves exactly like a singleton.
"""

import asyncio

import redis.asyncio as aioredis

from backend.config import get_settings

_clients: dict[int, aioredis.Redis] = {}


def get_redis() -> aioredis.Redis:
    loop_id = id(asyncio.get_event_loop())
    client = _clients.get(loop_id)
    if client is None:
        client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
        _clients[loop_id] = client
    return client


async def close_redis() -> None:
    loop_id = id(asyncio.get_event_loop())
    client = _clients.pop(loop_id, None)
    if client is not None:
        await client.aclose()


async def redis_health_check() -> dict:
    try:
        pong = await get_redis().ping()
        return {"ok": bool(pong)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": exc.__class__.__name__}
