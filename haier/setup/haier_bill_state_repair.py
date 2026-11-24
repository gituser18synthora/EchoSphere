"""Pure, local authored workflow repair for the reviewed bill-status call.

Historical bill availability, current possession, and a technician's phone
call are separate facts.  This transform uses the existing workflow capture
and correction contracts; it changes no runtime, database, or release.
"""
from __future__ import annotations

from copy import deepcopy
import re

from haier.setup.haier_call_audit_repair import (
    BOT_ID, WORKFLOW_ID, _ATTENTION, _BARE_NO, _BARE_YES, _patterns,
)
from haier.setup.haier_followup_audit_repair import _capture, _protect, _NO_NEED, _VISIT_PENDING


VERSION = 1
MARKER = "haierBillStateRepairVersion"
PREFIX = "n_haier_bill_state_"
BILL_HUBS = (
    "n_reason_bill_q", "n_reason_bill_q_clarify", "n_bill_dealer_q",
    "n_bill_dealer_q_clarify", "n_bill_charge_q", "n_bill_charge_q_clarify",
    "n_charges_info_q",
)
CONTACT_HUBS = ("n_intro_bill", "n_intro_bill_clarify")
SERVICE_HUBS = ("n_service_need", "n_service_need_clarify")

# Danda/double danda are punctuation, not Devanagari word characters.
_WORD = r"[\w\u0900-\u0963\u0966-\u097f]"
_B = rf"(?<!{_WORD})"
_E = rf"(?!{_WORD})"
_BILL = rf"{_B}(?:bill|invoice|बिल|रसीद|इनवॉइस){_E}"
_NEG = r"(?:नहीं|नही|नहि|nahi+n?|nahin|nhi|not|no)"
_PRESENT = r"(?:है|हैं|hai|hain)"
_NOW = r"(?:अभी|अब|abhi|ab|now)"
_PAST = r"(?:उस\s+(?:समय|वक्त|वक़्त)|तब|us\s+(?:samay|waqt|vakt)|tab|at\s+that\s+time|then|(?:technician\s+ki\s+)?visit\s+ke\s+(?:time|samay)|visit\s+के\s+(?:समय|time))"
_POSSESS = r"(?:मेरे\s+पास|हमारे\s+पास|mere\s+paas|hamare\s+paas|with\s+me)"
_FILL = r"(?:(?:तो|भी|ही|to|toh|bhi|hi|अभी|अब|abhi|ab|now)\s+)*"
_QUESTION = r"(?:[?？]|\b(?:kya|why|how|when|kyun|kyon|kaise|kab)\b|क्या|क्यों|क्यूँ|कैसे|कब)"
_EXPLICIT_WARRANTY = r"(?:warranty|वारंटी|guarantee|गारंटी|free\s+(?:service|hona|hai)|free\s+(?:में|होना)|मुफ्त\s+(?:service|सर्विस))"
_CHARGES = r"(?:charges?|चार्ज(?:ेज|ेस|ेज़)?|पैसे|paise|pay|payment)"
_WHY = r"(?:क्यों|क्यूँ|kyun|kyon|why|किस\s+बात|kis\s+baat|break\s*up|breakdown|कितन[ाे]|kitna|amount)"
_HYPOTHETICAL = r"(?:\b(?:if|agar|suppose|whether)\b|अगर|यदि|शायद|shayad|maybe|perhaps)"
_WARRANTY_YES = [
    r"(?:warranty|वारंटी|guarantee|गारंटी)\s+(?:(?:mein|me|में|period)\s+)?(?:hai|है|हैं)",
    r"\b(?:under|in)\s+warranty\b",
    r"free\s+(?:service|hona\s+chahiye|होना\s+चाहिए|है|hai)",
]

