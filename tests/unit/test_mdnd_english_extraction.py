"""English incident regressions and an explicit Hindi isolation contract."""
import json
import runpy
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as wfe
from shared.orchestration.extensions.zepto_mdnd.state import llm_extraction_enabled
from shared.orchestration.extensions.zepto_mdnd.slots import extract_mdnd_slots
from shared.providers.base import LLMResult


def definition():
    nodes, edges = runpy.run_path("zepto/setup/06_single_bots.py")["build_mdnd_workflow"]()
    return {"id": "english_mdnd", "version": 1, "name": "MDND", "nodes": nodes, "edges": edges}


@pytest.fixture
def collector(monkeypatch):
    d = definition()
    monkeypatch.setattr(wfe, "load_workflow_definition", lambda *a: d)
    async def memory(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer
    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", memory)
    return wfe.WorkflowEngine(), d


async def turn(collector, text, patch=None, *, understood=True, language="en-IN", signal="complaint", **extra):
    llm = AsyncMock()
    llm.generate.return_value = LLMResult(text=json.dumps({
        "patch": patch or {}, "evidence": {k: text for k in patch or {}},
        "understood": understood, **extra,
    }))
    result = await collector[0].handle_turn_detailed(
        session_id="test", tenant_id="t", bot_id="b", workflow_name="mdnd",
        user_text=text, language=language, signal=signal, llm=llm,
        context_values={"mdnd_deduction_amount": "500", "mdnd_deduction_date": "25 August",
                        "mdnd_order_last4": "9456"})
    return result, llm


@pytest.mark.parametrize("language,enabled", [("en-IN", True), ("en-US", True),
    ("hi-IN", False), ("hi", False), ("", False), ("ta-IN", False)])
def test_extractor_is_english_only(language, enabled):
    assert llm_extraction_enabled(definition(), language) is enabled


@pytest.mark.parametrize("text", [
    "I have delivered the order to correct customer but still the amount has been deducted under MDND.",
    "I have given the correct product to the customer. But still the amount has been deducted.",
    "I delivered the order to correct customer but still the amount work has been deducted. Under NBND.",
])
async def test_live_complaints_capture_recipient_and_advance(collector, text):
    await turn(collector, "Yes, I am delivery partner.", understood=False, signal="affirm")
    r, llm = await turn(collector, text, {"delivery_handoff": "customer"})
    assert r["slots"]["m_handover_recipient"] == "customer (direct)"
    assert r["slots"]["m_issue_description"] == text
    assert r["trace"][-1] == "n_ask_reached_called"
    assert r["slots"]["reached_location"] == "unknown"
    assert r["slots"]["customer_called"] == "unknown"
    assert llm.generate.await_count == 4


@pytest.mark.parametrize("at_entry", [True, False])
async def test_clear_complaint_without_delivery_facts_advances(collector, at_entry):
    if not at_entry:
        await turn(collector, "Yes, I am delivery partner.", understood=False, signal="affirm")
    r, _ = await turn(collector, "My amount has been deducted under MDND.")
    assert r["trace"][-1] == "n_ask_reached_called"
    assert r["slots"]["delivery_handoff"] == "unknown"


async def test_unclear_answer_stays_on_incident_question(collector):
    await turn(collector, "Yes, I am delivery partner.", understood=False, signal="affirm")
    r, _ = await turn(collector, "Blah um something", understood=False)
    assert r["trace"][-1] == "n_ask_issue_desc"
    assert "m_issue_description" not in r["slots"]


@pytest.mark.parametrize("text", ["Maine customer ko product diya phir bhi MDND mark hua hai",
    "मैंने customer को order दिया था फिर भी deduction हुआ है"])
async def test_hindi_never_calls_semantic_model(collector, text):
    r, llm = await turn(collector, text, language="hi-IN")
    llm.generate.assert_not_awaited()
    assert r["slots"]["m_handover_recipient"] == "customer (direct)"


async def test_english_hindi_switch_keeps_collected_recipient(collector):
    await turn(collector, "I delivered it to correct customer", {"delivery_handoff": "customer"})
    r, llm = await turn(collector, "हाँ मैं location पर गया था और customer को call किया था",
                        language="hi-IN", signal="affirm")
    llm.generate.assert_not_awaited()
    assert r["slots"]["m_handover_recipient"] == "customer (direct)"
    assert r["trace"][-1] == "n_ask_cx"


async def test_model_cannot_turn_unmentioned_location_into_no():
    text = "I have delivered the order to correct customer but still the amount has been deducted."
    llm = AsyncMock(generate=AsyncMock(return_value=LLMResult(text=json.dumps({
        "patch": {"delivery_handoff": "customer", "reached_location": "no"},
        "evidence": {"delivery_handoff": text, "reached_location": text}, "understood": True,
    }))))
    r = await extract_mdnd_slots(llm, text=text, slots={}, pending_variable="m_issue_description",
                                  pending_question="What happened?", language="en-IN")
    assert r.patch == {"delivery_handoff": "customer"}


@pytest.mark.parametrize("text,wrong_field,pending", [
    ("Yes, I reached the customer's location and called the customer before delivery.", "cx_support_called", ("reached_location", "customer_called")),
    ("Yes, CX support called me.", "customer_called", ("customer_called",)),
    ("I called the customer.", "cx_support_called", ("cx_support_called",)),
    ("I delivered the order to correct customer.", "reached_location", ()),
])
async def test_unrelated_positive_fact_is_not_inferred(text, wrong_field, pending):
    llm = AsyncMock(generate=AsyncMock(return_value=LLMResult(text=json.dumps({
        "patch": {wrong_field: "yes"}, "evidence": {wrong_field: text}, "understood": True,
    }))))
    r = await extract_mdnd_slots(llm, text=text, slots={}, pending_variable="m_issue_description",
                                 pending_question="What happened?", pending_fields=pending, language="en-IN")
    assert wrong_field not in r.patch


def test_patch_changes_no_existing_hindi_config():
    patch = runpy.run_path("zepto/setup/13_mdnd_english_extraction.py")["patched_nodes"]
    before = definition()["nodes"]
    for n in before:
        for key in ("semanticExtraction", "semanticExtractionLanguages", "acceptUnderstoodNarrative"):
            n.get("config", {}).pop(key, None)
    after = patch(before)
    assert patch(after) == after
    for old, new in zip(before, after):
        assert {k: v for k, v in new.items() if k != "config"} == {k: v for k, v in old.items() if k != "config"}
        assert all(new.get("config", {})[k] == v for k, v in old.get("config", {}).items())
