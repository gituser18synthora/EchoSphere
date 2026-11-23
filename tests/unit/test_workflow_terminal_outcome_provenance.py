"""Only explicitly authored terminal annotations can later be retracted."""
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as wfe


def definition(*, terminal=True, pending=False):
    return {
        "id": "wf_terminal_annotations", "version": 1, "name": "Outcome annotations",
        "nodes": [
            {"id": "start", "kind": "start"},
            {"id": "fact", "kind": "message", "config": {
                "silent": True, "setSlots": {"technician_visited": "yes"},
            }},
            {"id": "outcome", "kind": "message", "config": {
                "silent": True, "terminalOutcome": terminal,
                "setSlots": {"outcome": "resolved", "disposition": "completed",
                             "": "invalid", "nested": {"invalid": True}},
            }},
            {"id": "pending", "kind": "intent", "config": {"prompt": "Anything else?"}},
            {"id": "end", "kind": "end", "config": {"text": "Thank you."}},
        ],
        "edges": [
            {"from": "start", "to": "fact"},
            {"from": "fact", "to": "outcome"},
            {"from": "outcome", "to": "pending" if pending else "end"},
            {"from": "pending", "to": "end", "label": "no"},
        ],
    }


async def invoke(source, *, inputs=None):
    graph = wfe.build_definition_graph(source, MemorySaver())
    result = await graph.ainvoke(inputs or {"user_text": ""},
                                {"configurable": {"thread_id": "terminal-test"}})
    return graph, result


@pytest.mark.parametrize("terminal,expected", [
    (False, []), (True, ["outcome", "disposition"]), ("true", []),
])
async def test_only_explicit_terminal_annotations_are_reported(terminal, expected):
    _, result = await invoke(definition(terminal=terminal))
    assert result["status"] == "done"
    assert result["terminal_set_slots"] == expected
    assert result["slots"] == {"technician_visited": "yes", "outcome": "resolved",
                               "disposition": "completed"}


async def test_an_annotation_does_not_become_terminal_while_input_is_pending():
    graph, result = await invoke(definition(pending=True))
    assert result["awaiting"] == "pending"
    assert result["terminal_set_slots"] == []
    result = await graph.ainvoke({"user_text": "no"},
                                 {"configurable": {"thread_id": "terminal-test"}})
    assert result["status"] == "done"
    assert result["terminal_set_slots"] == []


async def test_previous_terminal_writes_are_not_reported_on_a_later_turn():
    graph, result = await invoke(definition())
    assert result["terminal_set_slots"] == ["outcome", "disposition"]
    result = await graph.ainvoke({"user_text": "", "current_node": "pending",
                                 "awaiting": "pending"},
                                {"configurable": {"thread_id": "terminal-test"}})
    assert result["terminal_set_slots"] == []


async def test_engine_exports_current_terminal_keys_without_prior_or_factual_keys(monkeypatch):
    source = definition()
    monkeypatch.setattr(wfe, "load_workflow_definition", lambda *args: deepcopy(source))
    engine = wfe.WorkflowEngine()
    engine._checkpointer = MemorySaver()
    result = await engine.handle_turn_detailed(
        session_id="terminal-test", tenant_id="tn_test", bot_id="bot_test",
        workflow_name="outcome_annotations", user_text="",
    )
    assert result["done"]
    assert result["terminalSetSlots"] == ["outcome", "disposition"]
    assert result["slots"]["technician_visited"] == "yes"