_CURRENT_NO = [
    rf"{_BILL}\s+{_FILL}(?:(?:available|मौजूद)\s+)?{_NEG}\s+(?:{_PRESENT}|available|मिला|मिली)",
    rf"{_NOW}\s+(?:{_POSSESS}\s+)?(?:{_BILL}\s+)?{_NEG}\s+{_PRESENT}",
    rf"\b(?:I|we)\s+(?:do\s+not|don't|dont)\s+have\s+(?:the\s+|my\s+|a\s+)?{_BILL}",
    rf"{_BILL}\s+(?:is\s+)?(?:not\s+(?:available|with\s+me)|unavailable)(?:\s+now)?",
    rf"{_BILL}\s+(?:available\s+)?{_PRESENT}\s+{_NEG}",
]
_CURRENT_YES = [
    rf"{_BILL}\s+{_FILL}(?:(?:available|मौजूद)\s+)?{_PRESENT}",
    rf"{_BILL}\s+(?:मिल\s+गया|मिला|आ\s+गया|mil\s+gaya|aa\s+gaya)(?:\s+{_PRESENT})?",
    rf"\b(?:I|we)\s+(?:now\s+|already\s+)?have\s+(?:the\s+|my\s+|a\s+)?{_BILL}",
    rf"{_BILL}\s+(?:is\s+)?(?:available\s+now|now\s+available|with\s+me)",
]
_IMPLICIT_YES = [
    rf"{_NOW}\s+(?:{_POSSESS}\s+)?{_FILL}{_PRESENT}(?:\s+{_POSSESS})?",
    r"(?:मिल\s+गया|mil\s+gaya|got\s+it|found\s+it)(?:[\s,.!।]+(?:अभी|अब|abhi|ab|now)\s+(?:है|hai))?",
]
_AT_VISIT_NO = [
    rf"{_PAST}[^.!?।;]{{0,40}}?{_NEG}\s+(?:था|थी|tha|thi|available)",
    rf"{_BILL}\s+(?:was\s+not|wasn't)\s+available\s+(?:at|during)\s+(?:the\s+)?(?:technician'?s?\s+)?visit",
]
_AT_VISIT_YES = [
    rf"{_PAST}(?!(?:(?!{_NOW})[^.!?।;]){{0,40}}?{_NEG})(?:(?!{_NOW})[^.!?।;]){{0,40}}?(?:था|थी|tha|thi|available)",
    rf"{_BILL}\s+was\s+available\s+(?:at|during)\s+(?:the\s+)?(?:technician'?s?\s+)?visit",
]
_CALL = [
    rf"{_B}(?:call|phone|कॉल|काल|फोन|फ़ोन){_E}\s+(?:ही\s+|भी\s+|to\s+|तो\s+)?(?:आया|आई|आयी|aaya|aayi|aya|की|किया|kiya)(?:\s+(?:था|थी|tha|thi|है|hai))?",
    r"\b(?:(?:received|got)\s+(?:a\s+|the\s+)?(?:call|phone\s+call)|(?:technician|he|they)\s+called)\b",
]
_VISIT = [
    rf"{_B}(?:visit|विजिट|विज़िट){_E}\s+(?:(?:भी|bhi|तो|to)\s+)?(?:हुई|हुआ|hui|hua|ho\s+gayi)(?:\s+(?:थी|था|है|thi|tha|hai))?",
    rf"{_B}(?:technician|टेक्नीशियन|तकनीशियन|वो|वह){_E}\s+(?:भी\s+|bhi\s+)?(?:आया|आए|आये|aaya|aaye|came|visited)(?:\s+(?:था|थे|है|tha|the|hai))?",
    r"\b(?:he|they|technician)\s+(?:came|visited)\b",
]
_NO_VISIT = [
    rf"{_B}(?:visit|विजिट|विज़िट){_E}\s+(?:अभी\s+तक\s+|abhi\s+tak\s+)?{_NEG}(?:\s+(?:हुई|हुआ|hui|hua))?",
    rf"{_B}(?:technician|टेक्नीशियन|तकनीशियन|कोई|koi){_E}\s+(?:अभी\s+तक\s+|abhi\s+tak\s+)?{_NEG}\s+(?:आया|आए|आये|aaya|aaye)",
    r"\b(?:no\s+(?:one|technician)\s+came|no\s+visit|never\s+visited|did\s+not\s+visit|didn't\s+visit)\b",
    rf"{_B}(?:visit|विजिट|विज़िट){_E}\s+(?:हुई|हुआ|hui|hua)(?:\s+(?:थी|था|thi|tha))?\s+{_NEG}",
    rf"{_B}(?:technician|टेक्नीशियन|तकनीशियन|वो|वह){_E}\s+(?:आया|आए|आये|aaya|aaye)(?:\s+(?:था|थे|tha|the))?\s+{_NEG}",
]
_NO_CALL = [
    rf"{_B}(?:call|phone|कॉल|फोन|फ़ोन){_E}\s+(?:अभी\s+तक\s+|abhi\s+tak\s+)?{_NEG}",
    rf"{_B}(?:call|phone|कॉल|फोन|फ़ोन){_E}\s+(?:आया|आई|aaya|aayi)(?:\s+(?:था|थी|tha|thi))?\s+{_NEG}",
    r"\b(?:no\s+call|never\s+called|did\s+not\s+call|didn't\s+call)\b",
]


