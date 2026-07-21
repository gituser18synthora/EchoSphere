"""RetrievalRequest kb_ids normalization: single / list / dupes / empty."""

import pytest
from pydantic import ValidationError

from shared.knowledge.schemas import RetrievalRequest


def test_single_string_becomes_list():
    request = RetrievalRequest(tenant_id="t1", kb_ids="kb-1", query="q")
    assert request.kb_ids == ["kb-1"]


def test_list_preserved_in_order():
    request = RetrievalRequest(tenant_id="t1", kb_ids=["kb-2", "kb-1"], query="q")
    assert request.kb_ids == ["kb-2", "kb-1"]


def test_duplicates_removed():
    request = RetrievalRequest(tenant_id="t1", kb_ids=["kb-1", "kb-2", "kb-1"], query="q")
    assert request.kb_ids == ["kb-1", "kb-2"]


def test_none_means_all_authorized():
    assert RetrievalRequest(tenant_id="t1", query="q").kb_ids is None


def test_empty_list_normalizes_to_none():
    assert RetrievalRequest(tenant_id="t1", kb_ids=[], query="q").kb_ids is None


def test_blank_entries_dropped():
    request = RetrievalRequest(tenant_id="t1", kb_ids=["", "  ", "kb-1"], query="q")
    assert request.kb_ids == ["kb-1"]


def test_empty_query_rejected():
    with pytest.raises(ValidationError):
        RetrievalRequest(tenant_id="t1", query="")


def test_top_k_bounds():
    with pytest.raises(ValidationError):
        RetrievalRequest(tenant_id="t1", query="q", top_k=0)
    with pytest.raises(ValidationError):
        RetrievalRequest(tenant_id="t1", query="q", top_k=51)
