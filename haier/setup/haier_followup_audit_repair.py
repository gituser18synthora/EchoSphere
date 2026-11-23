"""Pure authored workflow repairs from the later 23 November demo audit.

Apply after ``apply_call_audit_repair``.  This transform changes no service,
database, release, prompt record, or remote host.  Its explicit version marker
also keeps the earlier repair from being accidentally applied a second time.
"""
from __future__ import annotations

from copy import deepcopy
import re

from haier.setup.haier_call_audit_repair import (
    BOT_ID, WORKFLOW_ID, PREFIX as PREVIOUS_PREFIX, _ATTENTION, _BARE_NO,
    _BARE_YES, _DECLINED_SERVICE, _guard_specials, _literal_union, _patterns,
)


PREFIX = "n_haier_followup_"
VERSION = 1
_NEG = r"(?:nahi+n?|nahin|nhi|नहीं|नही|not)"
_NEED = r"(?:service|सर्विस|installation|इंस्टॉलेशन|requirement|रिक्वायरमेंट|zaroorat|zarurat|ज़रूरत|जरूरत)"
_QUESTION = r"(?:\?|\b(?:why|how|kya|kyun|kyon|kaise)\b|(?<![\w\u0900-\u097f])(?:क्यों|क्या|कैसे)(?![\w\u0900-\u097f]))"
_NO_NEED = [
    rf"(?<!\w){_NEED}(?:\s+(?:ki|की|to|तो|abhi|अभी)){{0,2}}\s+{_NEG}\s+(?:chahiye|चाहिए|चाहता|चाहती|needed|required|wanted)(?=$|[\s,.!।?])",
    *_DECLINED_SERVICE,
    rf"(?:mujhe|hamein|hume|hamko|मुझे|हमें|हमको)(?:\s+(?:ab|abhi|to|toh|अब|अभी|तो)){{0,3}}\s+(?:karani|karwani|karana|karwana|करानी|करवानी|कराना|करवाना)(?:\s+(?:hi|ही|to|तो)){{0,2}}\s+{_NEG}",
    rf"(?:karani|karwani|karana|karwana|करानी|करवानी|कराना|करवाना)(?:\s+(?:hi|ही))?\s+{_NEG}\s+(?:hai|है)",
    rf"{_NEG}\s+(?:chahiye|चाहिए)(?:\s+(?:haier|हायर|customer|कस्टमर|service|सर्विस|kuch|कुछ))",
    r"\b(?:do not|don't|dont|no longer)\s+want\s+(?:it|this|any help)\b",
]
_VISIT_PENDING = [
    rf"(?:visit|विजिट|विज़िट|installation|इंस्टॉलेशन|service|सर्विस)?\s*{_NEG}\s+(?:hui|hua|हुई|हुआ|ho\s+payi|हो\s+पाई)(?:\s+(?:hai|है|abhi|अभी|tak|तक))*",
    rf"(?:visit|विजिट|विज़िट)\s+(?:abhi\s+tak\s+|अभी\s+तक\s+)?{_NEG}",
]
_UNRESOLVED = [
    rf"{_NEG}\s+(?:hui|hua|हुई|हुआ|ho|हो)(?:\s+(?:hai|है))?\s+(?:theek|thik|ठीक|sahi|सही)",
    rf"(?:theek|thik|ठीक|sahi|सही|resolved|fixed)\s+{_NEG}",
    r"\b(?:still\s+(?:broken|not working|not fixed)|(?:not|never)\s+(?:fixed|resolved|working))\b",
    r"(?:अभी\s+भी|abhi\s+bhi)\s+(?:खराब|kharab|problem|प्रॉब्लम|दिक्कत)",
]
_RESOLVED = [
    r"(?:ठीक|सही|theek|thik|sahi)\s+(?:हो\s+(?:गया|गई|गए)|ho\s+(?:gaya|gayi|gaye))",
    r"\b(?:problem|issue)\s+(?:is\s+|has\s+been\s+)?(?:resolved|fixed)\b",
    r"\b(?:now\s+working|working\s+(?:fine|properly))\b",
]
_INSTALLATION_ONLY = [
    r"\b(?:no|not a|never any)\s+(?:problem|fault|defect)[^.!?।]*(?:installation|install)",
    r"\b(?:installation|install)\s+(?:was\s+|is\s+|only\s+)?(?:required|needed)\b",
    r"(?:problem|प्रॉब्लम|दिक्कत|खराबी|fault)\s+(?:नहीं|नही|nahi|nahin)[^.!?।]*(?:installation|इंस्टॉलेशन|इंस्टालेशन)",
    r"(?:सिर्फ|केवल|sirf|keval)\s+(?:installation|इंस्टॉलेशन|इंस्टालेशन)",
]
_PAID = [
    r"(?:payment|पेमेंट|पैसे|paise|पैसा|paisa)(?:\s+(?:उसका|uska|तो|to|पूरा|poora|भी|bhi|already))*\s+(?:हो\s*(?:गया|गई|चुका|चुकी)|ho\s*(?:gaya|gayi|chuka|chuki)|कर\s*दिया|kar\s*diya|दे\s*दिए|de\s*diye)",
    r"\b(?:already\s+paid|paid\s+already|(?:I|we)\s+(?:have\s+)?paid|payment\s+(?:is\s+)?(?:done|made|completed))\b",
    r"(?:पैसे|रुपये|रुपए|paise|rupaye|rupees|money|charges)\s+(?:ले\s*(?:गया|गए|गयी|लिए)|le\s+(?:gaya|gaye|liye)|paid|दे\s*(?:दिए|दिया)|de\s+(?:diye|diya))",
]
_PAYMENT_MENTION = r"(?<!\w)(?:payment|पेमेंट|पैसे|पैसा|paise|paisa|paid|charges?|चार्ज|रुपए|रुपये|rupaye|rupees|money)(?!\w)"
_WILL_PAY = [
    r"(?:payment|पेमेंट|पैसे|paise)[\s\S]*(?:मना\s+(?:क्यों|क्यूँ)\s+करेंगे|mana\s+ky(?:un|on)\s+karenge)",
    r"(?:payment|पेमेंट|पैसे|paise)\s+(?:दे\s*देंगे|देंगे|कर\s*देंगे|den(?:ge|gi)|kar\s+denge)",
    r"\b(?:will|would|can|shall|ready to|willing to)\s+pay\b",
    r"\b(?:did not|didn't|never)\s+refuse\s+(?:the\s+)?payment\b",
]
_PRIOR_VISIT = [
    r"(?:\d+|दस|बीस|das|bees|ten)\s+(?:दिन|din|days?)\s+(?:पहले|pehle|ago)[\s\S]*?(?:हुई\s+थी|हुआ\s+था|आए\s+थे|आया\s+था|hui\s+thi|hua\s+tha|aaye\s+the|aaya\s+tha)[\s\S]*?(?:उसके\s+बाद|uske\s+baad|since)[\s\S]*?(?:नहीं|nahi|not)",
    r"\b(?:visited|came)\b[^.!?।]*\b(?:ago|before|previously)\b[\s\S]*?\b(?:not\s+since|no\s+follow.?up)\b",
]
_DAY = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|सोमवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार|वेडनेसडे|वेडनसडे|बुध|somvaar|somwar|mangalwar|budhwar|guruwar|shukrawar|shaniwar|raviwar)"
_WEEKDAY_REQUEST = rf"(?<![\w\u0900-\u097f]){_DAY}(?![\w\u0900-\u097f])"
_DATED_TIMING = (rf"(?:{_WEEKDAY_REQUEST}|\b(?:next\s+(?:week|month)|(?:after|in)\s+(?:\d+|two|three|ten)\s+(?:days?|weeks?|months?))\b|"
                 r"(?:\d+|दो|तीन|दस|एक|do|teen|das|ek)\s+(?:दिन|din|हफ्ते|हफ़्ते|hafte|महीने|mahine)\s+(?:के\s+|ke\s+)?(?:बाद|baad)|"
                 r"(?:अगले|agle)\s+(?:हफ्ते|हफ़्ते|hafte|महीने|mahine))")
