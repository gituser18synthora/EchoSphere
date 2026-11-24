"""Remove the reviewed obsolete Haier completion-outcome shim.

Source clarification now has separate Haier, other-technician, and unknown
paths.  The old generic closure shim has no incoming connection.  Removing
that one unreachable node leaves every executable path unchanged; connecting
it merely to silence validation would reintroduce an unsupported closure.
"""
from __future__ import annotations

from copy import deepcopy

from haier.setup.haier_call_audit_repair import BOT_ID, WORKFLOW_ID


ORPHAN_ID = "m_n_v2_end_completed"
EDGE_ID = "e_m_n_v2_end_completed__n_v2_end_completed"
TARGET_ID = "n_v2_end_completed"
_EXPECTED_CONFIG = {
    "silent": True,
    "setSlots": {"outcome": "close_completed"},
    "terminalOutcome": True,
}


def apply_workflow_validation_repair(definition: dict) -> dict:
    """Return a copy without the exact reviewed, disconnected closure shim.

    Fail closed if the node has acquired an incoming path or changed meaning.
    Absence of both node and edge is the idempotent already-repaired state.
    """
    if definition.get("id") != WORKFLOW_ID or definition.get("bot_id", definition.get("botId")) != BOT_ID:
        raise ValueError("This repair is restricted to the reviewed Haier workflow")
    nodes, edges = definition.get("nodes", []), definition.get("edges", [])
    matches = [node for node in nodes if node.get("id") == ORPHAN_ID]
    references = [edge for edge in edges if ORPHAN_ID in (edge.get("from"), edge.get("to"))]
    if not matches:
        if references or any(edge.get("id") == EDGE_ID for edge in edges):
            raise ValueError("The removed completion shim still has an edge")
        return deepcopy(definition)
    if len(matches) != 1 or matches[0].get("kind") != "message" or matches[0].get("config") != _EXPECTED_CONFIG:
        raise ValueError("The completion shim no longer matches the reviewed node")
    if any(edge.get("to") == ORPHAN_ID for edge in edges):
        raise ValueError("The completion shim is connected and cannot be removed")
    if len(references) != 1 or references[0].get("id") != EDGE_ID or references[0].get("from") != ORPHAN_ID or references[0].get("to") != TARGET_ID:
        raise ValueError("The completion shim's outgoing contract has changed")
    if not any(node.get("id") == TARGET_ID and node.get("kind") == "end" for node in nodes):
        raise ValueError("The shared completion end node is missing")
    result = deepcopy(definition)
    result["nodes"] = [node for node in result["nodes"] if node.get("id") != ORPHAN_ID]
    result["edges"] = [edge for edge in result["edges"] if edge.get("id") != EDGE_ID]
    return result
