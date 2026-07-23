"""Hybrid-retrieval pure functions: fusion (weighted + RRF), dedupe, budgeting,
normalization, saturation and phrase boosting."""

import pytest

from shared.knowledge.retrieval.retriever import (
    apply_phrase_boost,
    budget_context,
    dedupe_sources,
    fuse_weighted,
    fuse_weighted_rrf,
    normalize_query,
    saturate_rank,
)
from shared.knowledge.schemas import SourceRef


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

    def test_scores_normalized_to_unit_range(self):
        dense = [ref("a", vector=0.9), ref("b", vector=0.8)]
        keyword = [ref("a", keyword=0.5)]
        fused = fuse_weighted_rrf(dense, keyword, 0.6, 0.4)
        assert all(0.0 <= s.score <= 1.0 for s in fused)
        # Rank 0 in both lists = the theoretical maximum → 1.0.
        assert fused[0].score == pytest.approx(1.0)


class TestSaturation:
    def test_zero_rank_is_zero(self):
        assert saturate_rank(0.0) == 0.0

    def test_monotonic_and_bounded(self):
        values = [saturate_rank(r) for r in (0.1, 0.5, 1.0, 6.0, 100.0)]
        assert values == sorted(values)
        assert all(0.0 < v < 1.0 for v in values)


class TestWeightedFusion:
    def test_final_score_is_weighted_normalized_sum(self):
        # vec 0.8, kw 1.0 saturated to 0.5 → (0.65*0.8 + 0.35*0.5) / 1.0 = 0.695
        fused = fuse_weighted([ref("a", vector=0.8)], [ref("a", keyword=1.0)], 0.65, 0.35)
        assert fused[0].score == pytest.approx(0.695)
        assert 0.0 <= fused[0].score <= 1.0

    def test_keyword_only_chunk_is_kept(self):
        fused = fuse_weighted([ref("a", vector=0.9)], [ref("b", keyword=2.0)], 0.65, 0.35)
        assert {s.chunk_id for s in fused} == {"a", "b"}
        b = next(s for s in fused if s.chunk_id == "b")
        assert b.score == pytest.approx(0.35 * (2.0 / 3.0))

    def test_ordering_by_fused_score(self):
        fused = fuse_weighted(
            [ref("weak", vector=0.2), ref("strong", vector=0.9)],
            [ref("weak", keyword=0.1)],
            0.65, 0.35,
        )
        assert fused[0].chunk_id == "strong"

    def test_sub_scores_preserved(self):
        fused = fuse_weighted([ref("a", vector=0.9)], [ref("a", keyword=0.5)], 0.65, 0.35)
        assert fused[0].vector_score == 0.9
        assert fused[0].keyword_score == 0.5

    def test_empty_inputs(self):
        assert fuse_weighted([], [], 0.65, 0.35) == []


class TestPhraseBoost:
    def test_containing_chunk_boosted_above_non_containing(self):
        contains = ref("a", "Contact us on 080 6743 6743 anytime", vector=0.3)
        contains.score = 0.3
        other = ref("b", "unrelated text about refunds", vector=0.35)
        other.score = 0.35
        boosted = apply_phrase_boost([other, contains], "080 6743 6743", 0.1)
        assert boosted[0].chunk_id == "a"
        assert boosted[0].score == pytest.approx(0.4)

    def test_single_word_query_not_boosted(self):
        src = ref("a", "renewal policy details", vector=0.3)
        src.score = 0.3
        assert apply_phrase_boost([src], "renewal", 0.1)[0].score == pytest.approx(0.3)

    def test_score_capped_at_one(self):
        src = ref("a", "the exact phrase here", vector=0.99)
        src.score = 0.99
        assert apply_phrase_boost([src], "exact phrase", 0.1)[0].score == 1.0

    def test_case_insensitive_match(self):
        src = ref("a", "PAN INDIA Toll Free NUMBER", vector=0.2)
        src.score = 0.2
        assert apply_phrase_boost([src], "pan india toll free number", 0.1)[0].score == pytest.approx(0.3)


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