_REQUEST_ACTION = (r"(?:\b(?:need|require|want|send|arrange|visit|service|installation|karwa|karva|karani|karwani|bhej)\b|"
                   r"करवा|करवाइ|करा\s*(?:दो|दीजिए)|भेज|चाहिए|ज़रूरत|जरूरत|requirement)")
_DAY_VALID = rf"{_WEEKDAY_REQUEST}(?!\s*(?:(?:ko|को)\s+)?(?:{_NEG}|mat|मत))"
_DAY_NEGATION = r"(?:\b(?:don't|do not|cannot|can't|not available|mat)\b|मत|नहीं\s+चाहिए|नही\s+चाहिए|nahi\s+chahiye)"
_WEEKDAY_SERVICE_REQUEST = (rf"^(?![\s\S]*{_QUESTION})(?![\s\S]*{_DAY_NEGATION})"
                            rf"(?=[\s\S]*{_REQUEST_ACTION})(?=[\s\S]*{_DAY_VALID})[\s\S]+$")
_DATED_REFUSAL = (rf"{_WEEKDAY_REQUEST}\s*(?:(?:ko|को)\s+)?"
                  rf"(?:(?:{_NEED}|technician|टेक्नीशियन|visit|विजिट)\s+)?"
                  rf"(?:(?:ki|की)\s+)?(?:{_NEG}|mat|मत|don't|do\s+not)")


