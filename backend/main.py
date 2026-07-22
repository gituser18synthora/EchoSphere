"""EchoSphere platform API.

Run: .venv/bin/uvicorn backend.main:app --reload --port 8000
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from shared.config import get_settings
from shared.errors import install_error_handlers
from backend.core.responses import ok
from shared.db.mongo import Mongo, create_indexes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("backend")


def _run_migrations() -> None:
    import os

    from alembic import command
    from alembic.config import Config

    ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alembic.ini")
    command.upgrade(Config(ini), "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    import os

    settings = get_settings()
    if settings.auto_run_migrations:
        logger.info("AUTO_RUN_MIGRATIONS=true — applying migrations")
        _run_migrations()
    await Mongo.connect()
    await create_indexes()
    if settings.enable_database_seed:
        # Idempotent base seed only (roles, permissions, super admin, catalogs).
        from backend.seeds.base_seed import run_base_seed

        run_base_seed()

    # Knowledge ingestion is a Postgres poll queue — without a running worker
    # every upload stays "queued" forever. Embed the worker here unless a
    # dedicated worker process is used (INGESTION_WORKER_EMBEDDED=false).
    worker_task: asyncio.Task | None = None
    worker_stop: asyncio.Event | None = None
    if settings.ingestion_worker_embedded and not os.environ.get("ECHOSPHERE_TEST_NULLPOOL"):
        from backend.workers.ingestion import run_worker

        worker_stop = asyncio.Event()
        worker_task = asyncio.create_task(run_worker(worker_stop))
        logger.info("Embedded ingestion worker started")
    yield
    if worker_task and worker_stop:
        worker_stop.set()
        try:
            await asyncio.wait_for(worker_task, timeout=30)
        except (TimeoutError, asyncio.CancelledError):
            worker_task.cancel()
    await Mongo.disconnect()


def create_app() -> FastAPI:
    from shared.config import validate_settings

    validate_settings("api")
    settings = get_settings()
    app = FastAPI(
        title="EchoSphere Platform API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_error_handlers(app)

    from backend.routers import (
        analytics,
        apis,
        audit,
        auth,
        billing,
        bots,
        catalog,
        channels,
        conversations,
        integrations,
        intents,
        knowledge,
        knowledge_documents,
        knowledge_review,
        master_data,
        platform,
        prompts,
        providers,
        releases,
        telephony,
        tenants,
        testing,
        users,
        voice_sessions,
        workflows,
    )

    prefix = "/api/v1"
    for module in (
        auth, users, tenants, billing, bots, catalog, knowledge, knowledge_documents,
        knowledge_review, prompts, intents, apis, workflows, channels, testing, releases,
        conversations, platform, integrations, audit, analytics, voice_sessions, telephony,
        master_data, providers,
    ):
        app.include_router(module.router, prefix=prefix)

    @app.get("/api/health")
    def health():
        return ok({"status": "up", "env": settings.app_env})

    @app.get("/api/health/ready")
    async def readiness():
        """Readiness: checks every backing service the API depends on."""
        from sqlalchemy import text as sa_text

        from shared.db.mysql import get_engine
        from shared.db.postgres import pg_health_check
        from shared.db.redis import redis_health_check

        checks: dict[str, dict] = {}
        try:
            with get_engine().connect() as conn:
                conn.execute(sa_text("SELECT 1"))
            checks["mysql"] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            checks["mysql"] = {"ok": False, "error": exc.__class__.__name__}
        checks["postgres"] = await pg_health_check()
        checks["redis"] = await redis_health_check()
        try:
            await Mongo.db().command("ping")
            checks["mongodb"] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            checks["mongodb"] = {"ok": False, "error": exc.__class__.__name__}
        healthy = all(c.get("ok") for c in checks.values())
        return ok({"ready": healthy, "checks": checks})

    return app


app = create_app()
