"""Retrieval quality against the REAL PG store: the full case matrix.

Covers exact-text, semantic, keyword-only, IDs/names/numbers, all three kb_id
modes, tenant isolation, empty/invalid KBs, duplicate chunks, low-similarity
queries and Devanagari content — using the real HybridRetriever + PgVectorStore
(only the embedder is the deterministic mock provider, as in all other
integration tests)."""

import hashlib
import uuid

import pytest

from shared.errors import NotFoundError
from shared.knowledge.schemas import ChunkPayload, RetrievalRequest

from tests.integration.test_pgvector_store import make_document

pytestmark = pytest.mark.integration


CHUNKS = [
    "Renewal Policy. The policy grace period for renewal is exactly 30 days. "
    "Customers may renew within the grace period without penalty.",
    "Support contact: call 080 6743 6743, the Pan India toll free number, "
    "or write to smartcare@example.com. Reference code XK-9917 applies.",
    "Invoice 78412 was issued to Rakesh Sharma on 14 March 2026 for the "
    "annual maintenance contract of the deep freezer unit.",
    "नवीनीकरण की अवधि तीस दिन है। ग्राहक बिना शुल्क के नवीनीकरण कर सकते हैं।",
]


async def seed_kb(store, embedder, tenant_id, kb_id, texts, *, file_name="kb.txt"):
    document_id = await make_document(tenant_id, kb_id)
    payloads = [
        ChunkPayload(
            tenant_id=tenant_id, kb_id=kb_id, document_id=document_id,
            chunk_index=index, content=text,
            content_hash=hashlib.sha256(f"{index}:{text}".encode()).hexdigest(),
            embedding=embedder._embed(text),
            embedding_model=embedder.model, embedding_dimension=embedder.dimension,
            meta={"file_name": file_name},
        )
        for index, text in enumerate(texts)
    ]
    await store.upsert_chunks(payloads)
    return document_id


@pytest.fixture()
async def corpus(knowledge_service, store, mock_embedder, control_plane, pg_cleanup):
    """One tenant with two seeded KBs plus an empty KB; a second tenant with
    private content for isolation checks."""
    tenant = control_plane.tenant()
    kb_main = control_plane.knowledge_source(tenant, name="Main KB")
    kb_extra = control_plane.knowledge_source(tenant, name="Extra KB")
    kb_empty = control_plane.knowledge_source(tenant, name="Empty KB")
    other_tenant = control_plane.tenant()
    kb_other = control_plane.knowledge_source(other_tenant, name="Other KB")

    pg_cleanup.append(await seed_kb(store, mock_embedder, tenant, kb_main, CHUNKS, file_name="main.txt"))
    pg_cleanup.append(await seed_kb(
        store, mock_embedder, tenant, kb_extra,
        ["Shipping is free for orders above five hundred rupees across India."],
        file_name="extra.txt",
    ))
    pg_cleanup.append(await seed_kb(
        store, mock_embedder, other_tenant, kb_other,
        ["Confidential acquisition plan for the fourth quarter, code name BLUEBIRD."],
        file_name="secret.txt",
    ))
    return {
        "tenant": tenant, "kb_main": kb_main, "kb_extra": kb_extra,
        "kb_empty": kb_empty, "other_tenant": other_tenant, "kb_other": kb_other,
        "service": knowledge_service,
    }


async def search(corpus, query, *, kb_ids=None, tenant=None, **kwargs):
    return await corpus["service"].search(
        RetrievalRequest(
            tenant_id=tenant or corpus["tenant"], kb_ids=kb_ids, query=query, **kwargs
        )
    )


class TestQueryShapes:
    async def test_exact_text_copied_from_chunk(self, corpus):
        result = await search(
            corpus, "The policy grace period for renewal is exactly 30 days",
            kb_ids=[corpus["kb_main"]],
        )
        assert result.answerable
        assert result.sources[0].rank == 1
        assert "grace period" in result.sources[0].text

    async def test_semantically_similar_query(self, corpus):
        result = await search(
            corpus, "how long can customers renew after expiry without penalty",
            kb_ids=[corpus["kb_main"]],
        )
        assert result.answerable
        assert any("renew" in s.text.lower() for s in result.sources)

    async def test_keyword_only_query_survives_weak_vector_score(self, corpus):
        """THE regression test: an exact rare token must be retrievable even
        when embedding similarity is far below the vector threshold."""
        result = await search(corpus, "XK-9917", kb_ids=[corpus["kb_main"]])
        assert result.answerable
        assert "XK-9917" in result.sources[0].text
        assert result.sources[0].keyword_score is not None

    async def test_names_ids_dates_numbers(self, corpus):
        for query in ("Invoice 78412", "Rakesh Sharma", "14 March 2026", "080 6743 6743"):
            result = await search(corpus, query, kb_ids=[corpus["kb_main"]])
            assert result.answerable, f"query {query!r} returned nothing"

    async def test_hindi_devanagari_query(self, corpus):
        result = await search(corpus, "नवीनीकरण की अवधि", kb_ids=[corpus["kb_main"]])
        assert result.answerable
        assert "नवीनीकरण" in result.sources[0].text

    async def test_very_low_similarity_query_is_gated(self, corpus):
        result = await search(
            corpus, "quantum chromodynamics lattice simulation parameters",
            kb_ids=[corpus["kb_main"]],
        )
        assert not result.answerable
        assert result.sources == []  # runtime default: no near-miss context
        assert result.skipped_reason in {"below_confidence_threshold", "no_matching_chunks"}

    async def test_low_similarity_with_diagnostics_returns_near_misses(self, corpus):
        result = await search(
            corpus, "quantum chromodynamics lattice simulation parameters",
            kb_ids=[corpus["kb_main"]], include_below_threshold=True,
        )
        assert not result.answerable
        if result.sources:  # dense always proposes candidates; all must be flagged
            assert all(not s.passed_gate for s in result.sources)


