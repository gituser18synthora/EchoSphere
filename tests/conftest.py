"""Shared fixtures.

Integration tests run against the REAL local services (MySQL, PostgreSQL,
Redis, MongoDB) but only ever create/delete their own uniquely-prefixed rows —
existing data is never touched (no truncation, no resets).
"""

import os

os.environ.setdefault("ECHOSPHERE_TEST_NULLPOOL", "1")

import uuid

import pytest
from sqlalchemy import create_engine, delete, text
from sqlalchemy.orm import Session

from shared.config import get_settings
from shared.ids import new_id
from shared.db.mysql import get_sessionmaker
from shared.knowledge.embeddings.mock_provider import MockEmbeddingProvider
from shared.knowledge.service import KnowledgeService
from shared.knowledge.vector_store import PgVectorStore
from shared.models import KnowledgeSource, Tenant


@pytest.fixture(scope="session")
def mock_embedder():
    return MockEmbeddingProvider(dimension=1536)


@pytest.fixture()
def store():
    return PgVectorStore()


@pytest.fixture()
def knowledge_service(store, mock_embedder):
    return KnowledgeService(store=store, embedder=mock_embedder)


class ControlPlaneFactory:
    """Creates uniquely-named MySQL rows and removes exactly those on teardown."""

    def __init__(self) -> None:
        self.tenant_ids: list[str] = []
        self.kb_ids: list[str] = []

    def tenant(self, name: str = "Test Tenant") -> str:
        session = get_sessionmaker()()
        try:
            tenant_id = f"tn_test_{uuid.uuid4().hex[:10]}"
            session.add(
                Tenant(
                    id=tenant_id, name=f"{name} {tenant_id[-4:]}",
                    domain=f"{tenant_id}.example.test", status="active",
                )
            )
            session.commit()
            self.tenant_ids.append(tenant_id)
            return tenant_id
        finally:
            session.close()

    def knowledge_source(
        self, tenant_id: str | None, *, scope: str = "tenant", status: str = "indexed",
        name: str = "Test KB", bot_id: str | None = None,
    ) -> str:
        session = get_sessionmaker()()
        try:
            kb_id = f"kstest_{uuid.uuid4().hex[:10]}"
            session.add(
                KnowledgeSource(
                    id=kb_id, tenant_id=tenant_id, bot_id=bot_id, scope=scope,
                    type="document", name=name, status=status,
                )
            )
            session.commit()
            self.kb_ids.append(kb_id)
            return kb_id
        finally:
            session.close()

    def cleanup(self) -> None:
        session = get_sessionmaker()()
        try:
            if self.kb_ids:
                session.execute(
                    delete(KnowledgeSource).where(KnowledgeSource.id.in_(self.kb_ids))
                )
            if self.tenant_ids:
                session.execute(delete(Tenant).where(Tenant.id.in_(self.tenant_ids)))
            session.commit()
        finally:
            session.close()


@pytest.fixture()
def control_plane():
    factory = ControlPlaneFactory()
    yield factory
    factory.cleanup()


@pytest.fixture()
async def pg_cleanup():
    """Collects PG document ids created during a test and hard-deletes them."""
    created: list[str] = []
    yield created
    if created:
        from sqlalchemy import delete as sa_delete

        from shared.db.postgres import get_pg_sessionmaker
        from shared.knowledge.models import IngestionJob, KnowledgeChunk, KnowledgeDocument

        async with get_pg_sessionmaker()() as session:
            await session.execute(
                sa_delete(IngestionJob).where(IngestionJob.document_id.in_(created))
            )
            await session.execute(
                sa_delete(KnowledgeChunk).where(KnowledgeChunk.document_id.in_(created))
            )
            await session.execute(
                sa_delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(created))
            )
            await session.commit()