def _put(items: list[dict], value: dict) -> None:
    if any(item["id"] == value["id"] for item in items):
        raise ValueError(f"Unexpected existing follow-up node or edge: {value['id']}")
    items.append(value)


def _capture(variable: str, canonical: str, patterns: list[str]) -> dict:
    for pattern in patterns:
        re.compile(pattern, re.I)
    return {"variable": variable, "overwrite": True,
            "entity": {"dataType": "text", "matchCanonicalValues": False,
                       "synonymPatterns": {canonical: patterns}}}


def _layer(result: dict, hub: str, routes: list[tuple[str, str, list[str]]], fallback: str) -> None:
    """Add fresh turn-scoped choices, retaining the previous correction graph."""
    node = next((n for n in result["nodes"] if n["id"] == hub), None)
    if not node or node["kind"] != "intent":
        raise ValueError(f"Reviewed follow-up intent is missing: {hub}")
    config = node["config"]
    corrections = [e for e in result["edges"] if e["from"] == hub
                   and "correction" in re.split(r"[/,|]", e.get("label", ""))]
    if len(corrections) > 1:
        raise ValueError(f"Multiple correction contracts at {hub}")
    previous_scope = config.get("correctionCaptureVariable")
    specs = config.get("alsoCapture", [])
    old_patterns = _patterns([c for c in specs if not previous_scope or c.get("variable") == previous_scope])
    if corrections:
        if not old_patterns:
            raise ValueError(f"Previous correction has no reviewed fresh capture at {hub}")
        previous = ("previous", corrections[0]["to"], old_patterns)
    else:
        previous = None
    # A model's generic affirmation must not hide an authored warranty,
    # paid, opt-out, or future-date answer. Specific repairs above are already
    # guarded against these labels, except for explicitly reviewed exceptions.
    protected = [e for e in result["edges"] if e["from"] == hub
                 and ("e_v2_special_" in e["id"] or "e_warranty_" in e["id"])]
    targets = list(dict.fromkeys(e["to"] for e in protected))
    priority = {"m_n_v2_end_repeat_calls": 0, "m_warranty_claimed": 1}
    targets.sort(key=lambda target: priority.get(target, 2))
    routes = [*routes, *[(f"protected_{index}", target,
                         [_literal_union([e.get("label", "") for e in protected if e["to"] == target])])
                        for index, target in enumerate(targets)], *([previous] if previous else [])]
    variable = f"haier_followup_{hub}_route"
    # JSON arrays retain order even if a database canonicalizes object keys.
    config["alsoCapture"] = [*specs, *[_capture(variable, name, patterns)
                                       for name, _, patterns in reversed(routes)]]
    config["correctionCaptureVariable"] = variable
    first = f"{PREFIX}{hub}_0"
    if corrections:
        corrections[0]["to"] = first
    else:
        _put(result["edges"], {"id": f"e_{PREFIX}{hub}_correction", "from": hub,
                               "to": first, "label": "correction"})
    known = {n["id"] for n in result["nodes"]}
    for index, (name, target, _) in enumerate(routes):
        if target not in known:
            raise ValueError(f"Reviewed follow-up destination is missing: {target}")
        node_id = f"{PREFIX}{hub}_{index}"
        _put(result["nodes"], {"id": node_id, "kind": "condition", "label": f"Follow-up: {hub} / {name}",
                              "config": {"variable": variable, "operator": "equals", "value": name}})
        _put(result["edges"], {"id": f"e_{node_id}_true", "from": node_id, "to": target, "label": "true"})
        _put(result["edges"], {"id": f"e_{node_id}_false", "from": node_id,
                               "to": f"{PREFIX}{hub}_{index + 1}" if index + 1 < len(routes) else fallback,
                               "label": "false"})


