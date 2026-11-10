"""Repair the Haier no-defect / unresolved-problem branch as authored data.

The client requirements table (HAIER_BOT_UPDATE_PROMPT.md, reason 7) and the
explicit conflict resolution in COVERAGE_CHECKLIST.md require an apology
followed by the re-dispatch proposal when the customer confirms that the
problem persists.  Asking the general service-requirement question again
adds a step that this branch does not require.

This module performs no I/O and never changes a workflow in place.  It does
not change how the shared workflow engine interprets customer speech.
"""
from __future__ import annotations

from copy import deepcopy
import re


APOLOGY_NODE = "n_unit_ok_pending_apology"
APOLOGY_EDGE = "e_unit_ok_pending_apology_redispatch"
PENDING_EDGES = ("e_unit_ok_issue", "e_unit_ok_issue__clarify")
STATUS_NODES = ("n_reason_unit_ok_q", "n_reason_unit_ok_q_clarify")
TIMING_VARIABLE = "haier_service_timing_route"
FUTURE_SOURCES = (
    ("e_v2_special_n_redispatch_n_v2_future_when", "future_when"),
    ("e_v2_special_n_redispatch_n_future_confirm", "future_date"),
)
# At a re-dispatch proposal, "not now" can decline immediate service. At
# the preceding problem-status question it can instead mean "not fixed
# yet". Reusing those aliases there would change the question's meaning.
_AMBIGUOUS_STATUS_NEGATIONS = frozenset({
    "not now", "abhi nahi", "अभी नहीं", "par abhi nahi", "lekin abhi nahi",
    "magar abhi nahi", "but abhi nahi", "but not now", "पर अभी नहीं",
    "लेकिन अभी नहीं", "मगर अभी नहीं", "hai par abhi nahi",
    "hai lekin abhi nahi", "hai magar abhi nahi", "to hai par abhi nahi",
    "toh hai par abhi nahi", "to hai lekin abhi nahi", "है पर अभी नहीं",
    "है लेकिन अभी नहीं", "तो है पर अभी नहीं", "तो है लेकिन अभी नहीं",
})


def _add_existing_future_routes(result: dict) -> None:
    """Reuse the existing postponement flow for volunteered service timing."""
    by_node = {node["id"]: node for node in result["nodes"]}
    by_edge = {edge["id"]: edge for edge in result["edges"]}
    source_aliases = {}
    for source_id, suffix in FUTURE_SOURCES:
        source = by_edge.get(source_id)
        if not source or source.get("to") not in by_node or source.get("from") != "n_redispatch":
            raise ValueError(f"Existing Haier service-timing edge is missing: {source_id}")
        aliases = [
            token.strip() for token in re.split(r"[/,|]", source.get("label") or "")
            if token.strip() and token.strip().lower() not in _AMBIGUOUS_STATUS_NEGATIONS
        ]
        if not aliases:
            raise ValueError(f"Existing Haier service-timing aliases are missing: {source_id}")
        source_aliases[suffix] = aliases
        for status_id in STATUS_NODES:
            if status_id not in by_node:
                raise ValueError(f"Haier problem-status node is missing: {status_id}")
            edge_id = f"e_haier_{status_id}_{suffix}"
            edge = {"id": edge_id, "from": status_id, "to": source["to"], "label": "/".join(aliases)}
            if edge_id in by_edge:
                if by_edge[edge_id] != edge:
                    raise ValueError(f"Existing Haier service-timing repair is different: {edge_id}")
            else:
                result["edges"].append(edge)

    # The existing vague-future aliases include long phrases such as "not
    # needed now". A shorter explicit date in the same answer must still go
    # directly to confirmation, rather than asking for that date again.
    # Reuse the interpreter's existing capture/correction contract; original
    # completed/pending special answers opt out and retain their own checks.
    boundary = r"(?<![\w\u0900-\u097f])"

    def literal(aliases):
        return boundary + "(?:" + "|".join(re.escape(alias) for alias in aliases) + ")"

    vague, dated = literal(source_aliases["future_when"]), literal(source_aliases["future_date"])
    date_target = by_edge[FUTURE_SOURCES[1][0]]["to"]
    for status_id in STATUS_NODES:
        outgoing = [edge for edge in result["edges"] if edge["from"] == status_id]
        correction_id = f"e_haier_{status_id}_dated_postponement_correction"
        if any(edge["id"] != correction_id and "correction" in re.split(r"[/,|]", edge.get("label") or "")
               for edge in outgoing):
            raise ValueError(f"Haier problem-status node already has a correction route: {status_id}")
        special_aliases = [
            token.strip() for edge in outgoing if edge["id"].startswith("e_v2_special_")
            for token in re.split(r"[/,|]", edge.get("label") or "") if token.strip()
        ]
        special_guard = rf"(?![\s\S]*{literal(special_aliases)})" if special_aliases else ""
        pattern = rf"^{special_guard}(?=[\s\S]*{vague})(?=[\s\S]*{dated})[\s\S]+$"
        capture = {
            "variable": TIMING_VARIABLE,
            "entity": {"dataType": "text", "matchCanonicalValues": False,
                       "synonymPatterns": {"dated_postponement": [pattern]}},
            "overwrite": True,
        }
        captures = by_node[status_id]["config"].setdefault("alsoCapture", [])
        existing = next((item for item in captures if item.get("variable") == TIMING_VARIABLE), None)
        if existing is not None and existing != capture:
            raise ValueError(f"Existing Haier service-timing capture is different: {status_id}")
        if existing is None:
            captures.append(capture)
        correction = {"id": correction_id, "from": status_id, "to": date_target, "label": "correction"}
        if correction_id in by_edge and by_edge[correction_id] != correction:
            raise ValueError(f"Existing Haier service-timing correction is different: {status_id}")
        if correction_id not in by_edge:
            result["edges"].append(correction)