def _union(patterns):
    return "|".join(f"(?:{pattern})" for pattern in patterns)


def _guard(patterns, *, exclude=(), question=False):
    rejected = [*exclude, *([_QUESTION] if question else [])]
    prefix = rf"^(?![\s\S]*(?:{_union(rejected)}))[\s\S]*" if rejected else ""
    return [prefix + f"(?:{pattern})" for pattern in patterns]


def _put(result, node):
    if any(old["id"] == node["id"] for old in result["nodes"]):
        raise ValueError(f"Unexpected existing bill repair node: {node['id']}")
    result["nodes"].append(node)


def _edge(result, source, target, label="next"):
    result["edges"].append({"id": f"e_{source}_{label}_{target}", "from": source, "to": target, "label": label})


def _message(result, name, target, *, slots=None, en=None, hi=None):
    config = {"silent": en is None, "setSlots": slots or {}}
    if en is not None:
        config.update(text=en, textByLanguage={"en": en, "hi": hi}, responseMode="fixed")
    _put(result, {"id": name, "kind": "message", "label": "Bill status: " + name, "config": config})
    _edge(result, name, target)


def _layer(result, hub, routes, fallback):
    """New captures may override a semantic guess, never a stale route slot."""
    node = next(n for n in result["nodes"] if n["id"] == hub)
    config = node["config"]
    previous_scope = config.get("correctionCaptureVariable")
    captures = config.get("alsoCapture", [])
    corrections = [e for e in result["edges"] if e["from"] == hub and e.get("label") == "correction"]
    if len(corrections) > 1:
        raise ValueError(f"Multiple correction contracts at {hub}")
    if corrections:
        previous = _patterns([c for c in captures if c.get("variable") == previous_scope])
        if not previous:
            raise ValueError(f"Previous correction captures missing: {hub}")
        routes = [*routes, ("previous", corrections[0]["to"], previous)]
    variable = f"haier_bill_state_{hub}_route"
    config["alsoCapture"] = [*captures, *[_capture(variable, name, pats) for name, _, pats in reversed(routes)]]
    config["correctionCaptureVariable"] = variable
    first = PREFIX + hub + "_0"
    if corrections:
        corrections[0]["to"] = first
    else:
        _edge(result, hub, first, "correction")
    for index, (name, target, _) in enumerate(routes):
        node_id = PREFIX + hub + f"_{index}"
        _put(result, {"id": node_id, "kind": "condition", "label": f"Bill state: {hub}/{name}",
                      "config": {"variable": variable, "operator": "equals", "value": name}})
        _edge(result, node_id, target, "true")
        _edge(result, node_id, PREFIX + hub + f"_{index+1}" if index+1 < len(routes) else fallback, "false")