def _protect(result: dict, hub: str, patterns: list[str], ignore: set[str] | None = None) -> list[str]:
    edges = []
    for edge in result["edges"]:
        if edge["from"] != hub:
            continue
        # Clarification clones retain their original special-case contracts.
        special = next((edge["id"].find(token) for token in ("e_v2_special_", "e_warranty_")
                        if token in edge["id"]), None)
        edges.append({**edge, "id": edge["id"][special:] if special is not None else edge["id"]})
    return _guard_specials(patterns, edges, ignore or set())


def _question_guard(patterns: list[str]) -> list[str]:
    return [rf"^(?![\s\S]*{_QUESTION})[\s\S]*(?:{pattern})" for pattern in patterns]


def _speech(node: dict, en: str, hi: str) -> None:
    node["config"].update(prompt=en, textByLanguage={"en": en, "hi": hi})


def _service_questions(result: dict) -> None:
    for base in ("n_service_need", "n_apology_call", "n_apology_visit", "n_apologize_no_visit"):
        for suffix in ("", "_clarify"):
            hub = base + suffix
            clarify = base + "_clarify" if not suffix else "m_n_v2_end_unverified"
            # A statement about a missed visit is not refusal of service.
            # Re-ask the service question without repeating the disputed fact.
            _layer(result, hub, [
                ("dated_refusal", "n_v2_future_when", _protect(result, hub, [_DATED_REFUSAL], {"n_future_confirm", "n_v2_future_when"})),
                ("no_service", "m_n_end_no_service", _protect(result, hub, _question_guard(_NO_NEED))),
                ("visit_pending", "n_service_need_clarify", _protect(result, hub, _VISIT_PENDING)),
                ("weekday", "n_redispatch", _protect(result, hub, [_WEEKDAY_SERVICE_REQUEST])),
                ("attention", clarify, list(_ATTENTION)),
            ], clarify)
            node = next(n for n in result["nodes"] if n["id"] == hub)
            node["config"]["alsoCapture"].append({
                "variable": "preferred_service_day", "overwrite": True,
                "entity": {"dataType": "text", "regexPattern": rf"(?={_WEEKDAY_SERVICE_REQUEST})[\s\S]*?({_DAY_VALID})"}})
            node["config"]["alsoCapture"].append({
                "variable": "preferred_service_timing", "overwrite": True,
                "entity": {"dataType": "text", "regexPattern": rf"^([\s\S]*{_DATED_TIMING}[\s\S]*)$"}})


def _status_questions(result: dict) -> None:
    for base in ("n_v2_paid_status_check", "n_reason_unit_ok_q"):
        for suffix in ("", "_clarify"):
            hub = base + suffix
            clarify = base + "_clarify" if not suffix else "m_n_v2_end_unverified"
            routes = [
                ("installation", "n_v2_no_response_status", _protect(result, hub, _INSTALLATION_ONLY)),
                ("unresolved", "n_service_need" if base == "n_v2_paid_status_check" else "n_unit_ok_pending_apology",
                 _protect(result, hub, _UNRESOLVED)),
            ]
            if base == "n_reason_unit_ok_q" and not suffix:
                _speech(next(n for n in result["nodes"] if n["id"] == hub),
                        "Does your [product_name] still have a problem?",
                        "क्या आपके [product_name] में अभी भी कोई problem है?")
                routes += [("problem_yes", "n_unit_ok_pending_apology", [_BARE_YES]),
                           ("problem_no", "n_resolved_how_q", [_BARE_NO])]
            if base == "n_v2_paid_status_check":
                _speech(next(n for n in result["nodes"] if n["id"] == hub),
                        "I understand that you have paid. Has the problem with your product been resolved now?",
                        "समझ गई, आपने payment कर दिया है। क्या आपके product की problem अब ठीक हो गई है?")
                # Explicit issue status wins over payment-only repetition.
                guard = r"^(?![\s\S]*(?:" + "|".join(_UNRESOLVED + _RESOLVED) + r"))[\s\S]*(?:"
                routes.append(("payment_only", base + "_clarify", _protect(result, hub, [guard + "|".join([*_PAID, _PAYMENT_MENTION]) + ")"])))
            _layer(result, hub, routes, clarify)
    for suffix in ("", "_clarify"):
        hub = "n_reason_refused_pay_q" + suffix
        clarify = "n_reason_refused_pay_q_clarify" if not suffix else "m_n_v2_end_unverified"
        _layer(result, hub, [("willing_to_pay", "n_service_need", _protect(result, hub, _WILL_PAY))], clarify)
    for suffix in ("", "_clarify"):
        hub = "n_intro_unit_ok" + suffix
        clarify = "n_intro_unit_ok_clarify" if not suffix else "m_n_v2_end_unverified"
        _layer(result, hub, [("prior_visit", "n_reason_unit_ok_q", _protect(result, hub, _PRIOR_VISIT,
                                                                         {"n_future_confirm", "n_v2_future_when"}))], clarify)


