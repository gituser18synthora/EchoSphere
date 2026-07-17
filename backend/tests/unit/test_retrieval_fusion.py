"""Hybrid-retrieval pure functions: RRF fusion, dedupe, budgeting, normalize."""

from backend.knowledge.retrieval.retriever import (
    budget_context,
    dedupe_sources,
    fuse_weighted_rrf,
    normalize_query,
)
from backend.knowledge.schemas import SourceRef


def ref(chunk_id: str, text: str = "text", vector: float | None = None,
        keyword: float | None = None) -> SourceRef:
    return SourceRef(
        kb_id="kb-1", document_id="d1", chunk_id=chunk_id, chunk_index=0,
        score=vector or keyword or 0.0, vector_score=vector, keyword_score=keyword,
        text=text,
    )


class TestNormalizeQuery:
    def test_whitespace_collapsed(self):
        assert normalize_query("  what   is\tthe  policy \n") == "what is the policy"

    def test_control_chars_stripped(self):
        assert normalize_query("hi\x00the\x1bre") == "hithere"

    def test_unicode_nfkc(self):
        assert normalize_query("ﬁne") == "fine"


class TestRRFFusion:
    def test_overlapping_chunk_ranks_first(self):
        dense = [ref("a", vector=0.9), ref("b", vector=0.8)]
        keyword = [ref("a", keyword=0.5), ref("c", keyword=0.4)]
        fused = fuse_weighted_rrf(dense, keyword, 0.6, 0.4)
        assert fused[0].chunk_id == "a"
        assert {s.chunk_id for s in fused} == {"a", "b", "c"}

    def test_sub_scores_preserved(self):
        fused = fuse_weighted_rrf([ref("a", vector=0.9)], [ref("a", keyword=0.5)], 0.6, 0.4)
        assert fused[0].vector_score == 0.9
        assert fused[0].keyword_score == 0.5

    def test_empty_inputs(self):
        assert fuse_weighted_rrf([], [], 0.6, 0.4) == []


class TestDedupe:
    def test_same_chunk_id_removed(self):
        out = dedupe_sources([ref("a", "x"), ref("a", "y")])
        assert len(out) == 1

    def test_near_identical_text_removed(self):
        out = dedupe_sources([ref("a", "The same   text here"), ref("b", "the same text here")])
        assert len(out) == 1

    def test_distinct_kept(self):
        out = dedupe_sources([ref("a", "first fact"), ref("b", "second fact")])
        assert len(out) == 2


class TestBudget:
    def test_budget_cuts_tail(self):
        sources = [ref(str(i), "word " * 400) for i in range(20)]
        kept = budget_context(sources, max_tokens=1500)
        assert 0 < len(kept) < 20

    def test_first_source_always_kept(self):
        huge = [ref("a", "word " * 100000)]
        assert len(budget_context(huge, max_tokens=10)) == 1
