"""PostgreSQL (knowledge plane) — async SQLAlchemy 2.0 + asyncpg + pgvector.

The control plane stays on MySQL; this engine owns knowledge documents,
chunks and embeddings only. Connections are pooled and request-scoped
sessions are provided through `get_pg_db`.
"""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from shared.config import get_settings

_engine: AsyncEngine | None = None
_SessionLocal: async_sessionmaker[AsyncSession] | None = None


def get_pg_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        import os

        settings = get_settings()
        if os.environ.get("ECHOSPHERE_TEST_NULLPOOL") not in (None, "", "0"):
            # Tests drive the app from several event loops (pytest session loop
            # + TestClient portals); pooled asyncpg connections are loop-bound,
            # so tests run without a pool.
            from sqlalchemy.pool import NullPool

            _engine = create_async_engine(settings.postgres_url, poolclass=NullPool)
        else:
            _engine = create_async_engine(
                settings.postgres_url,
                pool_pre_ping=True,
                pool_recycle=3600,
                pool_size=settings.postgres_pool_size,
                max_overflow=settings.postgres_max_overflow,
            )
    return _engine


def get_pg_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = async_sessionmaker(
            bind=get_pg_engine(), autoflush=False, expire_on_commit=False
        )
    return _SessionLocal


async def get_pg_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped async session."""
    async with get_pg_sessionmaker()() as session:
        yield session


async def dispose_pg_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _SessionLocal = None


async def pg_health_check() -> dict:
    """Liveness probe: connection + pgvector extension + a real vector operation."""
    try:
        async with get_pg_engine().connect() as conn:
            ext = (
                await conn.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
            ).scalar()
            if not ext:
                return {"ok": False, "error": "pgvector extension not installed"}
            # Exercise the vector type end to end (cast + distance operator).
            await conn.execute(text("SELECT '[1,0]'::vector <=> '[0,1]'::vector"))
        return {"ok": True, "pgvector": ext}
    except Exception as exc:  # noqa: BLE001 - health check reports, never raises
        return {"ok": False, "error": exc.__class__.__name__}