def _resolution_source(result: dict) -> None:
    """Unknown repair/completion source never means self-repair or cancellation."""
    for hub in ("n_resolved_how_q", "n_v2_completed_by_q"):
        node = next(n for n in result["nodes"] if n["id"] == hub)
        clarify = PREFIX + hub + "_clarify"
        config = deepcopy(node["config"])
        config.pop("alsoCapture", None)
        config.pop("correctionCaptureVariable", None)
        config["elseIsAnswer"] = False
        new = {"id": clarify, "kind": "intent", "label": "Clarify resolution source", "config": config}
        _put(result["nodes"], new)
        original = [e for e in result["edges"] if e["from"] == hub]
        for edge in original:
            unknown = edge.get("label") == "else" or "unknown" in edge["id"]
            if unknown:
                edge["to"] = clarify
            clone = {**deepcopy(edge), "id": "e_haier_followup_source_" + edge["id"], "from": clarify}
            if unknown:
                clone["to"] = "m_n_v2_end_unverified"
            _put(result["edges"], clone)
        for source, fallback in ((hub, clarify), (clarify, "m_n_v2_end_unverified")):
            # Bare polarity answers do not identify who performed the work.
            known_source = _literal_union([e.get("label", "") for e in result["edges"]
                                          if e["from"] == source and any(token in e["id"] for token in
                                               ("e_how_tech", "e_how_auto", "e_completed_by_haier", "e_completed_by_other"))])
            unclear = [rf"^(?![\s\S]*(?:{known_source}))[\s\S]*(?:{pattern})"
                       for pattern in [*_RESOLVED, _BARE_NO]] if known_source else [*_RESOLVED, _BARE_NO]
            other_source = [r"(?:\blocal\b|लोकल|बाहर\s+के|bahar\s+ke)\s+(?:technician|mechanic|टेक्नीशियन|मिस्त्री)",
                            r"\b(?:another|different|non.?haier)\s+(?:technician|mechanic)\b"]
            not_haier_source = [r"(?:haier|हायर|technician|टेक्नीशियन)[^.!?।]{0,45}(?:नहीं|नही|nahi|nahin|not)[^.!?।]{0,12}(?:किया|की|kiya|ki|done|fixed)",
                                rf"(?:haier|हायर|technician|टेक्नीशियन)[^.!?।]{{0,30}}(?:kiya|ki|किया|की)\s+(?:(?:hi|ही)\s+)?{_NEG}",
                                r"\b(?:not|wasn't|was not)\s+(?:by\s+)?(?:a\s+|the\s+)?haier\b"]
            positive_haier = r"(?:haier|हायर)\s+(?:(?:के|ke)\s+(?:technician|टेक्नीशियन)\s+)?(?:ने\s+(?:किया|की)|ne\s+(?:kiya|ki)|fixed|did)"
            not_self = [
                rf"^(?![\s\S]*(?:{positive_haier}))[\s\S]*(?:(?:khud|खुद)\s+{_NEG}\s+(?:kiya|ki|किया|की)|\b(?:did\s+not|didn't)\s+do\s+it\s+myself\b)",
            ]
            other_source = [p + rf"(?!\s+(?:(?:ne|ने)\s+)?(?:{_NEG}|(?:kiya|ki|किया|की)\s+(?:(?:hi|ही)\s+)?{_NEG}))"
                            for p in other_source]
            other_target = "m_n_end_resolved_auto" if hub == "n_resolved_how_q" else "m_n_v2_end_completed_other"
            _layer(result, source, [
                ("installation", "n_v2_no_response_status", _protect(result, source, _INSTALLATION_ONLY)),
                ("unresolved", "n_service_need", _protect(result, source, _UNRESOLVED)),
                ("payment_only", fallback, _protect(result, source, [rf"^(?![\s\S]*(?:{positive_haier}))[\s\S]*{_PAYMENT_MENTION}"])),
                ("not_self", fallback, _protect(result, source, not_self)),
                ("other_source", other_target, _protect(result, source, other_source)),
                ("not_haier", fallback, _protect(result, source, not_haier_source)),
                ("source_unknown", fallback, _protect(result, source, [
                    r"\b(?:not sure|don't know|do not know|pata nahi|pata nhi|malum nahi|yaad nahi)\b|पता\s+नहीं|मालूम\s+नहीं|याद\s+नहीं",
                    *_ATTENTION])),
                ("source_unclear", fallback, _protect(result, source, unclear)),
            ], fallback)
            next(n for n in result["nodes"] if n["id"] == source)["config"]["elseIsAnswer"] = False


