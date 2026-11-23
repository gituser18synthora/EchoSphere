"""A routing capture can coexist with contextual facts at one intent hub."""
import asyncio
from copy import deepcopy

import pytest

from tests.golden.harness import patched


def definition(*, scoped=True):
    config = {"prompt": "Is any work pending?", "alsoCapture": [
        {"variable": "installation_status", "overwrite": True,
         "entity": {"dataType": "text", "matchCanonicalValues": False,
                    "synonymPatterns": {"done": [r"installation done"]}}},
        {"variable": "route", "overwrite": True,
         "entity": {"dataType": "text", "matchCanonicalValues": False,
                    "synonymPatterns": {"declined": [r"no service needed"]}}},
    ]}
    if scoped:
        config["correctionCaptureVariable"] = "route"
    nodes = [{"id": "start", "kind": "start", "config": {}},
             {"id": "hub", "kind": "intent", "config": config}]
    nodes += [{"id": name, "kind": "end", "config": {"text": name}}
              for name in ("warranty", "decline", "unknown")]
    return {"id": "correction-scope", "name": "Scoped correction", "version": 1,
            "nodes": nodes, "edges": [
                {"id": "entry", "from": "start", "to": "hub"},
                {"id": "warranty", "from": "hub", "to": "warranty", "label": "warranty dispute"},
                {"id": "correction", "from": "hub", "to": "decline", "label": "correction"},
                {"id": "fallback", "from": "hub", "to": "unknown", "label": "else"},
            ]}


async def replay(text, *, slots=None, audit=None, scoped=True, signal=None):
    from langgraph.checkpoint.memory import MemorySaver
    from shared.orchestration.workflow_engine import build_definition_graph

    source = definition(scoped=scoped)
    with patched(source, []) as (_, executor):
        graph = build_definition_graph(source, MemorySaver())
        result = await graph.ainvoke({
            "current_node": "hub", "awaiting": "hub", "user_text": text,
            "signal_override": signal, "slots": deepcopy(slots or {}),
            "audit": deepcopy(audit or []), "language": "en-IN",
        }, {"configurable": {"thread_id": "isolated-scope-test"}})
        assert not executor.calls
        return result


@pytest.mark.parametrize("slots", [{}, {"route": "declined"}])
def test_context_fact_cannot_hijack_a_selected_special_route(slots):
    result = asyncio.run(replay("installation done, warranty dispute", slots=slots))
    assert result["trace"] == ["hub", "warranty"]
    assert result["slots"]["installation_status"] == "done"


@pytest.mark.parametrize("signal", ["affirm", "clarify", None])
def test_fresh_designated_capture_routes_even_with_conflicting_signal(signal):
    result = asyncio.run(replay("no service needed", signal=signal))
    assert result["trace"] == ["hub", "decline"]
    assert result["slots"]["route"] == "declined"


def test_previous_turn_capture_audit_cannot_route_a_new_answer():
    result = asyncio.run(replay("warranty dispute", slots={"route": "declined"}, audit=[
        {"action": "also_updated", "node": "hub", "variable": "route"},
    ]))
    assert result["trace"] == ["hub", "warranty"]


def test_legacy_unscoped_corrections_keep_their_existing_contract():
    result = asyncio.run(replay("installation done, warranty dispute", scoped=False))
    assert result["trace"] == ["hub", "decline"]
    assert result["slots"]["installation_status"] == "done"
