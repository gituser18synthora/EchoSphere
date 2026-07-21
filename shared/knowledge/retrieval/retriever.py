"""Hybrid retriever.

Pipeline: normalize query → dense (pgvector HNSW cosine) + keyword (FTS)
→ weighted reciprocal-rank fusion → duplicate removal → optional rerank
→ confidence gate → context-window budgeting.

Ported from the KMRAG retriever, re-keyed to tenant_id + kb_id filtering and
decoupled from its chat/cache layers.
"""

import asyncio
import logging
import re
import time
import unicodedata

from shared.config import get_settings
from shared.knowledge.schemas import RetrievalRequest, RetrievalResult, SourceRef
from shared.knowledge.vector_store.base import VectorStore

logger = logging.getLogger(__name__)

_RRF_K = 60
# Cheap token budget so we never ship an unbounded context to the LLM.
_MAX_CONTEXT_TOKENS = 3000


def normalize_query(query: str) -> str:
    """Unicode-normalize, collapse whitespace, strip control characters."""
    query = unicodedata.normalize("NFKC", query)
    # Drop control characters but keep whitespace so word boundaries survive.
    query = "".join(
        ch for ch in query if ch.isspace() or unicodedata.category(ch)[0] != "C"
    )
    return re.sub(r"\s+", " ", query).strip()


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def dedupe_sources(sources: list[SourceRef]) -> list[SourceRef]:
    """Drop exact chunk repeats and near-identical text (same normalized prefix)."""
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    out: list[SourceRef] = []
    for src in sources:
        if src.chunk_id in seen_ids:
            continue
        fingerprint = re.sub(r"\s+", " ", src.text.lower())[:400]
        if fingerprint in seen_text:
            continue
        seen_ids.add(src.chunk_id)
        seen_text.add(fingerprint)
        out.append(src)
    return out


def fuse_weighted_rrf(
    dense: list[SourceRef],
    keyword: list[SourceRef],
    vector_weight: float,
    keyword_weight: float,
) -> list[SourceRef]:
    """Weighted reciprocal-rank fusion; preserves per-source sub-scores."""
    fused: dict[str, SourceRef] = {}
    scores: dict[str, float] = {}
    for rank, src in enumerate(dense):
        scores[src.chunk_id] = scores.get(src.chunk_id, 0.0) + vector_weight / (_RRF_K + rank + 1)
        fused.setdefault(src.chunk_id, src)
    for rank, src in enumerate(keyword):
        scores[src.chunk_id] = scores.get(src.chunk_id, 0.0) + keyword_weight / (_RRF_K + rank + 1)
        if src.chunk_id in fused:
            fused[src.chunk_id].keyword_score = src.keyword_score
        else:
            fused.setdefault(src.chunk_id, src)
    ordered = sorted(fused.values(), key=lambda s: (-scores[s.chunk_id], s.chunk_id))
    for src in ordered:
        src.score = scores[src.chunk_id]
    return ordered


def budget_context(sources: list[SourceRef], max_tokens: int = _MAX_CONTEXT_TOKENS) -> list[SourceRef]:
    """Keep top sources until the approximate token budget is exhausted."""
    out: list[SourceRef] = []
    used = 0
    for src in sources:
        cost = _approx_tokens(src.text)
        if out and used + cost > max_tokens:
            break
        out.append(src)
        used += cost
    return out


class HybridRetriever:
    def __init__(self, store: VectorStore, embedder) -> None:
        self._store = store
        self._embedder = embedder

    async def retrieve(
        self,
        request: RetrievalRequest,
        authorized_kb_ids: list[str],
    ) -> RetrievalResult:
        """Run hybrid retrieval over pre-authorized KB ids.

        `authorized_kb_ids` MUST already be validated against the control plane
        (ownership, status, soft-delete) by KnowledgeService — this class never
        touches authorization.
        """
        settings = get_settings()
        started = time.perf_counter()
        query = normalize_query(request.query)

        if not authorized_kb_ids or not query:
            return RetrievalResult(
                used_knowledge_base=False,
                answerable=False,
                confidence=0.0,
                query=query,
                kb_ids=[],
                sources=[],
                duration_ms=(time.perf_counter() - started) * 1000,
                skipped_reason="no_authorized_knowledge_bases" if not authorized_kb_ids else "empty_query",
            )

        candidate_k = request.candidate_k or settings.retrieval_candidate_k
        min_score = request.min_score if request.min_score is not None else settings.retrieval_min_score

        query_embedding = await self._embedder.embed_query(query)

        dense_task = self._store.dense_search(
            tenant_id=request.tenant_id,
            kb_ids=authorized_kb_ids,
            query_embedding=query_embedding,
            limit=candidate_k,
            include_global=request.include_global,
        )
        keyword_task = self._store.keyword_search(
            tenant_id=request.tenant_id,
            kb_ids=authorized_kb_ids,
            query=query,
            limit=candidate_k,
            include_global=request.include_global,
        )
        dense, keyword = await asyncio.gather(dense_task, keyword_task)

        fused = fuse_weighted_rrf(
            dense,
            keyword,
            settings.retrieval_hybrid_vector_weight,
            settings.retrieval_hybrid_keyword_weight,
        )
        fused = dedupe_sources(fused)

        # Relevance gate on the raw cosine similarity (survives fusion):
        # a chunk passes if its vector similarity clears the threshold, or it
        # was a keyword hit strong enough to rank in the top candidates.
        gated = [
            s
            for s in fused
            if (s.vector_score is not None and s.vector_score >= min_score)
            or (s.keyword_score is not None and s.keyword_score > 0)
        ]

        if settings.retrieval_use_reranker and gated:
            gated = await self._maybe_rerank(query, gated[: request.rerank_k])

        top = budget_context(gated)[: request.top_k]

        confidence = max((s.vector_score or 0.0) for s in top) if top else 0.0
        answerable = bool(top) and confidence >= min_score

        return RetrievalResult(
            used_knowledge_base=True,
            answerable=answerable,
            confidence=round(confidence, 4),
            query=query,
            kb_ids=authorized_kb_ids,
            sources=top if answerable else [],
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            skipped_reason=None if answerable else "below_confidence_threshold",
        )

    async def _maybe_rerank(self, query: str, sources: list[SourceRef]) -> list[SourceRef]:
        """Optional cross-encoder rerank; fails open to the fused order."""
        try:
            from shared.knowledge.retrieval.reranker import rerank

            return await rerank(query, sources)
        except Exception as exc:  # noqa: BLE001 - reranker is best-effort
            logger.warning("Reranker unavailable, keeping fused order: %s", exc.__class__.__name__)
            return sources
