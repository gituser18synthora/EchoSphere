"""The configuration rollout works before and after the shared platform gate."""
import copy

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as we
from tenant.Wordline.setup.identifier_confirmation import ASKS, upgrade


def original():
    nodes = [{"id": "start", "kind": "start", "config": {}},
             {"id": "end", "kind": "end", "config": {}}]
    edges = [{"from": "start", "to": ASKS[0]}]
    for ask in ASKS:
        variable = "spoken_terminal_id_retry" if "retry" in ask else "spoken_terminal_id"
        nodes.extend([
            {"id": ask, "kind": "ask", "config": {"variable": variable, "question": "Terminal ID?",
                "entity": {"dataType": "text", "regexPattern": r"(\d{7,8})"}}},
            {"id": ask + "_lookup", "kind": "api", "config": {"connection": "lookup"}},
        ])
        edges.extend([{"id": ask + "_next", "from": ask, "to": ask + "_lookup"},
                      {"from": ask + "_lookup", "to": "end", "label": "success"}])
    return {"id": "wf_wordline_confirm", "version": 1, "name": "confirm", "nodes": nodes, "edges": edges}


def test_transform_is_idempotent_and_does_not_mutate_input():
    before = original()
    frozen = copy.deepcopy(before)
    after = upgrade(before, ["1132986A"])
    assert before == frozen and upgrade(after, ["1132986A"]) == after
    for ask in ASKS:
        assert next(e for e in after["edges"] if e.get("from") == ask)["to"] == ask + "__confirm"


@pytest.mark.parametrize("ask", ASKS)
async def test_explicit_gate_is_not_duplicated_by_new_runtime(monkeypatch, ask):
    doc = original()
    doc["edges"][0]["to"] = ask
    doc = upgrade(doc, ["1132986A", "3139511A"])
    monkeypatch.setattr(we, "load_workflow_definition", lambda *args: doc)
    wf = we.WorkflowEngine()
    saver = MemorySaver()
    async def memory():
        return saver
    monkeypatch.setattr(wf, "_get_checkpointer", memory)
    async def turn(text):
        return await wf.handle_turn_detailed(
            session_id=ask, tenant_id="tenant", bot_id="bot", workflow_name=doc["id"],
            user_text=text, language="en-IN", mock_tool_results={"lookup": {"ok": True}})
    result = await turn("1132986A")
    assert ask + "__confirm" in result["trace"]
    assert not result["awaitingIdentifierConfirmation"]
    rejected = await turn("no")
    assert "Reset" not in rejected["reply"] and "lookup" not in rejected["trace"]
    await turn("1132986A")
    result = await turn("yes, actually 3139511A")
    assert ask + "_lookup" not in result["trace"]
    assert "letter A" in result["reply"]
    final = await turn("yes")
    assert final["done"] and ask + "_lookup" in final["trace"]
    variable = "spoken_terminal_id_retry" if "retry" in ask else "spoken_terminal_id"
    assert final["slots"][variable] == "3139511A"
