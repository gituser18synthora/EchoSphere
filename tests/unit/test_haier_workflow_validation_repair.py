"""The stale completion shim is removed without changing executable paths."""
from __future__ import annotations

import asyncio
from copy import deepcopy

from langgraph.checkpoint.memory import MemorySaver
import pytest

from backend.routers.workflows import validate_definition
from haier.setup.haier_call_audit_repair import BOT_ID, WORKFLOW_ID
from haier.setup.haier_workflow_validation_repair import (
    EDGE_ID, ORPHAN_ID, TARGET_ID, apply_workflow_validation_repair,
)
from shared.orchestration.workflow_engine import build_definition_graph


def fixture():
    return {
        "id": WORKFLOW_ID, "bot_id": BOT_ID, "version": 95,
        "name": "Appointment booking journey", "status": "approved",
        "nodes": [
            {"id": "start", "kind": "start", "x": 50, "y": 60},
            {"id": "n_v2_completed_by_q", "kind": "intent", "config": {
                "prompt": "Who completed the work?", "elseIsAnswer": False,
            }},
            {"id": "clarify", "kind": "intent", "config": {
                "prompt": "Was it a Haier technician or another technician?", "elseIsAnswer": False,
            }},
            {"id": "haier", "kind": "message", "config": {
                "silent": True, "setSlots": {"outcome": "redispatch_completion"}, "terminalOutcome": True,
            }},
            {"id": "m_n_v2_end_completed_other", "kind": "message", "config": {
                "silent": True, "setSlots": {"outcome": "close_completed_other"}, "terminalOutcome": True,
            }},
            {"id": "unverified", "kind": "message", "config": {
                "silent": True, "setSlots": {"outcome": "unverified"}, "terminalOutcome": True,
            }},
            {"id": ORPHAN_ID, "kind": "message", "x": 1980, "y": 90, "config": {
                "silent": True, "setSlots": {"outcome": "close_completed"}, "terminalOutcome": True,
            }},
            {"id": TARGET_ID, "kind": "end", "config": {"text": "Request forwarded for closure."}},
            {"id": "haier_end", "kind": "end", "config": {"text": "Request forwarded for completion."}},
            {"id": "unverified_end", "kind": "end", "config": {"text": "The source is unconfirmed."}},
        ],
        "edges": [
            {"id": "start_q", "from": "start", "to": "n_v2_completed_by_q"},
            {"id": "haier_source", "from": "n_v2_completed_by_q", "to": "haier", "label": "Haier"},
            {"id": "other_source", "from": "n_v2_completed_by_q", "to": "m_n_v2_end_completed_other", "label": "local mechanic"},
            {"id": "unknown_source", "from": "n_v2_completed_by_q", "to": "clarify", "label": "not sure/pata nahi"},
            {"id": "other_answer", "from": "n_v2_completed_by_q", "to": "clarify", "label": "else"},
            {"id": "clarify_haier", "from": "clarify", "to": "haier", "label": "Haier"},
            {"id": "clarify_other", "from": "clarify", "to": "m_n_v2_end_completed_other", "label": "local mechanic"},
            {"id": "clarify_unknown", "from": "clarify", "to": "unverified", "label": "not sure/pata nahi"},
            {"id": "clarify_other_answer", "from": "clarify", "to": "unverified", "label": "else"},
            {"id": "other_end", "from": "m_n_v2_end_completed_other", "to": TARGET_ID},
            {"id": EDGE_ID, "from": ORPHAN_ID, "to": TARGET_ID},
            {"id": "haier_end", "from": "haier", "to": "haier_end"},
            {"id": "unverified_end", "from": "unverified", "to": "unverified_end"},
        ],
    }


def test_only_the_obsolete_node_and_its_edge_are_removed():
    before = fixture()
    snapshot = deepcopy(before)
    assert validate_definition(before["nodes"], before["edges"])[1] == [{
        "nodeId": ORPHAN_ID, "level": "warning",
        "message": "Not connected to the start node — this step never runs.",
    }]
    result = apply_workflow_validation_repair(before)
    assert before == snapshot
    assert result["nodes"] == [node for node in before["nodes"] if node["id"] != ORPHAN_ID]
    assert result["edges"] == [edge for edge in before["edges"] if edge["id"] != EDGE_ID]
    assert validate_definition(result["nodes"], result["edges"]) == ([], [])
    assert apply_workflow_validation_repair(result) == result
    result["nodes"][0]["x"] = 999
    assert before["nodes"][0]["x"] == 50


@pytest.mark.parametrize("hub,text,outcome,awaiting", [
    ("n_v2_completed_by_q", "Haier", "redispatch_completion", None),
    ("n_v2_completed_by_q", "local mechanic", "close_completed_other", None),
    ("n_v2_completed_by_q", "pata nahi", None, "clarify"),
    ("clarify", "pata nahi", "unverified", None),
])
def test_known_and_unknown_completion_paths_run_identically(hub, text, outcome, awaiting):
    async def run(definition):
        graph = build_definition_graph(definition, MemorySaver())
        return await graph.ainvoke({
            "current_node": hub, "awaiting": hub, "user_text": text,
            "slots": {}, "language": "hi-IN", "status": "collecting",
            "node_retries": {}, "audit": [], "context_values": {},
        }, {"configurable": {"thread_id": "completion-orphan-regression"}})

    before = fixture()
    old = asyncio.run(run(before))
    new = asyncio.run(run(apply_workflow_validation_repair(before)))
    for key in ("reply", "slots", "trace", "awaiting", "status", "end"):
        assert old.get(key) == new.get(key), key
    assert new["slots"].get("outcome") == outcome
    assert new.get("awaiting") == awaiting
    assert ORPHAN_ID not in new["trace"]


@pytest.mark.parametrize("change", [
    "wrong_bot", "wrong_workflow", "incoming", "config", "wrong_target",
    "extra_outgoing", "target_missing", "dangling_edge", "duplicate",
])
def test_changed_or_active_closure_contract_is_rejected(change):
    definition = fixture()
    node = next(node for node in definition["nodes"] if node["id"] == ORPHAN_ID)
    edge = next(edge for edge in definition["edges"] if edge["id"] == EDGE_ID)
    if change == "wrong_bot":
        definition["bot_id"] = "other_bot"
    elif change == "wrong_workflow":
        definition["id"] = "other_workflow"
    elif change == "incoming":
        definition["edges"].append({"id": "active_path", "from": "clarify", "to": ORPHAN_ID})
    elif change == "config":
        node["config"]["setSlots"]["outcome"] = "active_other_outcome"
    elif change == "wrong_target":
        edge["to"] = "haier_end"
    elif change == "extra_outgoing":
        definition["edges"].append({"id": "extra", "from": ORPHAN_ID, "to": "haier_end"})
    elif change == "target_missing":
        definition["nodes"] = [node for node in definition["nodes"] if node["id"] != TARGET_ID]
    elif change == "dangling_edge":
        definition["nodes"] = [node for node in definition["nodes"] if node["id"] != ORPHAN_ID]
    elif change == "duplicate":
        definition["nodes"].append(deepcopy(node))
    with pytest.raises(ValueError):
        apply_workflow_validation_repair(definition)
