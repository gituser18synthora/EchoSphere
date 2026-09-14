"""State-machine tests through the MDND semantic extraction boundary.

Model outputs are controlled here to test checkpointing, routing, retries and
corrections; test_mdnd_slots also exercises actual multilingual model calls.
"""

import json
import runpy
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as wfe
from shared.orchestration.mdnd_state import FIELDS
from shared.providers.base import LLMResult

CONTEXT = {"mdnd_deduction_amount": "400 rupees", "mdnd_order_last4": "9203",
           "mdnd_deduction_date": "4 August"}
ALL = {"customer_called": "yes", "reached_location": "yes",
       "delivery_handoff": "doorstep", "cx_support_called": "yes"}


@pytest.fixture
def collector(monkeypatch):
    config = runpy.run_path("zepto/setup/06_single_bots.py")
    nodes, edges = config["build_mdnd_workflow"]()
    # The per-turn LLM extractor is a separate opt-in on top of the state
    # guards (``semanticSlots``): these tests exercise the extractor path.
    for node in nodes:
        if node["id"] == "n_start":
            node["config"]["semanticExtraction"] = "llm"
    definition = {"id": "wf_semantic_mdnd", "name": "MDND", "version": 1,
                  "nodes": nodes, "edges": edges}
    monkeypatch.setattr(wfe, "load_workflow_definition", lambda *args: definition)

    async def memory(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer
    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", memory)
    return wfe.WorkflowEngine(), AsyncMock(), definition


async def turn(collector, text, patch=None, session="semantic", **extra):
    engine, llm, _ = collector
    failure = extra.pop("failure", False)
    kwargs = extra.pop("kwargs", {})
    llm.generate.side_effect = RuntimeError("provider unavailable") if failure else None
    llm.generate.return_value = LLMResult(text=json.dumps({
        "patch": patch or {}, "evidence": {key: text for key in patch or {}}, **extra,
    }, ensure_ascii=False))
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_x", bot_id="bot_x", workflow_name="mdnd",
        user_text=text, language="hi-IN", context_values=CONTEXT,
        llm=llm, **kwargs,
    )


async def test_all_four_in_entry_story_skip_every_enquiry(collector):
    result = await turn(collector, "Called customer, left it at the door, CX called.", ALL,
                        drop_location="at the door")
    assert result["trace"][-1] == "n_hub_verify"
    assert not any(node in result["spokenNodes"] for node in
                   ("n_ask_reached", "n_ask_called", "n_ask_handover", "n_ask_cx"))
    assert {key: result["slots"][key] for key in FIELDS} == ALL
    assert result["slots"]["m_drop_location"] == "at the door"


async def test_two_then_one_then_brief_answer_never_reask_handoff(collector):
    await turn(collector, "हाँ बोलिए")
    r = await turn(collector, "मैं वहाँ गया था, उसके door के बाहर वहीं रखा",
                   {"reached_location": "yes", "delivery_handoff": "doorstep"},
                   drop_location="door के बाहर")
    assert r["trace"][-1] == "n_ask_called"
    r = await turn(collector, "हाँ मैंने call किया था तभी उसने call पर बताया कहाँ रखना है",
                   {"customer_called": "yes"})
    assert r["trace"][-1] == "n_ask_cx"
    assert "n_ask_handover" not in r["spokenNodes"]
    r = await turn(collector, "नहीं", {"cx_support_called": "no"})
    assert r["trace"][-1] == "n_hub_verify"
    assert r["slots"]["customer_called"] == "yes"
    assert r["slots"]["cx_support_called"] == "no"


async def test_other_field_no_does_not_answer_current_question(collector):
    await turn(collector, "हाँ बोलिए")
    await turn(collector, "customer को call किया था", {"customer_called": "yes"})
    r = await turn(collector, "CX से कोई call नहीं आया", {"cx_support_called": "no"},
                   kwargs={"signal": "refusal"})
    assert r["trace"][-1] == "n_ask_reached"
    assert r["slots"]["reached_location"] == "unknown"
    assert r["slots"]["cx_support_called"] == "no"


async def test_negative_location_is_not_overwritten_by_handoff_regex(collector):
    await turn(collector, "हाँ बोलिए")
    r = await turn(collector, "I did not reach the location; customer said leave it at the door.",
                   {"reached_location": "no"})
    assert r["slots"]["reached_location"] == "no"
    assert r["slots"]["delivery_handoff"] == "unknown"
    assert r["trace"][-1] == "n_ask_called"


