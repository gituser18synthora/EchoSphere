"""Hybrid retriever.

Pipeline: normalize query → dense (pgvector HNSW cosine) + keyword (FTS)
→ score fusion (normalized weighted-sum, or weighted RRF) → duplicate removal
→ relevance gate (vector similarity OR keyword rank) → optional rerank
→ context-window budgeting.

Ported from the KMRAG retriever, re-keyed to tenant_id + kb_id filtering and
decoupled from its chat/cache layers.

Scores:
- vector_score  raw cosine similarity in [0, 1]
- keyword_score raw ts_rank_cd (unbounded; saturated before fusion)
- score         fused score, normalized to [0, 1] for both fusion methods
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


def saturate_rank(rank: float, k: float = 1.0) -> float:
    """Map an unbounded ts_rank_cd to (0, 1) with BM25-style saturation."""
    return rank / (rank + k) if rank > 0 else 0.0


def _merge_by_chunk(dense: list[SourceRef], keyword: list[SourceRef]) -> dict[str, SourceRef]:
    """Union the two candidate lists, keeping both sub-scores per chunk."""
    merged: dict[str, SourceRef] = {}
    for src in dense:
        merged[src.chunk_id] = src
    for src in keyword:
        if src.chunk_id in merged:
            merged[src.chunk_id].keyword_score = src.keyword_score
        else:
            merged[src.chunk_id] = src
    return merged


def fuse_weighted(
    dense: list[SourceRef],
    keyword: list[SourceRef],
    semantic_weight: float,
    bm25_weight: float,
    bm25_saturation: float = 1.0,
) -> list[SourceRef]:
    """Normalized weighted score fusion.

    final = (semantic_weight * cosine_sim + bm25_weight * saturated_bm25)
            / (semantic_weight + bm25_weight)
    Both inputs are in [0, 1] after normalization, so the fused score is too.
    """
    merged = _merge_by_chunk(dense, keyword)
    total = (semantic_weight + bm25_weight) or 1.0
    for src in merged.values():
        vec_norm = min(1.0, max(0.0, src.vector_score or 0.0))
        kw_norm = saturate_rank(src.keyword_score or 0.0, bm25_saturation)
        src.score = (semantic_weight * vec_norm + bm25_weight * kw_norm) / total
    return sorted(merged.values(), key=lambda s: (-s.score, s.chunk_id))


def fuse_weighted_rrf(
    dense: list[SourceRef],
    keyword: list[SourceRef],
    vector_weight: float,
    keyword_weight: float,
) -> list[SourceRef]:
    """Weighted reciprocal-rank fusion; preserves per-source sub-scores.

    Scores are normalized by the theoretical maximum (rank 0 in both lists)
    so the fused score lands in [0, 1] like the weighted method.
    """
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
    max_possible = ((vector_weight + keyword_weight) or 1.0) / (_RRF_K + 1)
    ordered = sorted(fused.values(), key=lambda s: (-scores[s.chunk_id], s.chunk_id))
    for src in ordered:
        src.score = min(1.0, scores[src.chunk_id] / max_possible)
    return ordered


def apply_phrase_boost(sources: list[SourceRef], query: str, boost: float) -> list[SourceRef]:
    """Bump chunks that contain the whole query as a phrase (exact terms,
    codes, names). Only applied to multi-token queries — single words already
    rank fine through BM25."""
    if boost <= 0 or len(query.split()) < 2:
        return sources
    needle = re.sub(r"\s+", " ", query.casefold())
    for src in sources:
        haystack = re.sub(r"\s+", " ", src.text.casefold())
        if needle in haystack:
            src.score = min(1.0, src.score + boost)
    return sorted(sources, key=lambda s: (-s.score, s.chunk_id))


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
        timings: dict[str, float] = {}
        query = normalize_query(request.query)

        if not authorized_kb_ids or not query:
            reason = "no_authorized_knowledge_bases" if not authorized_kb_ids else "empty_query"
            return self._empty_result(query, started, reason, timings)

        candidate_k = request.candidate_k or settings.retrieval_candidate_k
        min_score = request.min_score if request.min_score is not None else settings.retrieval_min_score
        min_keyword_rank = settings.retrieval_min_keyword_rank

        # Embedding failures (provider outage, dimension drift) degrade to
        # keyword-only search instead of failing the whole request.
        t0 = time.perf_counter()
        query_embedding: list[float] | None = None
        embed_error: str | None = None
        try:
            query_embedding = await self._embedder.embed_query(query)
            self._record_query_embedding_usage(request)
        except Exception as exc:  # noqa: BLE001 - fail open to keyword search
            embed_error = exc.__class__.__name__
            logger.warning("knowledge.retrieve embed failed (%s) — keyword-only", embed_error)
        timings["embed"] = self._ms(t0)

        dense: list[SourceRef] = []
        keyword: list[SourceRef] = []

        async def run_dense() -> None:
            nonlocal dense
            if query_embedding is None:
                return
            t = time.perf_counter()
            dense = await self._store.dense_search(
                tenant_id=request.tenant_id,
                kb_ids=authorized_kb_ids,
                query_embedding=query_embedding,
                limit=candidate_k,
                include_global=request.include_global,
            )
            timings["dense"] = self._ms(t)

        async def run_keyword() -> None:
            nonlocal keyword
            t = time.perf_counter()
            keyword = await self._store.keyword_search(
                tenant_id=request.tenant_id,
                kb_ids=authorized_kb_ids,
                query=query,
                limit=candidate_k,
                include_global=request.include_global,
            )
            timings["keyword"] = self._ms(t)

        await asyncio.gather(run_dense(), run_keyword())

        t0 = time.perf_counter()
        if settings.retrieval_fusion_method == "rrf":
            fused = fuse_weighted_rrf(
                dense,
                keyword,
                settings.retrieval_hybrid_vector_weight,
                settings.retrieval_hybrid_keyword_weight,
            )
        else:
            fused = fuse_weighted(
                dense,
                keyword,
                settings.retrieval_semantic_weight,
                settings.retrieval_bm25_weight,
                settings.retrieval_bm25_saturation,
            )
        merged_count = len(fused)
        fused = dedupe_sources(fused)
        deduped_count = len(fused)
        fused = apply_phrase_boost(fused, query, settings.retrieval_phrase_boost)
        timings["fuse"] = self._ms(t0)

        # Relevance gate: a chunk is relevant if its vector similarity clears
        # the threshold OR its keyword rank clears the (raw ts_rank_cd) floor.
        # Keyword hits must survive on their own — exact terms, codes and
        # names often have weak embedding similarity, which is precisely when
        # lexical search matters.
        for src in fused:
            src.passed_gate = (
                (src.vector_score is not None and src.vector_score >= min_score)
                or (src.keyword_score is not None and src.keyword_score >= min_keyword_rank)
            )
        gated = [s for s in fused if s.passed_gate]

        reranked_count = 0
        if settings.retrieval_use_reranker and gated:
            t0 = time.perf_counter()
            gated = await self._maybe_rerank(query, gated[: request.rerank_k])
            reranked_count = len(gated)
            timings["rerank"] = self._ms(t0)

        top = budget_context(gated)[: request.top_k]
        answerable = bool(top)
        confidence = max(s.score for s in top) if top else 0.0

        if answerable:
            skipped_reason = None
        elif merged_count == 0:
            skipped_reason = "no_matching_chunks"
        else:
            skipped_reason = "below_confidence_threshold"

        sources = top
        if not answerable and request.include_below_threshold:
            # Test console: surface the best near-misses so empty results are
            # explainable (each carries passed_gate=False).
            sources = fused[: request.top_k]
        elif not answerable:
            sources = []
        for i, src in enumerate(sources):
            src.rank = i + 1

        diagnostics = {
            "kbCount": len(authorized_kb_ids),
            "queryLength": len(query),
            "embedder": getattr(self._embedder, "model", "unknown"),
            "embedError": embed_error,
            "fusionMethod": settings.retrieval_fusion_method,
            "semanticWeight": settings.retrieval_semantic_weight,
            "bm25Weight": settings.retrieval_bm25_weight,
            "minScore": min_score,
            "minKeywordRank": min_keyword_rank,
            "denseCandidates": len(dense),
            "keywordCandidates": len(keyword),
            "mergedCandidates": merged_count,
            "afterDedupe": deduped_count,
            "afterGate": len(gated),
            "reranked": reranked_count,
            "returned": len(sources),
            "timingsMs": {k: round(v, 2) for k, v in timings.items()},
            "zeroResultReason": skipped_reason,
        }

        return RetrievalResult(
            used_knowledge_base=True,
            answerable=answerable,
            confidence=round(confidence, 4),
            query=query,
            kb_ids=authorized_kb_ids,
            sources=sources,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            skipped_reason=skipped_reason,
            diagnostics=diagnostics,
        )

    def _record_query_embedding_usage(self, request: RetrievalRequest) -> None:
        """Bill the query embedding to the searching tenant, off the hot path.

        Every search is one real provider call (no request-id dedupe needed);
        embedders that report no usage (mock) are never recorded. Failures
        only log — metering must not break retrieval.
        """
        tokens = int(getattr(self._embedder, "last_usage_tokens", 0) or 0)
        requests = int(getattr(self._embedder, "last_usage_requests", 0) or 0)
        if (not tokens and not requests) or not request.tenant_id:
            return

        def _write() -> None:
            from shared.billing.metering import record_usage_event
            from shared.db.mysql import get_sessionmaker

            session = get_sessionmaker()()
            try:
                record_usage_event(
                    session,
                    tenant_id=request.tenant_id,
                    bot_id=request.bot_id,
                    capability="embedding",
                    provider_code=getattr(self._embedder, "provider_code", "openai"),
                    model_code=getattr(self._embedder, "model", ""),
                    requests=requests or 1,
                    total_tokens=tokens,
                    usage_source=getattr(self._embedder, "last_usage_source", "provider"),
                    usage_metadata={"kind": "query"},
                )
            except Exception:  # noqa: BLE001
                logger.warning("query embedding usage recording failed")
                session.rollback()
            finally:
                session.close()

        try:
            asyncio.get_running_loop()
            asyncio.ensure_future(asyncio.to_thread(_write))
        except RuntimeError:
            _write()

    @staticmethod
    def _ms(since: float) -> float:
        return (time.perf_counter() - since) * 1000

    @staticmethod
    def _empty_result(
        query: str, started: float, reason: str, timings: dict[str, float]
    ) -> RetrievalResult:
        return RetrievalResult(
            used_knowledge_base=False,
            answerable=False,
            confidence=0.0,
            query=query,
            kb_ids=[],
            sources=[],
            duration_ms=(time.perf_counter() - started) * 1000,
            skipped_reason=reason,
            diagnostics={
                "kbCount": 0,
                "queryLength": len(query),
                "denseCandidates": 0,
                "keywordCandidates": 0,
                "mergedCandidates": 0,
                "returned": 0,
                "timingsMs": {k: round(v, 2) for k, v in timings.items()},
                "zeroResultReason": reason,
            },
        )

    async def _maybe_rerank(self, query: str, sources: list[SourceRef]) -> list[SourceRef]:
        """Optional cross-encoder rerank; fails open to the fused order."""
        try:
            from shared.knowledge.retrieval.reranker import rerank

            return await rerank(query, sources)
        except Exception as exc:  # noqa: BLE001 - reranker is best-effort
            logger.warning("Reranker unavailable, keeping fused order: %s", exc.__class__.__name__)
            return sources
