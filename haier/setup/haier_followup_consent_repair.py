"""Pure incremental repair of the reviewed Haier visit-permission workflow.

Apply after follow-up repair version 1. The transform only updates authored
workflow configuration; callers handle any separately authorized publication.
"""
from __future__ import annotations

from copy import deepcopy
import re

from haier.setup.haier_call_audit_repair import BOT_ID, WORKFLOW_ID
from haier.setup.haier_followup_audit_repair import _protect


VERSION = 4
MARKER = "haierFollowupConsentRepairVersion"
HUBS = ("n_redispatch", "n_haier_audit_redispatch_confirm")
_CLARIFY = {
    "en": "May I forward the technician visit request?",
    "hi": "क्या technician की visit वाली request आगे forward कर दूँ?",
}
_UNVERIFIED = {
    "en": "I am sorry, we could not complete the confirmation on this call. Thank you for giving your valuable time to Haier. Have a good day.",
    "hi": "माफ़ी चाहूँगी, इस call पर जानकारी पूरी तरह confirm नहीं हो पाई। Haier को अपना कीमती समय देने के लिए धन्यवाद, आपका दिन शुभ रहे।",
}
_OLD_WORD_BOUNDARY = r"[\w\u0900-\u097f]"
_WORD_BOUNDARY = r"[\w\u0900-\u0963\u0966-\u097f]"


def _repair_service_decline_boundaries(definition: dict) -> None:
    """Treat Hindi danda as punctuation in the existing no-service route.

    U+0964/U+0965 belong to the Devanagari block but are not word characters.
    The old token guard rejected a complete refusal ending in either mark.
    Only the two reviewed permission hubs' existing refusal captures change.
    """
    nodes = {node["id"]: node for node in definition["nodes"]}
    for hub in HUBS:
        node = nodes.get(hub, {})
        config = node.get("config", {})
        variable = f"haier_audit_{hub}_route"
        captures = [capture for capture in config.get("alsoCapture", [])
                    if capture.get("variable") == variable
                    and "no_service" in capture.get("entity", {}).get("synonymPatterns", {})]
        if node.get("kind") != "intent" or len(captures) != 1:
            raise ValueError(f"Reviewed scoped no-service route is missing: {hub}")
        patterns = captures[0]["entity"]["synonymPatterns"]["no_service"]
        if not isinstance(patterns, list) or not all(isinstance(pattern, str) for pattern in patterns):
            raise ValueError(f"Reviewed no-service patterns are missing: {hub}")
        if sum(_OLD_WORD_BOUNDARY in pattern for pattern in patterns) != 2:
            raise ValueError(f"Review the existing no-service boundary before editing: {hub}")
        repaired = [pattern.replace(_OLD_WORD_BOUNDARY, _WORD_BOUNDARY) for pattern in patterns]
        for pattern in repaired:
            re.compile(pattern, re.I)
        captures[0]["entity"]["synonymPatterns"]["no_service"] = repaired


