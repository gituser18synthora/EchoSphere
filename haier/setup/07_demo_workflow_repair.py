"""Pure, bot-specific repair of the exported Haier v77 workflow.

This module never reads credentials, connects to a server, or writes a database.
Apply ``build_definition(exported_workflow)`` to a reviewed local snapshot. The
caller is responsible for validation and any separately authorised publication.
"""
from __future__ import annotations

from copy import deepcopy
import re

from haier.setup.haier_unit_ok_repair import apply_unit_ok_repair


BOT_ID = "bot_450a4f7b4832"
WORKFLOW_ID = "wf_1c3e59362c51"
CONTACT_VARIABLE = "haier_technician_contact_route"
INTRO_IDS = ("n_intro_unit_ok", "n_intro_unit_ok_clarify")
CONTACT_CAPTURE_ORDER = ("unknown", "no_visit", "no_call", "no_contact", "contact_confirmed")

# Interpret the contact clause, rather than any occurrence of a negative word
# elsewhere in the answer (for example dissatisfaction or an unresolved issue).
_BOUNDARY = r"(?<![\w\u0900-\u097f])"
_NEG = r"(?:नहीं|नही|ना|nahi+n?|nhi|not|never|no)"
_CONTACT = r"(?:call|phone|visit|technician|कॉल|काल|फोन|फ़ोन|विजिट|विज़िट|टेक्नीशियन|तकनीशियन|संपर्क|बात)"
_NOT_NEGATED_PREFIX = r"(?<!नहीं )(?<!नही )(?<!ना )(?<!no )(?<!never )(?<!not )"
_PAST_AUX = r"(?:था|थी|थे|है|हैं|tha|thi|the|hai)"
_POSITIVE_CONTACT = [
    rf"{_BOUNDARY}{_NOT_NEGATED_PREFIX}{_CONTACT}\s+(?:तो\s+|to\s+)?(?:आया|आयी|आई|आए|आये|हुई|हुआ|की|किया|aaya|aayi|aaye|hui|hua|kiya|came|called|visited|received|got)(?=[\s,.!?।]|$)(?!\s+(?:(?:तो|to)\s+)?(?:{_PAST_AUX}\s+)?{_NEG})",
    rf"{_BOUNDARY}(?<!not )(?<!never )(?<!haven't )(?:received|got|had)\s+(?:(?:a|the|any)\s+)?(?:call|visit)",
    rf"{_BOUNDARY}{_NOT_NEGATED_PREFIX}(?:he|they|technician)\s+(?:came|called|visited)",
    # Ellipsis is accepted only at the start of the response to this contact
    # question, not 'problem hui thi' or 'service hui thi' in another clause.
    rf"^\W*(?:(?:हाँ|हां|जी|haan|han|ji|yes)[\s,]*)?(?:हुई|हुआ|आया|आए|आये|आई|आयी|hui|hua|aaya|aaye|aayi)\s+(?:तो\s+|to\s+)?(?:थी|था|थे|thi|tha|the)(?=[\s,.!?।]|$)(?!\s+{_NEG})",
]
_NEGATIVE_CONTACT = [
    rf"{_BOUNDARY}{_CONTACT}\s+(?:(?:अभी|अभी\s+तक|abhi|abhi\s+tak)\s+)?{_NEG}\s+(?:आय[ाीे]|आए|आये|हुई|हुआ|की|किया|aaya|aayi|aaye|hui|hua|kiya)",
    rf"{_BOUNDARY}(?:कोई|किसी|koi|kisi)\s+(?:(?:भी|bhi)\s+)?{_NEG}\s+(?:आया|आए|आये|aaya|aaye)",
    r"\b(?:no|neither)\s+(?:call|visit|contact|technician)\b",
    r"\b(?:no\s+one|nobody)\s+(?:came|called|visited)\b",
    r"\b(?:did\s+not|didn't|never|have\s+not|haven't)\s+(?:receive|get|received|got|had)\s+(?:(?:a|the|any)\s+)?(?:call|visit)\b",
    rf"{_BOUNDARY}{_NEG}\s+{_CONTACT}\s+(?:आया|आए|आये|हुई|हुआ|aaya|aaye|hui|hua|came|called|visited)",
    rf"{_BOUNDARY}{_CONTACT}\s+(?:आया|आए|आये|हुई|हुआ|aaya|aaye|hui|hua)(?:\s+{_PAST_AUX})?\s+{_NEG}",
]
_BARE_YES = r"^\W*(?:(?:हाँ|हां|जी|हाँजी|हांजी|यस|हम्म|haan|han|ji|jee|yes|yeah|yup|hmm|correct|right)(?:[\s,.!?।]+|$))+(?:(?:सर|मैडम|sir|madam|mam)[\s,.!?।]*)?\W*$"
_BARE_NO = r"^\W*(?:(?:नहीं|नही|जी|नो|नौ|नू|nahi+n?|nhi|ji|jee|no|nope)(?:[\s,.!?।]+|$))*(?:नहीं|नही|नो|नौ|नू|nahi+n?|nhi|no|nope)(?:[\s,.!?।]*(?:जी|ji|jee|sir|madam|mam))*\W*$"
_UNCERTAIN = r"(?:पता\s*नहीं|मालूम\s*नहीं|याद\s*नहीं|शायद|pata\s*nahi|yaad\s*nahi|not\s+sure|don'?t\s+(?:know|remember)|maybe|perhaps)"
_CALL = r"(?:call|phone|कॉल|काल|फोन|फ़ोन|संपर्क|बात)"
_VISIT = r"(?:visit|technician|विजिट|विज़िट|टेक्नीशियन|तकनीशियन)"
_NO_BOTH = [
    rf"(?:ना|na|no|neither)\s+{_CALL}[^,.!?।]*(?:ना|na|nor|no)\s+{_VISIT}",
    rf"{_CALL}\s*(?:या|और|or|and|aur|ya)\s*{_VISIT}\s+{_NEG}",
    rf"(?=[\s\S]*{_CALL}\s+{_NEG})(?=[\s\S]*{_VISIT}\s+{_NEG})",
]