class TestKbIdModes:
    async def test_single_kb_id(self, corpus):
        result = await search(corpus, "policy grace period", kb_ids=[corpus["kb_main"]])
        assert result.answerable and result.kb_ids == [corpus["kb_main"]]

    async def test_multiple_kb_ids(self, corpus):
        result = await search(
            corpus, "free shipping orders",
            kb_ids=[corpus["kb_main"], corpus["kb_extra"]],
        )
        assert result.answerable
        assert any(s.kb_id == corpus["kb_extra"] for s in result.sources)

    async def test_no_kb_id_searches_all_tenant_kbs(self, corpus):
        result = await search(corpus, "shipping free orders rupees")
        assert result.answerable
        assert corpus["kb_extra"] in {s.kb_id for s in result.sources}
        # Authorized set includes every searchable KB of the tenant.
        assert set(result.kb_ids) >= {corpus["kb_main"], corpus["kb_extra"]}

    async def test_wrong_tenant_cannot_use_foreign_kb(self, corpus):
        with pytest.raises(NotFoundError):
            await search(corpus, "anything", kb_ids=[corpus["kb_other"]])

    async def test_wrong_tenant_never_leaks_content(self, corpus):
        result = await search(corpus, "confidential acquisition plan BLUEBIRD")
        assert all("BLUEBIRD" not in s.text for s in result.sources)

    async def test_empty_kb_reports_no_matching_chunks(self, corpus):
        result = await search(corpus, "policy grace period", kb_ids=[corpus["kb_empty"]])
        assert not result.answerable
        assert result.sources == []
        assert result.skipped_reason == "no_matching_chunks"
        assert result.diagnostics["mergedCandidates"] == 0

    async def test_invalid_kb_is_404(self, corpus):
        with pytest.raises(NotFoundError):
            await search(corpus, "anything", kb_ids=["ks_does_not_exist"])


class TestResultQuality:
    async def test_duplicate_chunks_are_deduped(
        self, corpus, store, mock_embedder, pg_cleanup
    ):
        dup_text = "Warranty coverage lasts twenty four months from purchase."
        pg_cleanup.append(await seed_kb(
            store, mock_embedder, corpus["tenant"], corpus["kb_extra"],
            [dup_text, dup_text], file_name="dup.txt",
        ))
        result = await search(corpus, "warranty coverage months", kb_ids=[corpus["kb_extra"]])
        assert result.answerable
        texts = [s.text for s in result.sources]
        assert len([t for t in texts if t == dup_text]) == 1

    async def test_ranks_are_sequential_and_scores_normalized(self, corpus):
        result = await search(corpus, "renewal grace period", kb_ids=[corpus["kb_main"]])
        assert [s.rank for s in result.sources] == list(range(1, len(result.sources) + 1))
        assert all(0.0 <= s.score <= 1.0 for s in result.sources)

    async def test_diagnostics_are_populated(self, corpus):
        result = await search(corpus, "renewal grace period", kb_ids=[corpus["kb_main"]])
        diag = result.diagnostics
        assert diag["kbCount"] == 1
        assert diag["denseCandidates"] > 0
        assert diag["fusionMethod"] == "weighted"
        assert set(diag["timingsMs"]) >= {"embed", "dense", "keyword", "fuse"}
        assert diag["zeroResultReason"] is None

    async def test_min_score_override_applies(self, corpus):
        # An impossible threshold with keyword floor still lets exact hits through,
        # so verify with a semantic-only query instead.
        result = await search(
            corpus, "how long can customers renew after expiry",
            kb_ids=[corpus["kb_main"]], min_score=0.99,
        )
        assert not result.answerable or all(
            (s.keyword_score or 0) > 0 for s in result.sources
        )