def apply_unit_ok_repair(definition: dict) -> dict:
    """Return an isolated workflow with the confirmed pending branch repaired.

    The two existing unresolved-status edges are redirected. Existing
    postponement branches also become available when the caller volunteers
    service timing at either status question. Completed, resolved and
    uncertain-response branches retain their existing targets. The
    re-dispatch proposal still asks the customer to agree, and the existing
    outcome marker is reached only after that response.

    Reject unexpected source data rather than silently applying a partial
    repair to a different workflow revision.
    """
    result = deepcopy(definition)
    nodes = result.get("nodes") or []
    edges = result.get("edges") or []
    by_node = {node["id"]: node for node in nodes}
    by_edge = {edge["id"]: edge for edge in edges}
    if "n_redispatch" not in by_node:
        raise ValueError("Haier re-dispatch proposal node is missing")
    for edge_id in PENDING_EDGES:
        edge = by_edge.get(edge_id)
        if edge is None or edge.get("to") not in ("n_service_need", APOLOGY_NODE):
            raise ValueError(f"Unexpected Haier pending-status edge: {edge_id}")

    apology = {
        "id": APOLOGY_NODE,
        "kind": "message",
        "label": "Unit OK — confirmed problem persists: apologise",
        "x": 1080,
        "y": 1230,
        "config": {
            "text": "I apologise for the inconvenience.",
            "responseMode": "fixed",
            "textByLanguage": {
                "en": "I apologise for the inconvenience.",
                "hi": "माफ़ी चाहूँगी असुविधा के लिए।",
            },
            "responseDirective": (
                "Speak this exact fixed line in the chosen language. "
                "Do not claim that a call or visit did not happen. "
                "Do not claim that a dispatch or backend action is completed."
            ),
        },
    }
    next_edge = {
        "id": APOLOGY_EDGE,
        "from": APOLOGY_NODE,
        "to": "n_redispatch",
    }
    if APOLOGY_NODE in by_node:
        if by_node[APOLOGY_NODE] != apology or by_edge.get(APOLOGY_EDGE) != next_edge:
            raise ValueError("Existing Haier pending-status repair is different")
    else:
        if APOLOGY_EDGE in by_edge:
            raise ValueError("Haier pending-status repair edge already exists")
        nodes.append(apology)
        edges.append(next_edge)
    for edge_id in PENDING_EDGES:
        by_edge[edge_id]["to"] = APOLOGY_NODE
    result["nodes"], result["edges"] = nodes, edges
    _add_existing_future_routes(result)
    return result