async def test_latest_correction_during_collection_and_summary(collector):
    await turn(collector, "हाँ बोलिए")
    await turn(collector, "I called and reached", {"customer_called": "yes", "reached_location": "yes"})
    r = await turn(collector, "Actually I did not call; left it at the door and CX called.",
                   {"customer_called": "no", "delivery_handoff": "doorstep", "cx_support_called": "yes"})
    assert r["trace"][-1] == "n_hub_verify"
    assert r["slots"]["customer_called"] == "no"
    r = await turn(collector, "नहीं, गार्ड को दिया था", {"delivery_handoff": "guard"})
    assert r["trace"][-1] == "n_hub_verify"
    assert "n_api" not in r["trace"]
    assert r["slots"]["delivery_handoff"] == "guard"
    assert "m_drop_location" not in r["slots"]
    assert "n_ask_guard_name_known" not in r["spokenNodes"]


async def test_retraction_only_reasks_named_field(collector):
    # A patch is accepted only with a quote that names the field (evidence
    # gate) — the utterance must really carry all four answers.
    await turn(collector, "customer ko call kiya tha, location par gaya tha, door par rakh diya, CX se call aaya", ALL)
    r = await turn(collector, "CX वाला गलत है", {"cx_support_called": "unknown"},
                   explicit_retractions=["cx_support_called"])
    assert r["trace"][-1] == "n_ask_cx"
    assert r["slots"]["cx_support_called"] == "unknown"
    assert all(r["slots"][key] == value for key, value in ALL.items() if key != "cx_support_called")


@pytest.mark.parametrize("failure", [False, True])
async def test_unclear_first_answer_or_provider_failure_stays_contextual(collector, failure):
    await turn(collector, "हाँ बोलिए")
    r = await turn(collector, "झम बड़...", failure=failure)
    assert r["trace"][-1] == "n_ask_issue_desc"
    assert "माफ़" in r["reply"] and "क्या हुआ था" in r["reply"]
    assert "details देख" not in r["reply"]
    assert r["offScript"] is False and r["responseMode"] == "fixed"
    assert all(r["slots"][key] == "unknown" for key in FIELDS)


async def test_interruption_rollback_restores_all_slots(collector):
    await turn(collector, "हाँ बोलिए")
    await turn(collector, "customer को call किया था", {"customer_called": "yes"})
    engine, _, _ = collector
    assert await engine.rollback_last_turn(session_id="semantic", workflow_name="mdnd")
    r = await turn(collector, "नहीं मैंने call नहीं किया", {"customer_called": "no"})
    assert r["slots"]["customer_called"] == "no"
    assert r["slots"]["reached_location"] == "unknown"


async def test_failure_keeps_existing_facts_and_does_not_burn_retry_budget(collector):
    await turn(collector, "I called and reached", {"customer_called": "yes", "reached_location": "yes"})
    for _ in range(4):
        r = await turn(collector, "unclear", failure=True)
        assert r["trace"][-1] == "n_ask_handover" and not r["done"]
        assert r["slots"]["customer_called"] == "yes" and r["slots"]["reached_location"] == "yes"
        assert "किसको" in r["reply"] and not r["offScript"]


async def test_extractor_not_called_for_unflagged_workflow(collector):
    _, llm, definition = collector
    next(node for node in definition["nodes"] if node["kind"] == "start")["config"] = {}
    await turn(collector, "हाँ बोलिए")
    llm.generate.assert_not_awaited()


def test_summary_fallback_names_the_recorded_relative_not_a_generic_member():
    """The deterministic confirmation speaks the recipient the partner named
    ("customer की माँ"), never a generic household member for a named relative."""
    from shared.orchestration.mdnd_state import summary_fallback
    slots = {"m_called_customer": "no (did not call)", "m_reached_location": "yes (reached the location)",
             "m_handover_recipient": "mother", "m_cx_support_call": "yes (received CX support call)"}
    hi, en = summary_fallback(slots, "hi-IN"), summary_fallback(slots, "en-IN")
    assert "customer की माँ को" in hi and "किसी member" not in hi
    assert "the customer's mother" in en and "family member" not in en
    slots["m_handover_recipient"] = "relative (other)"
    assert "customer के घर के किसी member" in summary_fallback(slots, "hi-IN")
    slots["m_handover_recipient"] = "guard / security"
    assert "order guard को सौंपा था" in summary_fallback(slots, "hi-IN")