def _identity(result: dict) -> None:
    gate = next(n for n in result["nodes"] if n["id"] == "n_identity_gate")
    patterns = gate["config"]["entity"]["synonymPatterns"]
    family = r"(?:wife|husband|spouse|mother|father|son|daughter|वाइफ|पत्नी|पति|माँ|मां|पिता|बेटा|बेटी)"
    other_person = (rf"(?:\b(?:I\s+am|I'm|main|mein)\b|मैं)[^.!?।]{{0,40}}"
                    rf"(?:unki|unka|uski|uska|उनकी|उनका|उसकी|उसका|his|her)\s+{family}"
                    rf"(?!\s+(?:nahi|nahin|नहीं|नही|not))")
    family_denied = rf"{family}\s+(?:nahi|nahin|नहीं|नही|not)"
    wrong_party = (
        r"\bwrong\s+(?:number|person)\b|(?:गलत|ग़लत)\s+(?:नंबर|व्यक्ति|आदमी)|"
        r"\b(?:not\s+(?:the\s+)?customer|not\s+the\s+(?:owner|person)|not\s+me)\b|"
        rf"(?:main|mai|mein|मैं|मै)[^.!?।]{{0,30}}(?:customer|कस्टमर|ग्राहक)\s+{_NEG}(?:\s+(?:hoon|hun|hu|हूँ|हूं))?|"
        rf"(?:{other_person})"
    )
    decline_patterns = [
        *[rf"^(?![\s\S]*(?:{_DATED_REFUSAL}))[\s\S]*(?:{p})" for p in _question_guard(_NO_NEED)],
        r"(?:नहीं\s+चाहिए|nahi\s+chahiye)[\s\S]*(?:नहीं\s+चाहिए|nahi\s+chahiye)[\s\S]*(?:ठीक\s+हो\s+गया|theek\s+ho\s+gaya)",
    ]
    # An explicitly identified third party cannot cancel the named customer's
    # request. They keep the existing wrong-party availability branch.
    decline_patterns = [rf"^(?![\s\S]*(?:{wrong_party}))[\s\S]*(?:{p})" for p in decline_patterns]
    decline_guard = "|".join(f"(?:{p})" for p in decline_patterns)
    patterns["no"] = [rf"^(?![\s\S]*(?:{family_denied}|{decline_guard}))[\s\S]*(?:{p})"
                      for p in [*patterns.get("no", []), other_person, wrong_party]]
    identity_question = rf"(?:{_QUESTION}|कहाँ|कहां|कौन|\b(?:kahan|kahaan|kaun|where|who)\b)"
    answer = r"(?:yes|yeah|yup|haan|han|ha|हाँ|हां|हा|हाँजी|हांजी|ji|jee|जी)"
    greeting = r"(?:hello|hallo|हेलो|हलो|हैलो|namaste|नमस्ते|namaskar|नमस्कार)"
    polite = r"(?:ji|jee|जी|sir|सर|mam|maam|madam|मैम|boliye|बोलिए|बोलिये)"
    separator = r"[\s,.!।:;—-]+"
    patterns["yes"] = [
        rf"^(?![\s\S]*{identity_question})\W*(?:{greeting}{separator})*(?:{answer})(?:{separator}(?:{answer}|{polite}))*\W*$",
        rf"^(?![\s\S]*{identity_question})\W*(?:(?:{answer}|{polite}){separator})*(?:main|mai|mein|मैं|मै)\s+(?:(?:hi|ही)\s+)?(?:hoon|hu|hun|हूँ|हूं|हु)\W*$",
        rf"^(?![\s\S]*{identity_question})\W*(?:(?:{answer}|{polite}){separator})*(?:main|mai|mein|मैं|मै)\s+(?:bol\s+rah[ai]\s+(?:hoon|hu|hun)|बोल\s+रह[ाी]\s+(?:हूँ|हूं))\W*$",
        r"^\W*(?:speaking|yes it is|this is (?:he|she|me)|it's me|that's me)\W*$",
    ]
    # Identity is not inferred. An explicit opt-out can be honoured without
    # disclosing the recorded complaint or insisting on the named person.
    patterns["service_declined"] = decline_patterns
    target = PREFIX + "identity_declined"
    edge = next(e for e in result["edges"] if e["id"] == "e_gate_next")
    original = edge["to"]
    if original != PREVIOUS_PREFIX + "identity_known":
        raise ValueError("The earlier identity safety repair is missing")
    edge["to"] = target
    _put(result["nodes"], {"id": target, "kind": "condition", "label": "Respect explicit service opt-out",
                          "config": {"variable": "identity_answer", "operator": "equals", "value": "service_declined"}})
    _put(result["edges"], {"id": f"e_{target}_true", "from": target, "to": "m_n_end_no_service", "label": "true"})
    _put(result["edges"], {"id": f"e_{target}_false", "from": target, "to": original, "label": "false"})
    gate["config"]["haierFollowupRepairVersion"] = VERSION


