"""Health collection independent of open browser tabs.

Run a dedicated collector with: python -m backend.workers.monitoring
Disable the embedded collector with MONITORING_WORKER_EMBEDDED=false.
"""
import asyncio
import logging
import signal
import uuid
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.monitoring import claim_collection, persist_collection
from backend.core.service_health import probe_services
from shared.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_sessionmaker():
    # Collector transactions have their own small pool and DBAPI timeouts.
    # An unavailable DB must not leave shutdown waiting on an unbounded thread
    # or change timeout policy for business requests on the shared API pool.
    engine = create_engine(get_settings().mysql_url, pool_size=1, max_overflow=0,
        pool_timeout=5, pool_pre_ping=True, pool_recycle=1800,
        connect_args={"connect_timeout": 3, "read_timeout": 5, "write_timeout": 5})
    return sessionmaker(engine, autoflush=False, expire_on_commit=False)


async def collect_once(*, owner: str | None = None, in_process_api: bool = False) -> bool:
    owner = owner or uuid.uuid4().hex
    settings = get_settings()

    def claim():
        with get_sessionmaker()() as db:
            return claim_collection(db, owner)
    if not await asyncio.to_thread(claim):
        return False
    # Well below the fencing lease. A hung dependency cannot retain leadership.
    async with asyncio.timeout(20):
        metrics = await probe_services(in_process_api=in_process_api)

    def persist():
        with get_sessionmaker()() as db:
            return persist_collection(db, metrics, owner, poll_seconds=settings.monitoring_poll_seconds,
                retention_hours=settings.monitoring_retention_hours, failure_threshold=settings.monitoring_failure_threshold)
    return await asyncio.to_thread(persist)


async def run_worker(stop_event: asyncio.Event | None = None, *, in_process_api: bool = True) -> None:
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            await collect_once(in_process_api=in_process_api)
        except Exception as exc:
            # Do not dump dependency DSNs, SQL parameters or credentials.
            logger.warning("Health collection failed (%s); retrying next interval", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(10, get_settings().monitoring_poll_seconds))
        except TimeoutError:
            pass


async def _main():
    from shared.config import validate_settings
    from shared.db.mongo import Mongo
    validate_settings("api")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        try:
            await Mongo.connect()
        except Exception as exc:
            # The initialized Motor client can reconnect after the outage.
            # Keep collecting other services while Mongo is unavailable.
            logger.warning("Collector Mongo connection unavailable (%s)", type(exc).__name__)
        await run_worker(stop, in_process_api=False)
    finally:
        await Mongo.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
