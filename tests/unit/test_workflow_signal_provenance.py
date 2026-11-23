"""A validated null signal is a decision, not a missing classification."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as wfe


LONG_DECLINE = (
    "नहीं, अब तो मुझे करानी ही नहीं है क्योंकि। 2 महीने हो चुके हैं इस बात को, "
    "कभी कोई फ़ोन करता है, कभी कोई फ़ोन करता है, आने को कोई तैयार ही नहीं होता।"
)
IDENTITY = dict(session_id="signal-test", tenant_id="tn_test", bot_id="bot_test",
                workflow_name="signal_test")
THREAD = {"configurable": {"thread_id": "signal-test"}}


def definition():
    return {
        "id": "wf_signal_test", "version": 1, "name": "Signal test",
        "nodes": [
            {"id": "start", "kind": "start"},
            {"id": "hub", "kind": "intent", "config": {
                "prompt": "May I proceed?", "elseIsAnswer": True,
            }},
            {"id": "accepted", "kind": "end", "config": {"text": "Accepted."}},
            {"id": "unclassified", "kind": "end", "config": {"text": "No decision."}},
        ],
        "edges": [
            {"from": "start", "to": "hub"},
            {"from": "hub", "to": "accepted", "label": "yes"},
            {"from": "hub", "to": "unclassified", "label": "else"},
        ],
    }


@pytest.fixture
def engine(monkeypatch):
    instance = wfe.WorkflowEngine()
    instance._checkpointer = MemorySaver()
    monkeypatch.setattr(wfe, "load_workflow_definition", lambda *args: definition())
    return instance


async def direct(graph, text, **inputs):
    return await graph.ainvoke({
        "current_node": "hub", "awaiting": "hub", "user_text": text, **inputs,
    }, THREAD)


@pytest.mark.parametrize("inputs", [{}, {"signal_override": None},
                                     {"signal_override": ""},
                                     {"signal_authoritative": False}])
async def test_legacy_missing_or_null_signal_keeps_regex_fallback(inputs):
    graph = wfe.build_definition_graph(definition(), MemorySaver())
    result = await direct(graph, LONG_DECLINE, **inputs)
    assert result["signal"] == "payment_intent"
    assert result["trace"] == ["hub", "accepted"]


@pytest.mark.parametrize("signal", [None, ""])
async def test_authoritative_null_does_not_invent_payment_intent(signal):
    graph = wfe.build_definition_graph(definition(), MemorySaver())
    result = await direct(graph, LONG_DECLINE,
                          signal_override=signal, signal_authoritative=True)
    assert result["signal"] is None
    assert result["trace"] == ["hub", "unclassified"]
    assert result["signal_override"] is None
    assert result["signal_authoritative"] is False


@pytest.mark.parametrize("authoritative", [False, True])
async def test_supplied_nonnull_signal_keeps_priority(authoritative):
    graph = wfe.build_definition_graph(definition(), MemorySaver())
    result = await direct(graph, LONG_DECLINE,
                          signal_override="affirm", signal_authoritative=authoritative)
    assert result["signal"] == "affirm"
    assert result["trace"] == ["hub", "accepted"]


async def test_authoritative_null_still_allows_authored_literal_answers():
    graph = wfe.build_definition_graph(definition(), MemorySaver())
    result = await direct(graph, "yes", signal_override=None, signal_authoritative=True)
    assert result["signal"] is None
    assert result["trace"] == ["hub", "accepted"]


async def test_authority_and_override_do_not_survive_a_checkpoint():
    graph = wfe.build_definition_graph(definition(), MemorySaver())
    await direct(graph, LONG_DECLINE, signal_override=None, signal_authoritative=True)
    next_turn = await direct(graph, LONG_DECLINE)
    assert next_turn["signal"] == "payment_intent"
    assert next_turn["trace"] == ["hub", "accepted"]
    await direct(graph, LONG_DECLINE, signal_override="affirm", signal_authoritative=True)
    next_turn = await direct(graph, LONG_DECLINE)
    assert next_turn["signal"] == "payment_intent"


async def test_output_signal_is_never_reused_as_an_input():
    graph = wfe.build_definition_graph(definition(), MemorySaver())
    result = await direct(graph, LONG_DECLINE, signal="affirm")
    assert result["signal"] == "payment_intent"


async def test_empty_turn_has_no_signal_even_with_authoritative_override():
    graph = wfe.build_definition_graph(definition(), MemorySaver())
    result = await direct(graph, "", signal_override="affirm", signal_authoritative=True)
    assert result["signal"] is None
    assert result["awaiting"] == "hub"
    assert result["signal_authoritative"] is False


@pytest.mark.parametrize("authoritative,expected", [(False, "payment_intent"), (True, None)])
async def test_public_engine_contract(engine, authoritative, expected):
    await engine.handle_turn_detailed(user_text="", **IDENTITY)
    result = await engine.handle_turn_detailed(
        user_text=LONG_DECLINE, signal=None, signal_authoritative=authoritative, **IDENTITY,
    )
    assert result["signal"] == expected
    assert result["trace"] == ["hub", "unclassified" if authoritative else "accepted"]


async def test_simple_engine_wrapper_passes_signal_authority(engine):
    await engine.handle_turn(user_text="", **IDENTITY)
    reply, done = await engine.handle_turn(
        user_text=LONG_DECLINE, signal=None, signal_authoritative=True, **IDENTITY,
    )
    assert done and reply == "No decision."


async def test_public_engine_overwrites_any_checkpointed_input_authority(engine):
    await engine.handle_turn_detailed(user_text="", **IDENTITY)
    graph = await engine._get_definition_graph(definition())
    thread = {"configurable": {"thread_id": "signal-test:signal_test"}}
    await graph.aupdate_state(thread, {"signal_authoritative": True, "signal_override": "affirm"})
    result = await engine.handle_turn_detailed(user_text=LONG_DECLINE, **IDENTITY)
    assert result["signal"] == "payment_intent"


async def test_readback_early_return_clears_input_authority(monkeypatch):
    graph = wfe.build_definition_graph(definition(), MemorySaver())
    monkeypatch.setattr(wfe, "configured_readback", lambda *args: ("phone", "1 2 3"))
    result = await direct(graph, "Repeat my number", signal_override=None,
                          signal_authoritative=True)
    assert result["signal"] == "question"
    assert result["reply"] == "1 2 3"
    assert result["signal_authoritative"] is False
    assert result["signal_override"] is None


async def test_extraction_started_ahead_preserves_authoritative_null(engine, monkeypatch):
    class Provider:
        key = "test"
        extract = AsyncMock(return_value={"patch": {}})

        def llm_extraction_enabled(self, *args):
            return True

        def canonical_slots(self, slots):
            return deepcopy(slots)

        def merge_extraction(self, *args):
            return set()

        def semantic_node(self, node, *args):
            return node

        def is_summary_hub(self, node):
            return False

    provider = Provider()
    monkeypatch.setattr(wfe._extensions, "semantic_provider_for", lambda _: provider)
    llm = object()
    await engine.handle_turn_detailed(user_text="", **IDENTITY)
    prepared = await engine.start_extraction(user_text=LONG_DECLINE, llm=llm, **IDENTITY)
    assert prepared is not None
    ahead = asyncio.get_running_loop().create_future()
    ahead.set_result(prepared)
    result = await engine.handle_turn_detailed(
        user_text=LONG_DECLINE, signal=None, signal_authoritative=True,
        extraction_ahead=ahead, llm=llm, **IDENTITY,
    )
    assert result["extractionAhead"] == "used"
    assert result["signal"] is None
    assert result["trace"] == ["hub", "unclassified"]
    assert provider.extract.await_count == 1
