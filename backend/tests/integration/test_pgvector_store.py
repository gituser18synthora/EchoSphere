"""PgVectorStore against the real PostgreSQL: upserts, tenant filters, search."""

import hashlib
import uuid

import pytest
from sqlalchemy import delete as sa_delete

from backend.db.postgres import get_pg_sessionmaker
from backend.knowledge.models import KnowledgeChunk, KnowledgeDocument
from backend.knowledge.schemas import ChunkPayload

pytestmark = pytest.mark.integration


async def make_document(tenant_id: str | None, kb_id: str) -> str:
    document_id = f"kdoc_test_{uuid.uuid4().hex[:10]}"
    async with get_pg_sessionmaker()() as session:
        session.add(
            KnowledgeDocument(
                id=document_id, tenant_id=tenant_id, kb_id=kb_id,
                file_name="t.txt", content_hash=uuid.uuid4().hex, status="ready",
            )
        )
        await session.commit()
    return document_id


def payloads_for(embedder, tenant_id, kb_id, document_id, texts):
    out = []
    for index, text in enumerate(texts):
        out.append(
            ChunkPayload(
                tenant_id=tenant_id, kb_id=kb_id, document_id=document_id,
                chunk_index=index, content=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                embedding=embedder._embed(text),
                embedding_model=embedder.model, embedding_dimension=embedder.dimension,
                meta={"file_name": "t.txt"},
            )
        )
    return out


@pytest.fixture()
async def seeded(store, mock_embedder, pg_cleanup):
    tenant_a, tenant_b = "tn_test_a", "tn_test_b"
    kb_a, kb_b = f"kstest_{uuid.uuid4().hex[:8]}", f"kstest_{uuid.uuid4().hex[:8]}"
    doc_a = await make_document(tenant_a, kb_a)
    doc_b = await make_document(tenant_b, kb_b)
    pg_cleanup.extend([doc_a, doc_b])
    await store.upsert_chunks(
        payloads_for(mock_embedder, tenant_a, kb_a, doc_a,
                     ["the policy grace period is thirty days",
                      "claims must be submitted within ninety days"])
    )
    await store.upsert_chunks(
        payloads_for(mock_embedder, tenant_b, kb_b, doc_b,
                     ["tenant b secret pricing sheet"])
    )
    return tenant_a, kb_a, doc_a, tenant_b, kb_b, doc_b


class TestDenseSearch:
    async def test_finds_relevant_chunk(self, store, mock_embedder, seeded):
        tenant_a, kb_a, *_ = seeded
        results = await store.dense_search(
            tenant_id=tenant_a, kb_ids=[kb_a],
            query_embedding=mock_embedder._embed("policy grace period"), limit=5,
        )
        assert results and "thirty days" in results[0].text

    async def test_tenant_filter_blocks_cross_tenant_kb(self, store, mock_embedder, seeded):
        tenant_a, _, _, _, kb_b, _ = seeded
        results = await store.dense_search(
            tenant_id=tenant_a, kb_ids=[kb_b],
            query_embedding=mock_embedder._embed("secret pricing"), limit=5,
        )
        assert results == []  # kb_b rows belong to tenant_b — never visible


class TestKeywordSearch:
    async def test_websearch_match(self, store, seeded):
        tenant_a, kb_a, *_ = seeded
        results = await store.keyword_search(
            tenant_id=tenant_a, kb_ids=[kb_a], query="grace period", limit=5
        )
        assert results and results[0].keyword_score > 0

    async def test_or_fallback(self, store, seeded):
        tenant_a, kb_a, *_ = seeded
        results = await store.keyword_search(
            tenant_id=tenant_a, kb_ids=[kb_a],
            query="nonexistentterm grace", limit=5,
        )
        assert results  # AND finds nothing; OR fallback matches "grace"


class TestUpsertIdempotency:
    async def test_reupsert_updates_not_duplicates(self, store, mock_embedder, pg_cleanup):
        kb = f"kstest_{uuid.uuid4().hex[:8]}"
        doc = await make_document("tn_test_a", kb)
        pg_cleanup.append(doc)
        first = payloads_for(mock_embedder, "tn_test_a", kb, doc, ["version one"])
        await store.upsert_chunks(first)
        second = payloads_for(mock_embedder, "tn_test_a", kb, doc, ["version two"])
        await store.upsert_chunks(second)
        assert await store.count_chunks("tn_test_a", kb) == 1
        results = await store.keyword_search(
            tenant_id="tn_test_a", kb_ids=[kb], query="version", limit=5
        )
        assert "version two" in results[0].text

    async def test_dimension_mismatch_rejected(self, store):
        bad = ChunkPayload(
            tenant_id="t", kb_id="k", document_id="d", chunk_index=0,
            content="x", content_hash="h", embedding=[0.1, 0.2],
        )
        with pytest.raises(ValueError):
            await store.upsert_chunks([bad])


class TestDeletion:
    async def test_deleted_chunks_never_surface(self, store, mock_embedder, seeded):
        tenant_a, kb_a, doc_a, *_ = seeded
        await store.delete_document(tenant_a, doc_a)
        dense = await store.dense_search(
            tenant_id=tenant_a, kb_ids=[kb_a],
            query_embedding=mock_embedder._embed("grace period"), limit=5,
        )
        keyword = await store.keyword_search(
            tenant_id=tenant_a, kb_ids=[kb_a], query="grace period", limit=5
        )
        assert dense == [] and keyword == []