def _safe(result, hub, patterns):
    # A present-tense bill negation is not a request for service later. Other
    # future-date, paid, completion, opt-out and explicit warranty edges win.
    later = r"(?:\b(?:later|baad\s+me(?:in)?|not\s+now|out\s+of\s+(?:town|station)|busy)\b|बाद\s+में|अभी\s+(?:बाहर|व्यस्त)|abhi\s+(?:bahar|busy|ghar\s+par\s+nahi|out\s+of)|service\s+(?:abhi\s+nahi|अभी\s+नहीं)|(?:abhi\s+nahi|अभी\s+नहीं)\s+(?:karwa|करवा|bhej|भेज)|(?:lekin|but|par|लेकिन|पर)\s+(?:abhi\s+nahi|अभी\s+नहीं)|(?:abhi\s+nahi|अभी\s+नहीं)\W*$)"
    return _protect(result, hub, _guard(patterns, exclude=[_EXPLICIT_WARRANTY, later]),
                    {"n_v2_future_when", "n_charges_info_q", "m_warranty_claimed"})


def apply_bill_state_repair(definition: dict) -> dict:
    """Return the reviewed bill branch with separate historical/current facts."""
    if definition.get("id") != WORKFLOW_ID or definition.get("bot_id", definition.get("botId")) != BOT_ID:
        raise ValueError("This repair is restricted to the reviewed Haier workflow")
    original = {n["id"]: n for n in definition.get("nodes", [])}
    gate = original.get("n_identity_gate", {}).get("config", {})
    if gate.get(MARKER) == VERSION:
        return deepcopy(definition)
    if gate.get(MARKER) is not None or gate.get("haierFollowupConsentRepairVersion") != 4:
        raise ValueError("Apply the reviewed consent repair version 4 before the bill repair")
    for hub in (*BILL_HUBS, *CONTACT_HUBS, *SERVICE_HUBS):
        if original.get(hub, {}).get("kind") != "intent":
            raise ValueError(f"Reviewed bill intent is missing: {hub}")
    for target in ("n_reason_bill_q", "n_service_need", "n_bill_charge_q", "n_apology_visit",
                   "m_n_end_no_service", "m_n_v2_end_unverified", "m_warranty_claimed"):
        if target not in original:
            raise ValueError(f"Reviewed bill destination is missing: {target}")
    result = deepcopy(definition)
    nodes = {n["id"]: n for n in result["nodes"]}
    # A charge objection alone is not an explicit warranty assertion. Keep
    # the authored warranty/free-service vocabulary, dropping generic charges.
    for edge in result["edges"]:
        if edge["from"] in (*BILL_HUBS, *CONTACT_HUBS) and edge["to"] == "m_warranty_claimed":
            aliases = [alias for alias in re.split(r"[/,|]", edge.get("label", "")) if re.search(_EXPLICIT_WARRANTY, alias, re.I)]
            if not aliases:
                raise ValueError(f"Explicit warranty aliases are missing: {edge['from']}")
            edge["label"] = "/".join(aliases)

    now_q, now_clarify = PREFIX + "now_q", PREFIX + "now_clarify"
    visit_q, visit_clarify = PREFIX + "visit_q", PREFIX + "visit_clarify"
    service_q, service_clarify = PREFIX + "service_need", PREFIX + "service_need_clarify"
    # The general service question is shared by other rejection reasons.
    # Keep their graph untouched: only the bill branch enters these clones.
    for source, hub in zip(SERVICE_HUBS, (service_q, service_clarify)):
        clone = deepcopy(nodes[source])
        clone.update(id=hub, label="Bill state: " + hub)
        _put(result, clone)
        for edge in list(result["edges"]):
            if edge["from"] != source:
                continue
            clone_edge = {**deepcopy(edge), "id": f"e_bill_service_{hub}_{edge['id']}", "from": hub}
            if clone_edge["to"] == "n_service_need_clarify":
                clone_edge["to"] = service_clarify
            if clone_edge["to"] == "m_warranty_claimed":
                clone_edge["label"] = "/".join(alias for alias in re.split(r"[/,|]", edge.get("label", "")) if re.search(_EXPLICIT_WARRANTY, alias, re.I))
            result["edges"].append(clone_edge)
    questions = {
        now_q: ("Do you have the bill available now?", "क्या आपके पास अब bill available है?"),
        now_clarify: ("Just to confirm, is the bill available with you now?", "एक बार confirm कर दूँ, क्या अभी आपके पास bill available है?"),
        visit_q: ("I understand that you received a call. Did the technician also visit your place?", "समझ गई, आपको call आया था। क्या technician आपके यहाँ आए भी थे?"),
        visit_clarify: ("I have noted the call. Did a technician come to your place, or was it only a phone call?", "Call की बात समझ गई। क्या technician आपके यहाँ आए थे, या सिर्फ़ phone पर बात हुई थी?"),
    }
    for hub, (en, hi) in questions.items():
        template = deepcopy(nodes["n_reason_bill_q"] if hub in (now_q, now_clarify) else nodes["n_intro_bill_clarify"])
        template.update(id=hub, label="Bill state: " + hub)
        config = {"prompt": en, "textByLanguage": {"en": en, "hi": hi}, "responseMode": "fixed",
                  "elseIsAnswer": True, "responseDirective": "Speak the fixed authored question in the chosen language. Do not assume a visit from a phone call, or infer warranty or free service from a bill."}
        template["config"] = config
        _put(result, template)
        # Preserve all explicit special answers, before the bounded else.
        source = "n_reason_bill_q" if hub in (now_q, now_clarify) else "n_intro_bill_clarify"
        for edge in list(result["edges"]):
            if edge["from"] == source and ("e_v2_special_" in edge["id"] or "e_warranty_" in edge["id"]):
                clone = {**deepcopy(edge), "id": f"e_bill_clone_{hub}_{edge['id']}", "from": hub}
                result["edges"].append(clone)
        _edge(result, hub, now_clarify if hub == now_q else visit_clarify if hub == visit_q else "m_n_v2_end_unverified", "else")

    current_yes = _guard(_CURRENT_YES, exclude=[*_CURRENT_NO, _HYPOTHETICAL])
    implicit_yes = _guard(_IMPLICIT_YES, exclude=[*_CURRENT_NO, _HYPOTHETICAL], question=True)
    # A why/amount objection may carry a bill assertion. It is never consent
    # to payment, nor itself an affirmative warranty claim.
    bill_question = [rf"^(?=[\s\S]*{_CHARGES})(?=[\s\S]*{_WHY})[\s\S]+$"]
    ack, policy, unknown_policy = PREFIX+"available_ack", PREFIX+"charges_policy", PREFIX+"unknown_bill_policy"
    _message(result, ack, service_q, en="Alright, I have noted that the bill is available with you now.",
             hi="ठीक है, आपके पास अब bill available है, मैंने यह बात note कर ली है।")
    _message(result, policy, service_q, en="I have noted that you have the bill. The bill and applicable charges need to be checked by the technician; I cannot confirm charges or warranty from here.",
             hi="आपके पास bill है, यह मैंने note कर लिया है। Bill और लागू होने वाले charges technician check करेंगे; मैं यहाँ से charges या warranty confirm नहीं कर सकती।")
    _message(result, unknown_policy, now_q, en="The bill and applicable charges need to be checked by the technician; I cannot confirm charges or warranty from here.",
             hi="Bill और लागू होने वाले charges technician check करेंगे; मैं यहाँ से charges या warranty confirm नहीं कर सकती।")
    policy_known = PREFIX + "policy_known"
    _put(result, {"id": policy_known, "kind": "condition", "config": {"variable": "bill_available_now", "operator": "equals", "value": "yes"}})
    _edge(result, policy_known, policy, "true")
    _edge(result, policy_known, unknown_policy, "false")
    known_now = PREFIX+"known_now"
    _put(result, {"id": known_now, "kind": "condition", "config": {"variable": "bill_available_now", "operator": "equals", "value": "yes"}})
    _edge(result, known_now, ack, "true")
    known_no = PREFIX+"known_no"
    _edge(result, known_now, known_no, "false")
    _put(result, {"id": known_no, "kind": "condition", "config": {"variable": "bill_available_now", "operator": "equals", "value": "no"}})
    _edge(result, known_no, "n_bill_charge_q", "true")
    _edge(result, known_no, now_q, "false")
    _message(result, PREFIX+"past_no", known_now, slots={"bill_available_at_visit": "no"})
    _message(result, PREFIX+"past_yes", service_q, slots={"bill_available_at_visit": "yes"})
    _message(result, PREFIX+"current_yes", ack, slots={"bill_available_now": "yes"})
    _message(result, PREFIX+"current_no", "n_bill_charge_q", slots={"bill_available_now": "no"})
    _message(result, PREFIX+"contact_visit", "n_reason_bill_q", slots={"technician_contact_type": "visit"})
    _message(result, PREFIX+"contact_no_visit", "n_apology_visit", slots={"technician_contact_type": "no_visit"})
    _message(result, PREFIX+"contact_phone", visit_q, slots={"technician_contact_type": "phone_call"})
    _message(result, PREFIX+"contact_phone_clarify", visit_clarify, slots={"technician_contact_type": "phone_call"})

    for hub in (*BILL_HUBS, service_q, service_clarify, now_q, now_clarify):
        implicit = hub in (*BILL_HUBS, now_q, now_clarify)
        other_subject = r"(?:payment|पेमेंट|refund|रिफंड|product|प्रोडक्ट|पैसे|paise|salary|सैलरी|technician|टेक्नीशियन|delivery|डिलीवरी)"
        yes_patterns = [*current_yes, *(_guard(implicit_yes, exclude=[other_subject]) if implicit else [])]
        captures = next(n for n in result["nodes"] if n["id"] == hub)["config"].setdefault("alsoCapture", [])
        asserted_in_charge_question = [
            rf"^(?![\s\S]*{_HYPOTHETICAL})(?!\W*(?:क्या|kya|does|do|is)(?:\s|$))(?=[\s\S]*{_CHARGES})(?=[\s\S]*{_WHY})[\s\S]*(?:{_union(current_yes)})"
        ]
        denied_in_charge_question = [
            rf"^(?![\s\S]*{_HYPOTHETICAL})(?!\W*(?:क्या|kya|does|do|is)(?:\s|$))(?=[\s\S]*{_CHARGES})(?=[\s\S]*{_WHY})[\s\S]*(?:{_union(_CURRENT_NO)})"
        ]
        captures.extend([_capture("bill_available_at_visit", "no", _guard(_AT_VISIT_NO, exclude=[_HYPOTHETICAL], question=True)),
                         _capture("bill_available_at_visit", "yes", _guard(_AT_VISIT_YES, exclude=[*_AT_VISIT_NO, _HYPOTHETICAL], question=True)),
                         _capture("bill_available_now", "yes", [*_guard(yes_patterns, question=True), *asserted_in_charge_question]),
                         _capture("bill_available_now", "no", [*_guard(_CURRENT_NO, exclude=[_HYPOTHETICAL], question=True), *denied_in_charge_question])])
        fallback = now_clarify if hub == now_q else "m_n_v2_end_unverified" if hub == now_clarify else (
            service_clarify if hub == service_q else "m_n_v2_end_unverified" if hub == service_clarify else
            "n_reason_bill_q_clarify" if hub == "n_reason_bill_q" else "n_bill_dealer_q_clarify" if hub == "n_bill_dealer_q" else
            "n_bill_charge_q_clarify" if hub in ("n_bill_charge_q", "n_charges_info_q") else "m_n_v2_end_unverified")
        routes = [
            ("no_service", "m_n_end_no_service", _protect(result, hub, _guard(_NO_NEED, question=True),
                {"m_warranty_claimed", "n_v2_future_when", "n_charges_info_q"})),
            ("warranty_unconfirmed", policy_known, _protect(result, hub,
                [rf"^(?=[\s\S]*{_EXPLICIT_WARRANTY})(?![\s\S]*(?:{_union(_guard(_WARRANTY_YES, exclude=[_HYPOTHETICAL, _NEG], question=True))}))[\s\S]+$"],
                {"m_warranty_claimed", "n_charges_info_q"})),
            ("bill_charges_question", policy_known, _safe(result, hub, bill_question)),
            ("current_no", PREFIX+"current_no", _safe(result, hub, _guard(_CURRENT_NO, question=True))),
            ("current_yes", PREFIX+"current_yes", _safe(result, hub, _guard(yes_patterns, question=True))),
        ]
        if hub in ("n_reason_bill_q", "n_reason_bill_q_clarify"):
            routes.extend([
                ("past_no", PREFIX+"past_no", _safe(result, hub, _guard([*_AT_VISIT_NO, _BARE_NO], question=True))),
                ("past_yes", PREFIX+"past_yes", _safe(result, hub, _guard([*_AT_VISIT_YES, _BARE_YES], exclude=_AT_VISIT_NO, question=True))),
            ])
        if hub in (now_q, now_clarify):
            routes.extend([("now_yes", PREFIX+"current_yes", _safe(result, hub, _guard([_BARE_YES], question=True))),
                           ("now_no", PREFIX+"current_no", _safe(result, hub, _guard([_BARE_NO], question=True)))])
        if hub in (service_q, service_clarify):
            routes.extend([("visit_pending", service_clarify, _safe(result, hub, list(_VISIT_PENDING))),
                           ("attention", service_clarify if hub == service_q else "m_n_v2_end_unverified", list(_ATTENTION))])
        _layer(result, hub, routes, fallback)

    for hub in (*CONTACT_HUBS, visit_q, visit_clarify):
        fallback = "n_intro_bill_clarify" if hub == "n_intro_bill" else visit_clarify if hub == visit_q else "m_n_v2_end_unverified"
        call_target = PREFIX+"contact_phone" if hub in CONTACT_HUBS else PREFIX+"contact_phone_clarify" if hub == visit_q else "m_n_v2_end_unverified"
        routes = [
            ("no_service", "m_n_end_no_service", _protect(result, hub, _guard(_NO_NEED, question=True),
                {"m_warranty_claimed", "n_v2_future_when", "n_charges_info_q"})),
            ("no_visit", PREFIX+"contact_no_visit", _safe(result, hub, _guard([*_NO_VISIT, _BARE_NO], question=True))),
            ("no_call", "n_intro_bill_clarify", _safe(result, hub, _guard(_NO_CALL, exclude=_VISIT, question=True))),
            ("visited", PREFIX+"contact_visit", _safe(result, hub, _guard([*_VISIT, _BARE_YES], exclude=_NO_VISIT, question=True))),
            ("phone_only", call_target, _safe(result, hub, _guard(_CALL, exclude=[*_VISIT, *_NO_VISIT, r"(?:call|phone|कॉल|फोन)\s+"+_NEG, r"(?:call|phone|कॉल|फोन)\s+(?:आया|आई|aaya|aayi)(?:\s+(?:था|थी|tha|thi))?\s+"+_NEG], question=True))),
        ]
        _layer(result, hub, routes, fallback)
    next(n for n in result["nodes"] if n["id"] == "n_identity_gate")["config"][MARKER] = VERSION
    return result