def _product_context(result: dict) -> None:
    start = next(n for n in result["nodes"] if n["kind"] == "start")
    outgoing = [e for e in result["edges"] if e["from"] == start["id"]]
    if len(outgoing) != 1:
        raise ValueError("The reviewed workflow must have one start destination")
    original = outgoing[0]["to"]
    prefill, check, fallback = PREFIX + "product_context", PREFIX + "product_exists", PREFIX + "product_fallback"
    outgoing[0]["to"] = prefill
    _put(result["nodes"], {"id": prefill, "kind": "ask", "label": "Read optional product metadata",
                          "config": {"variable": "product_name", "prefillFromContext": "product_name", "prefillOnly": True}})
    _put(result["edges"], {"id": f"e_{prefill}", "from": prefill, "to": check})
    _put(result["nodes"], {"id": check, "kind": "condition", "label": "Product metadata present",
                          "config": {"variable": "product_name", "operator": "exists"}})
    _put(result["edges"], {"id": f"e_{check}_missing", "from": check, "to": fallback, "label": "false"})
    sentinels = ("0", "unknown", "null", "none", "n/a", "na", "[product_name]")
    _put(result["edges"], {"id": f"e_{check}_present", "from": check, "to": PREFIX + "product_sentinel_0", "label": "true"})
    for index, value in enumerate(sentinels):
        node_id = PREFIX + f"product_sentinel_{index}"
        _put(result["nodes"], {"id": node_id, "kind": "condition", "label": "Reject product placeholder",
                              "config": {"variable": "product_name", "operator": "equals", "value": value}})
        _put(result["edges"], {"id": f"e_{node_id}_yes", "from": node_id, "to": fallback, "label": "true"})
        _put(result["edges"], {"id": f"e_{node_id}_no", "from": node_id,
                               "to": PREFIX + f"product_sentinel_{index + 1}" if index + 1 < len(sentinels) else original,
                               "label": "false"})
    _put(result["nodes"], {"id": fallback, "kind": "message", "label": "Use a neutral product description",
                          "config": {"silent": True, "setSlots": {"product_name": "Haier product"}}})
    _put(result["edges"], {"id": f"e_{fallback}", "from": fallback, "to": original})
    # Dialer JSON may encode the same missing product as 0, 0.0 or "0.00".
    next(n for n in result["nodes"] if n["id"] == PREFIX + "product_sentinel_0")["config"]["operator"] = "numeric_eq"


def _payment_facts(result: dict) -> None:
    for node in result["nodes"]:
        if node.get("kind") != "intent" or not node.get("config", {}).get("correctionCaptureVariable"):
            continue
        node.setdefault("config", {}).setdefault("alsoCapture", []).append(
            _capture("payment_status", "paid", _PAID))


