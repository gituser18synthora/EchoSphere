"""Repair explicit service refusals in the Haier workflow as authored data.

Pure transform: no API, database, model calls or shared runtime changes.
The service question and its clarification retain their existing special-case
and postponement routes. A scoped refusal capture uses the interpreter's
existing correction edge to give an explicit no-service answer priority over
the overlapping service-required keyword aliases.
"""
from __future__ import annotations

from copy import deepcopy
import re


WORKFLOW_ID = "wf_1c3e59362c51"
BOT_ID = "bot_450a4f7b4832"
HUBS = ("n_service_need", "n_service_need_clarify")
VARIABLE = "haier_service_need_declined"
NO_SERVICE = "m_n_end_no_service"

_BOUNDARY = r"(?<![\w\u0900-\u097f])"
_END = r"(?![\w\u0900-\u097f])"
_NEGATIVE = r"(?:nahi+n?|nahin|nhi|नहीं|नही|not)"
_NEED = r"(?:requirement|रिक्वायरमेंट|रिक्वायरमेन्ट|zaroorat|zarurat|ज़रूरत|जरूरत|आवश्यकता)"
_SERVICE = r"(?:service|सर्विस|सेवा)"
_MODIFIER = r"(?:ki|की|to|toh|तो|bilkul|बिलकुल|बिल्कुल|abhi|अभी|currently|is|are)"
_DECLINE = (
    rf"{_BOUNDARY}{_NEED}(?:\s+{_MODIFIER}){{0,3}}\s+{_NEGATIVE}{_END}",
    rf"{_BOUNDARY}{_SERVICE}(?:\s+{_MODIFIER}){{0,3}}\s+{_NEGATIVE}\s+"
    rf"(?:chahiye|चाहिए|needed|required|wanted|करवानी|करवाना|karwani|karwana){_END}",
    rf"\b(?:don't|do not|dont|no longer)\s+(?:need|want|require)\s+"
    rf"(?:(?:a|any|the|your)\s+)?{_SERVICE}\b",
    rf"\bno\s+(?:service|requirement)(?:\s+(?:needed|required))?\b",
)
_QUESTION = r"(?:\?|\b(?:why|how|when|kya|kyun|kyon|kaise|kab)\b|क्यों|क्यूँ|कैसे|क्या)"


def _tokens(label: str) -> list[str]:
    return [token.strip().lower() for token in re.split(r"[/,|]", label) if token.strip()]


def _capture(edges: list[dict]) -> dict:
    # Existing authored special cases (including later/date/uncertain
    # answers) keep their precedence. A noun alone is never agreement.
    special = [token for edge in edges
               if edge["id"].startswith("e_v2_special_") or edge["id"].endswith("__unsure")
               for token in _tokens(edge.get("label", ""))]
    guard = (rf"(?![\s\S]*{_BOUNDARY}(?:" + "|".join(map(re.escape, special)) + "))") if special else ""
    alternatives = "|".join(f"(?:{pattern})" for pattern in _DECLINE)
    pattern = rf"^{guard}(?![\s\S]*{_QUESTION})[\s\S]*(?:{alternatives})"
    re.compile(pattern, re.IGNORECASE)
    return {
        "variable": VARIABLE,
        "overwrite": True,
        "entity": {"dataType": "text", "matchCanonicalValues": False,
                   "synonymPatterns": {"yes": [pattern]}},
    }


def apply_service_need_repair(definition: dict) -> dict:
    """Return a repaired copy of the reviewed bot's service-requirement hubs.

    Reject changed topology/correction contracts rather than redirect an
    unrelated workflow. Reapplying the same repair is a no-op.
    """
    if definition.get("id") != WORKFLOW_ID or definition.get("bot_id", BOT_ID) != BOT_ID:
        raise ValueError("This repair is restricted to the reviewed Haier workflow")
    result = deepcopy(definition)
    nodes = {node["id"]: node for node in result["nodes"]}
    if nodes.get(NO_SERVICE, {}).get("kind") != "message":
        raise ValueError("The reviewed no-service outcome node is missing")
    for hub in HUBS:
        node = nodes.get(hub)
        if not node or node.get("kind") != "intent":
            raise ValueError(f"The reviewed service question is missing: {hub}")
        outgoing = [edge for edge in result["edges"] if edge.get("from") == hub]
        suffix = "__clarify" if hub.endswith("_clarify") else ""
        negative = next((edge for edge in outgoing if edge["id"] == "e_service_no" + suffix), None)
        positive = next((edge for edge in outgoing if edge["id"] == f"e_{hub}__service_required"), None)
        if not negative or negative.get("to") != NO_SERVICE or not positive or positive.get("to") != "n_redispatch":
            raise ValueError(f"Unexpected service-requirement destinations: {hub}")
        correction = {"id": f"e_haier_{hub}_explicit_no_service", "from": hub,
                      "to": NO_SERVICE, "label": "correction"}
        existing = [edge for edge in outgoing if "correction" in _tokens(edge.get("label", ""))]
        if existing and existing != [correction]:
            raise ValueError(f"Service question already has another correction route: {hub}")
        capture = _capture(outgoing)
        config = node.setdefault("config", {})
        captures = config.get("alsoCapture") or []
        if captures and captures != [capture]:
            raise ValueError(f"Service question already has a different capture contract: {hub}")
        config["alsoCapture"] = [capture]
        positive["label"] = "/".join(token for token in positive["label"].split("/")
                                     if token.strip().lower() not in ("requirement", "रिक्वायरमेंट"))
        if not existing:
            result["edges"].append(correction)
    return result
