"""Shared fixtures.

Integration tests run against the REAL local services (MySQL, PostgreSQL,
Redis, MongoDB) but only ever create/delete their own uniquely-prefixed rows —
existing data is never touched (no truncation, no resets).
"""

import os

os.environ.setdefault("ECHOSPHERE_TEST_NULLPOOL", "1")

import uuid

import pytest
from sqlalchemy import delete

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