def _preference_readback(node: dict) -> None:
    config = node["config"]
    config["readback"] = {
        "en": {"groups": [
            {"requires": ["preferred_service_day"],
             "template": "Alright, I have noted that you prefer a visit on {preferred_service_day}. May I forward your request for a technician visit?"},
            {"absent": ["preferred_service_day"], "template": config["textByLanguage"]["en"]},
        ]},
        "hi": {"groups": [
            {"requires": ["preferred_service_day"],
             "template": "ठीक है, मैंने {preferred_service_day} के लिए आपकी preference नोट कर ली है। क्या technician की visit के लिए मैं आपकी request आगे बढ़ा दूँ?"},
            {"absent": ["preferred_service_day"], "template": config["textByLanguage"]["hi"]},
        ]},
    }


def _natural_consent(result: dict) -> None:
    prefix = r"(?:(?:हाँ|हां|haan|han|yes|जी|ji|okay|ok|ठीक\s+है|theek\s+hai|please|प्लीज)[\s,.!।]+)*"
    command = (
        r"(?:(?:technician|टेक्नीशियन)\s+(?:को\s+)?(?:भेज\s*(?:दो|दीजिए|दिजिए)|bhej\s+(?:do|dijiye))|"
        r"(?:send|please\s+send)\s+(?:a\s+|the\s+)?technician|"
        r"request\s+(?:को\s+)?(?:आगे\s+)?(?:बढ़ा|भेज)\s*(?:दो|दीजिए|दिजिए)|"
        r"request\s+(?:ko\s+)?(?:aage\s+)?(?:badha|bhej)\s+(?:do|dijiye)|"
        r"(?:भेज\s*(?:दो|दीजिए|दिजिए)|bhej\s+(?:do|dijiye)))"
    )
    negative = rf"(?<!\w)(?:{_NEG}|no|mat|मत|don't|do\s+not)(?!\w)"
    consent = rf"^(?![\s\S]*(?:\?|{negative}))\W*{prefix}{command}(?:\s+(?:जी|ji|please|प्लीज))?\W*$"
    for hub in ("n_redispatch", PREVIOUS_PREFIX + "redispatch_confirm"):
        fallback = PREVIOUS_PREFIX + "redispatch_confirm" if hub == "n_redispatch" else "m_n_v2_end_unverified"
        _layer(result, hub, [("consent_command", "m_n_end_redispatch_thanks", _protect(result, hub, [consent]))], fallback)


def _product_readbacks(result: dict) -> None:
    # The voice renderer substitutes square-bracket context placeholders.
    # Use the existing deterministic readback contract for this one authored
    # display value, so the sanitized slot wins over raw dialer metadata.
    for node in result["nodes"]:
        config = node.get("config", {})
        templates = config.get("textByLanguage") or {}
        if not any("[product_name]" in str(text) for text in templates.values()):
            continue
        if config.get("readback"):
            raise ValueError(f"Review existing product readback before editing: {node['id']}")
        config["readback"] = {
            lang: {"groups": [
                {"requires": ["product_name"], "template": text.replace("[product_name]", "{product_name}")},
                {"absent": ["product_name"], "template": text},
            ]}
            for lang, text in templates.items()
        }


def apply_followup_audit_repair(definition: dict) -> dict:
    """Return the reviewed, already repaired Haier workflow with later fixes."""
    if definition.get("id") != WORKFLOW_ID or definition.get("bot_id", definition.get("botId", BOT_ID)) != BOT_ID:
        raise ValueError("This repair is restricted to the reviewed Haier workflow")
    gate = next((n for n in definition.get("nodes", []) if n["id"] == "n_identity_gate"), None)
    if not gate:
        raise ValueError("Reviewed identity gate is missing")
    if gate.get("config", {}).get("haierFollowupRepairVersion") == VERSION:
        return deepcopy(definition)
    if not any(n["id"] == PREVIOUS_PREFIX + "identity_known" for n in definition.get("nodes", [])):
        raise ValueError("Apply the earlier call-audit repair first")
    result = deepcopy(definition)
    _service_questions(result)
    _status_questions(result)
    _resolution_source(result)
    _identity(result)
    _product_context(result)
    _natural_consent(result)
    _payment_facts(result)
    for node in result["nodes"]:
        if node.get("kind") == "message" and "outcome" in node.get("config", {}).get("setSlots", {}):
            node["config"]["terminalOutcome"] = True
        if node["id"] in ("n_redispatch", PREVIOUS_PREFIX + "redispatch_confirm"):
            _speech(node, "Alright, may I forward your request for a technician visit?",
                    "ठीक है, क्या technician की visit के लिए मैं आपकी request आगे बढ़ा दूँ?")
            _preference_readback(node)
    _product_readbacks(result)
    return result
