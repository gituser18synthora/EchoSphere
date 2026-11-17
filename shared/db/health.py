"""Read-only backing-service readiness checks shared by API and monitoring.

Each check has a deadline, and checks run concurrently. The synchronous MySQL
SELECT runs off the event loop so an unavailable database cannot freeze other
health requests. No check creates schema, indexes, rows, or external paid calls.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from shared.db.mongo import Mongo
from shared.db.mysql import get_engine
from shared.db.postgres import pg_health_check
from shared.db.redis import redis_health_check

CHECK_TIMEOUT_SECONDS = 2.0
BACKING_SERVICES = ("mysql", "postgres", "redis", "mongodb")
_mysql_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mysql-readiness")


def _mysql_ping() -> dict:
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"ok": True}


async def _check(name: str) -> dict:
    if name == "mysql":
        # A cancelled async deadline cannot stop a synchronous DBAPI connect.
        # Limit this path to one worker so a prolonged outage cannot consume
        # the application's default executor with repeated health checks.
        return await asyncio.get_running_loop().run_in_executor(_mysql_executor, _mysql_ping)
    if name == "postgres":
        return await pg_health_check()
    if name == "redis":
        return await redis_health_check()
    if name == "mongodb":
        await Mongo.db().command("ping")
        return {"ok": True}
    raise ValueError("Unknown backing service")


async def check_backing_service(name: str) -> dict:
    if name not in BACKING_SERVICES:
        raise ValueError("Unknown backing service")
    try:
        return await asyncio.wait_for(_check(name), timeout=CHECK_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — readiness reports failures
        return {"ok": False, "error": exc.__class__.__name__}


async def check_backing_services() -> dict[str, dict]:
    results = await asyncio.gather(*(check_backing_service(name) for name in BACKING_SERVICES))
    return dict(zip(BACKING_SERVICES, results, strict=True))