def _tokens(label: str) -> list[str]:
    return [part.strip().lower() for part in re.split(r"[/,|]", label) if part.strip()]


def _literal_pattern(tokens: list[str]) -> str:
    """Factor exact alias prefixes; this guard only tests whether any matches."""
    trie = {}
    for token in tokens:
        branch = trie
        for character in token:
            branch = branch.setdefault(character, {})
        branch[""] = None

    def emit(branch):
        children = [re.escape(character) + emit(child)
                    for character, child in sorted(branch.items()) if character]
        if not children:
            return ""
        suffix = children[0] if len(children) == 1 else "(?:" + "|".join(children) + ")"
        return "(?:" + suffix + ")?" if "" in branch else suffix

    return _BOUNDARY + emit(trie)


def contact_entity(special_edges: list[dict]) -> dict:
    """Scoped fact capture; existing special-edge selection remains untouched.

    A literal special-case match opts OUT of this capture. The original
    interpreter then chooses among those edges with its original longest-token
    and signal priorities. Copying them into a new canonical matcher would
    silently change that precedence when several special cases occur together.
    """
    special_tokens = [token for edge in special_edges for token in _tokens(edge["label"])]
    special = _literal_pattern(special_tokens) if special_tokens else r"(?!)"

    def guarded(patterns):
        # Canonical matching accepts any alternative. Keep that same ordered
        # OR inside one expression, so the authored special-case exclusion is
        # compiled and checked once per canonical, rather than once for every
        # contact phrasing. This only changes Haier's workflow data.
        alternatives = "|".join(f"(?:{pattern})" for pattern in patterns)
        return [rf"^(?![\s\S]*{special})[\s\S]*(?:{alternatives})"]

    # Contact facts win over negation in another clause. Uncertainty about the
    # contact itself still stays unconfirmed; an explicit special case is left
    # entirely to its original edge above.
    def certain(pattern):
        # An uncertain installation date in a separate clause does not undo
        # an explicit contact fact. Uncertainty in the contact clause does.
        return (rf"^(?![\s\S]*{_UNCERTAIN}[^,.!?।]*(?:{pattern}))"
                rf"(?![\s\S]*(?:{pattern})[^,.!?।]*{_UNCERTAIN})[\s\S]*(?:{pattern})")

    positive = [certain(p) for p in _POSITIVE_CONTACT]
    no_call = [p.replace(_CONTACT, _CALL) for p in _NEGATIVE_CONTACT]
    no_visit = [p.replace(_CONTACT, _VISIT) for p in _NEGATIVE_CONTACT]
    # Keep only the relevant noun in English structural patterns. A missing
    # call does not establish that no visit happened, or conversely.
    no_call = [p.replace("(?:call|visit|contact|technician)", "(?:call|contact)")
                .replace("(?:call|visit)", "call") for p in no_call
               if "no\\s+one|nobody" not in p and "(?:कोई|किसी|koi|kisi)" not in p]
    no_call.append(r"\b(?:no\s+one|nobody)\s+called\b")
    no_call.append(r"\b(?:did\s+not|didn't|never|has\s+not|hasn't)\s+call\b")
    no_visit = [p.replace("(?:call|visit|contact|technician)", "(?:visit|technician)")
                 .replace("(?:call|visit)", "visit")
                 .replace("(?:came|called|visited)", "(?:came|visited)") for p in no_visit]
    no_visit.append(r"\b(?:did\s+not|didn't|never|has\s+not|hasn't)\s+(?:come|visit)\b")
    other_subject = r"(?:प्रॉब्लम|समस्या|दिक्कत|काम|सर्विस|संतुष्ट|सेटिस्फाई|सैटिस्फाई|problem|issue|work|service|satisf|installation|इंस्टॉलेशन)"
    unrelated_negation = rf"{other_subject}[^,.!?।]*{_NEG}"
    return {"dataType": "text", "matchCanonicalValues": False,
            "synonymPatterns": {
                "contact_confirmed": guarded(positive + [_BARE_YES]),
                "no_contact": guarded([certain(p) for p in _NO_BOTH + [_BARE_NO]]),
                "no_call": guarded([certain(p) for p in no_call]),
                "no_visit": guarded([certain(p) for p in no_visit]),
                "unknown": guarded([_UNCERTAIN, unrelated_negation]),
            }}