def make_pdf_bytes(text: str, pages: int = 1) -> bytes:
    import fitz

    doc = fitz.open()
    for page_index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Section {page_index + 1}", fontsize=16)
        page.insert_textbox(fitz.Rect(72, 100, 520, 700), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture()
def pdf_bytes():
    return make_pdf_bytes(
        "The policy grace period for renewal is exactly 30 days. "
        "Customers may renew within the grace period without penalty. " * 8
    )


# ── Database isolation for seed tests ────────────────────────────────────────
# The demo dataset writes business rows (tenants, users, invoices, …) and the
# production-initialisation tests need an EMPTY schema. Both must only ever run
# against a disposable database created for the test; the shared development
# database is read, never written. Note that ``get_settings`` is lru_cached, so
# an environment override only takes effect after ``get_settings.cache_clear()``
# — the fixtures below do that, and the demo-seed guard checks the database the
# code would really write to, not what a test believes it configured.

DISPOSABLE_DB_PREFIX = "echosphere_disposable_"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SHARED_DB_WATCHED_TABLES = (
    "users", "tenants", "invoices", "subscriptions", "voice_bots", "plans",
    "voice_profiles", "provider_defs", "provider_models", "platform_templates",
    "integrations", "approved_models", "audit_logs",
)


def _is_disposable(database: str | None) -> bool:
    return bool(database) and database.startswith(DISPOSABLE_DB_PREFIX)


@pytest.fixture(autouse=True, scope="session")
def _demo_seed_only_on_disposable_databases():
    """Fail fast if any test would run the demo seed against a shared database."""
    import shared.db.mysql as mysql_db
    import backend.seeds.demo_seed as demo_seed

    original = demo_seed.run_demo_seed

    def guarded(db=None, days: int = 90):
        if isinstance(db, Session):
            target = db.get_bind().url.database
        elif db is None:
            # What run_demo_seed() would really open: the already-bound engine
            # if there is one, otherwise the (uncached) current settings.
            target = (
                mysql_db._engine.url.database if mysql_db._engine is not None
                else get_settings().mysql_database
            )
        else:
            target = None  # sentinel objects used by unit tests never reach a database
        if target is not None and not _is_disposable(target):
            raise AssertionError(
                f"run_demo_seed() would write to the shared database {target!r}; "
                f"tests may only load demo data into a disposable "
                f"'{DISPOSABLE_DB_PREFIX}*' schema (fixture disposable_mysql_database)"
            )
        return original(db=db, days=days)

    demo_seed.run_demo_seed = guarded
    yield
    demo_seed.run_demo_seed = original


@pytest.fixture()
def shared_database_untouched():
    """Snapshot the shared database before the test and verify it is unchanged after."""
    settings = get_settings()
    engine = create_engine(settings.mysql_url)

    def snapshot() -> dict:
        with engine.connect() as conn:
            counts = {
                table: conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar()
                for table in _SHARED_DB_WATCHED_TABLES
            }
            counts["alembic_version"] = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar()
            return counts

    before = snapshot()
    yield settings.mysql_database
    after = snapshot()
    engine.dispose()
    assert after == before, (
        f"shared database {settings.mysql_database!r} changed during the test: "
        f"{ {k: (before[k], after[k]) for k in before if before[k] != after[k]} } "
        "(either the test leaked out of its disposable schema, or another test "
        "process wrote to the shared database concurrently — run this suite alone)"
    )


@pytest.fixture()
def disposable_mysql_database(monkeypatch, shared_database_untouched):
    """A brand-new MySQL schema owned by the test and dropped afterwards.

    While active, ``Settings`` and the process-wide engine/sessionmaker point at
    that schema, so seed code that opens its own session cannot reach the
    development database. The fixture fails — it never falls back to the
    shared database — when the host is not local, the schema cannot be
    created, or the override does not take effect.
    """
    import shared.db.mysql as mysql_db

    settings = get_settings()
    if settings.mysql_host not in _LOCAL_HOSTS:
        pytest.fail(
            f"refusing to create a disposable database on non-local MySQL host "
            f"{settings.mysql_host!r}"
        )
    name = f"{DISPOSABLE_DB_PREFIX}{uuid.uuid4().hex[:10]}"
    assert name != settings.mysql_database
    admin = create_engine(settings.mysql_url.replace(f"/{settings.mysql_database}?", "/?"))
    try:
        with admin.begin() as conn:
            conn.execute(text(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            ))
    except Exception as exc:  # privilege or connectivity problem: do not continue
        admin.dispose()
        pytest.fail(f"cannot create disposable database {name!r}: {exc}")

    previous_engine, previous_sessionmaker = mysql_db._engine, mysql_db._SessionLocal
    monkeypatch.setenv("MYSQL_DATABASE", name)
    get_settings.cache_clear()
    mysql_db._engine = None
    mysql_db._SessionLocal = None
    try:
        effective = get_settings().mysql_database
        if effective != name:
            pytest.fail(f"MYSQL_DATABASE override did not take effect (settings say {effective!r})")
        yield name
    finally:
        if mysql_db._engine is not None:
            mysql_db._engine.dispose()
        mysql_db._engine, mysql_db._SessionLocal = previous_engine, previous_sessionmaker
        monkeypatch.undo()          # restore the environment now, then drop the cache
        get_settings.cache_clear()
        with admin.begin() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
        admin.dispose()