def _forward_permission() -> str:
    # Match a complete imperative in the permission-question context. An
    # affirmative about a visit, payment or repair is not this command.
    prefix = r"(?:(?:हाँ|हां|haan|han|yes|जी|ji|jee|okay|ok|ठीक\s+है|theek\s+hai|please|प्लीज)[\s,.!।]+)*"
    subject = r"(?:(?:(?:मेरी|यह|ये|इस|meri|yeh|ye|is|my|this)\s+)?(?:request|रिक्वेस्ट|अनुरोध)\s+(?:(?:को|ko)\s+)?)?"
    direction = r"(?:(?:आगे|aage|age)\s+)?"
    forward = r"(?:बढ़ा|बढा|badhaa?)"
    imperative = r"(?:दो|दे\s*दो|देना|दें|दीजिए|दीजिये|दिजिए|do|de\s+do|dena|dein|dijiye|dijiyega)"
    send = rf"(?:भेज\s*{imperative}|bhej\s+{imperative})"
    command = (
        rf"(?:{forward}\s*{imperative}|बढ़ाइए|बढाइए|badhaiye|"
        rf"(?:बढ़ा\s*कर|बढा\s*कर|badhaa?\s*kar)\s+(?:{send}|{imperative})|"
        rf"(?:forward|forword|फॉरवर्ड|फ़ॉरवर्ड)\s+(?:कर\s*{imperative}|kar\s+{imperative}|करें|करिए|करिये|karein|kariye))"
    )
    negation = r"(?<![\w\u0900-\u097f])(?:नहीं|नही|ना|मत|nahi+n?|nahin|nhi|na|mat|no|not|never|don't|do\s+not)(?![\w\u0900-\u097f])"
    question = r"(?:[?？]|(?<![\w\u0900-\u097f])(?:क्या|क्यों|कैसे|कब|kya|kyun|kyon|kaise|kab|why|how|when|can\s+you|could\s+you|will\s+you)(?![\w\u0900-\u097f]))"
    return rf"^(?![\s\S]*(?:{negation}|{question}))\W*{prefix}{subject}{direction}{command}(?:\s+(?:जी|ji|please|प्लीज))?\W*$"


def _elided_permission() -> str:
    """Accept a complete 'do it' command only at the visit-permission hubs.

    The immediately preceding authored question supplies the omitted action.
    Anchoring keeps reports such as 'payment kar diya' and additional business
    facts out of this consent route, even if a model labels them affirmative.
    """
    prefix = r"(?:(?:हाँ|हां|haan|han|yes|जी|ji|jee|okay|ok|ठीक\s+है|theek\s+hai|please|प्लीज)[\s,.!।]+)*"
    subject = r"(?:(?:आप|aap)\s+)?"
    imperative = r"(?:दो|दे\s*दो|देना|दें|दीजिए|दीजिये|दिजिए|do|de\s+do|dena|dein|dijiye|dijiyega)"
    command = (rf"(?:कर\s*{imperative}|kar\s+{imperative}|कीजिए|कीजिये|किजिए|"
               rf"कीजिएगा|कीजियेगा|kiijiye|kijiye|kiijiyega|kijiyega|keejiye)")
    negation = r"(?<![\w\u0900-\u097f])(?:नहीं|नही|ना|मत|nahi+n?|nahin|nhi|na|mat|no|not|never|don't|do\s+not)(?![\w\u0900-\u097f])"
    question = r"(?:[?？]|(?<![\w\u0900-\u097f])(?:क्या|क्यों|कैसे|कब|kya|kyun|kyon|kaise|kab|why|how|when|can\s+you|could\s+you|will\s+you)(?![\w\u0900-\u097f]))"
    return rf"^(?![\s\S]*(?:{negation}|{question}))\W*{prefix}{subject}{command}(?:\s+(?:जी|ji|please|प्लीज))?\W*$"


