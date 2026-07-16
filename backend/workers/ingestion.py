"""Ingestion worker — polls the Postgres job queue and runs the pipeline.

Run: python -m backend.workers.ingestion
Multiple instances are safe (jobs claimed with FOR UPDATE SKIP LOCKED).
Shutdown: SIGINT/SIGTERM finish in-flight jobs, then exit.
"""

import asyncio
import logging
import signal

from backend.config import get_settings
from backend.knowledge.ingestion.pipeline import IngestionPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("backend.workers.ingestion")

_CONCURRENCY = 2


async def run_worker(stop_event: asyncio.Event | None = None) -> None:
    settings = get_settings()
    pipeline = IngestionPipeline()
    stop = stop_event or asyncio.Event()
    inflight: set[asyncio.Task] = set()

    logger.info(
        "Ingestion worker started (poll=%ss, concurrency=%d, embedder=%s)",
        settings.ingestion_worker_poll_seconds, _CONCURRENCY, settings.embedding_provider,
    )
    while not stop.is_set():
        inflight = {t for t in inflight if not t.done()}
        job_id = None
        if len(inflight) < _CONCURRENCY:
            try:
                job_id = await pipeline.claim_next_job()
            except Exception:  # noqa: BLE001 - DB blips must not kill the worker
                logger.exception("claim_next_job failed; backing off")
                await asyncio.sleep(5)
                continue
        if job_id:
            logger.info("claimed job %s", job_id)
            inflight.add(asyncio.create_task(pipeline.process_job(job_id)))
        else:
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.ingestion_worker_poll_seconds
                )
            except TimeoutError:
                pass
    if inflight:
        logger.info("draining %d in-flight job(s)", len(inflight))
        await asyncio.gather(*inflight, return_exceptions=True)
    logger.info("Ingestion worker stopped")


def main() -> None:
    stop = asyncio.Event()

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(stop)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