def _set_ordered_contact_captures(node: dict, special_edges: list[dict]) -> None:
    """Keep contact precedence in a JSON array, which MySQL preserves.

    JSON object key order is not retained by MySQL. Each entity therefore
    contains only one canonical, and later successful captures overwrite
    earlier ones. The final slot has the same precedence as contact_entity's
    original reviewed order, independently of persisted object key ordering.
    """
    entity = contact_entity(special_edges)
    config = node["config"]
    unrelated = [capture for capture in config.get("alsoCapture") or []
                 if not isinstance(capture, dict)
                 or capture.get("variable") != CONTACT_VARIABLE]
    config["alsoCapture"] = unrelated + [
        {"variable": CONTACT_VARIABLE, "overwrite": True,
         "entity": {**deepcopy(entity),
                    "synonymPatterns": {canonical: deepcopy(entity["synonymPatterns"][canonical])}}}
        for canonical in CONTACT_CAPTURE_ORDER
    ]


def apply_contact_repair(definition: dict) -> dict:
    """Repair only the Unit-OK contact question implicated in these calls."""
    result = deepcopy(definition)
    nodes = {node["id"]: node for node in result["nodes"]}
    marker = "e_n_intro_unit_ok_contact_correction"
    if any(e["id"] == marker for e in result["edges"]):
        # Upgrade an already published repair without duplicating graph nodes
        # or edges. Existing multi-canonical captures were order-sensitive.
        for node_id in INTRO_IDS:
            specials = [edge for edge in result["edges"]
                        if edge["from"] == node_id
                        and edge["id"].startswith("e_v2_special_")]
            _set_ordered_contact_captures(nodes[node_id], specials)
        return result
    base_edges = [e for e in result["edges"] if e["from"] == INTRO_IDS[0]]
    yes = next(e for e in base_edges if e["id"] == "e_n_intro_unit_ok_yes")
    no = next(e for e in base_edges if e["id"] == "e_n_intro_unit_ok_no")
    if yes["to"] != "n_reason_unit_ok_q" or no["to"] != "n_apologize_no_visit":
        raise ValueError("Unexpected Unit-OK contact destinations; review the newer snapshot")
    for node_id in INTRO_IDS:
        node = nodes[node_id]
        if node.get("kind") != "intent":
            raise ValueError("Unexpected contact node kind")
        outgoing = [e for e in result["edges"] if e["from"] == node_id]
        if any("correction" in _tokens(e.get("label", "")) for e in outgoing):
            raise ValueError("Contact node already declares a different correction edge")
        specials = [e for e in outgoing if e["id"].startswith("e_v2_special_")]
        fallback = next((e["to"] for e in outgoing if e.get("label") == "else"), None)
        if not fallback:
            raise ValueError("Contact node is missing its reviewed uncertainty destination")
        _set_ordered_contact_captures(node, specials)
        conditions = [("contact_confirmed", yes["to"]), ("no_contact", no["to"]),
                      ("no_call", "n_apology_call"), ("no_visit", "n_apology_visit")]
        condition_ids = [f"{node_id}_{canonical}" for canonical, _ in conditions]
        if any(condition_id in nodes for condition_id in condition_ids):
            raise ValueError("Generated contact condition IDs already exist")
        for condition_id, (canonical, _) in zip(condition_ids, conditions):
            result["nodes"].append({"id": condition_id, "kind": "condition",
                                    "label": f"Haier scoped contact fact = {canonical}?",
                                    "config": {"variable": CONTACT_VARIABLE,
                                               "operator": "equals", "value": canonical}})
        result["edges"].append({"id": f"e_{node_id}_contact_correction", "from": node_id,
                                "to": condition_ids[0], "label": "correction"})
        for index, (condition_id, (_, target)) in enumerate(zip(condition_ids, conditions)):
            result["edges"].extend([
                {"id": f"e_{condition_id}_yes", "from": condition_id, "to": target, "label": "true"},
                {"id": f"e_{condition_id}_no", "from": condition_id,
                 "to": condition_ids[index + 1] if index + 1 < len(condition_ids) else fallback,
                 "label": "false"},
            ])
    return result


def build_definition(exported_workflow: dict) -> dict:
    """Return a repaired copy; no changes to source snapshots or services."""
    if exported_workflow.get("id") != WORKFLOW_ID:
        raise ValueError("This repair is only for the reviewed Haier workflow")
    return apply_unit_ok_repair(apply_contact_repair(exported_workflow))