def _forward_wording(value):
    """Update authored speech variants without changing readback conditions."""
    if isinstance(value, str):
        return value.replace("आगे बढ़ा दूँ", "आगे forward कर दूँ").replace(
            "May I go ahead with the technician visit request?", _CLARIFY["en"])
    if isinstance(value, dict):
        return {key: _forward_wording(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_forward_wording(child) for child in value]
    return value


def apply_followup_consent_repair(definition: dict) -> dict:
    """Return a repaired copy, rejecting unreviewed workflow contracts."""
    if definition.get("id") != WORKFLOW_ID or definition.get("bot_id", definition.get("botId")) != BOT_ID:
        raise ValueError("This repair is restricted to the reviewed Haier workflow")
    nodes = {node["id"]: node for node in definition.get("nodes", [])}
    gate = nodes.get("n_identity_gate", {}).get("config", {})
    if gate.get("haierFollowupRepairVersion") != 1:
        raise ValueError("Apply reviewed Haier follow-up repair version 1 first")
    if gate.get(MARKER) == VERSION:
        return deepcopy(definition)
    previous_version = gate.get(MARKER)
    if previous_version not in (None, 1, 2, 3):
        raise ValueError("Review the existing consent repair version before editing")
    closing = nodes.get("n_v2_end_unverified")
    outcome = nodes.get("m_n_v2_end_unverified", {}).get("config", {})
    if not closing or closing.get("kind") != "end" or outcome.get("setSlots", {}).get("outcome") != "unverified_no_action":
        raise ValueError("Reviewed unverified outcome contract is missing")
    result = deepcopy(definition)
    nodes = {node["id"]: node for node in result["nodes"]}
    for hub in HUBS:
        node = nodes.get(hub)
        if not node or node.get("kind") != "intent":
            raise ValueError(f"Reviewed permission question is missing: {hub}")
        config = node["config"]
        variable = config.get("correctionCaptureVariable")
        captures = [capture for capture in config.get("alsoCapture", [])
                    if capture.get("variable") == variable
                    and "consent_command" in capture.get("entity", {}).get("synonymPatterns", {})]
        conditions = [candidate for candidate in result["nodes"]
                      if candidate.get("kind") == "condition"
                      and candidate.get("config", {}).get("variable") == variable
                      and candidate["config"].get("operator") == "equals"
                      and candidate["config"].get("value") == "consent_command"]
        if len(captures) != 1 or len(conditions) != 1 or not any(
            edge.get("from") == conditions[0]["id"] and edge.get("label") == "true"
            and edge.get("to") == "m_n_end_redispatch_thanks" for edge in result["edges"]
        ):
            raise ValueError(f"Reviewed scoped consent route is missing: {hub}")
        patterns = _protect(result, hub, [_forward_permission(), _elided_permission()])
        for pattern in patterns:
            re.compile(pattern, re.I)
        existing = captures[0]["entity"]["synonymPatterns"]["consent_command"]
        existing.extend(pattern for pattern in patterns if pattern not in existing)
        # Readbacks and authored resume prompts can otherwise keep the old
        # question after a date preference or an interruption.
        if previous_version != 3:
            for field in ("prompt", "text", "textByLanguage", "readback", "resumePrompt", "resumePromptByLanguage"):
                if field in config:
                    config[field] = _forward_wording(config[field])
    if previous_version in (1, 2, 3):
        if previous_version == 1:
            _repair_service_decline_boundaries(result)
        nodes["n_identity_gate"]["config"][MARKER] = VERSION
        return result
    clarify = nodes[HUBS[1]]["config"]
    if set(clarify.get("readback", {})) != {"en", "hi"}:
        raise ValueError("Reviewed bilingual permission readback is missing")
    clarify.update(prompt=_CLARIFY["en"], textByLanguage=deepcopy(_CLARIFY))
    # Keep date preference as a preference. No appointment is promised.
    clarify["readback"] = {
        "en": {"groups": [
            {"requires": ["preferred_service_day"], "template": f"I have noted your preference for {{preferred_service_day}}. {_CLARIFY['en']}"},
            {"absent": ["preferred_service_day"], "template": _CLARIFY["en"]},
        ]},
        "hi": {"groups": [
            {"requires": ["preferred_service_day"], "template": f"मैंने {{preferred_service_day}} की आपकी preference नोट कर ली है। {_CLARIFY['hi']}"},
            {"absent": ["preferred_service_day"], "template": _CLARIFY["hi"]},
        ]},
    }
    nodes["n_v2_end_unverified"]["config"].update(
        text=_UNVERIFIED["en"], textByLanguage=deepcopy(_UNVERIFIED), responseMode="fixed",
        responseDirective="Speak this fixed closing in the chosen language. Do not add a customer-care referral or claim that a request, cancellation, technician visit or callback was arranged. The confirmation remains incomplete.",
    )
    _repair_service_decline_boundaries(result)
    nodes["n_identity_gate"]["config"][MARKER] = VERSION
    return result
