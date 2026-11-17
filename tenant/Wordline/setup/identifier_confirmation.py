"""Configuration-only TID confirmation for runtimes predating the platform gate.

The transform adds a strict confirmation before each merchant lookup. It can
be tested with the unchanged demo runtime; no platform files need deploying.
"""
from __future__ import annotations

import copy
import re

from shared.orchestration.identifier_confirmation import spoken_readback

ASKS = ("n_ask_tid", "n_tid_retry", "n_bat_tid", "n_bat_tid_retry")
TID_PATTERN = (r"(?<![A-Za-z0-9])((?:(?:[A-Za-z]|ए|एम)(?:\s+for\s+[A-Za-z]+)?[\s-]*)?"
               r"\d(?:[\s-]?\d){6,7}(?:[\s-]*(?:[A-Za-z]|ए|एम))?)(?![A-Za-z0-9])")
YES = (r"(?i)^\s*(?=.*(?:yes|yeah|yep|correct|right|confirmed|absolutely|haan|han|sahi|हाँ|हां|हा|जी|सही))"
       r"(?:(?:yes|yeah|yep|correct|right|confirmed|absolutely|okay|ok|it['’]s|that['’]s|"
       r"it|is|that|haan|han|haa|ha|haanji|hanji|ji|sahi|hai|hain|bilkul|sir|madam|"
       r"हाँ|हां|हा|हाँजी|हांजी|जी|सही|है|बिल्कुल|सर)[\s,.!।]*)+$")
NO = (r"(?i)(?<!\w)(?:no|not|wrong|incorrect|nahi|nahin|nhi|galat|but|actually|last|"
      r"नहीं|नही|गलत|ग़लत|लेकिन|आखिरी|आख़िरी)(?!\w)|\d{4,}")


def repair_confirmation_nodes(definition: dict) -> dict:
    """Refresh only the four existing confirmation gates, without rewiring.

    This pure transform also repairs documents produced by an older upgrade;
    it preserves node IDs, positions, captures, unknown settings and edge
    precedence. Applying it to a live configuration is a separate operation.
    """
    result = copy.deepcopy(definition)
    by_id = {node["id"]: node for node in result["nodes"]}
    for ask_id in ASKS:
        capture = by_id.get(ask_id, {}).get("config", {})
        for key in ("question", "prompt", "text"):
            if isinstance(capture.get(key), str):
                capture[key] = re.sub(r"\bA for alpha\b", "A for Apple", capture[key], flags=re.I)
        for locale, text in (capture.get("textByLanguage") or {}).items():
            if isinstance(text, str):
                capture["textByLanguage"][locale] = re.sub(r"\bA for alpha\b", "A for Apple", text, flags=re.I)
        confirmation = by_id.get(capture.get("identifierConfirmationNode"), {})
        config = confirmation.get("config", {})
        if confirmation.get("kind") != "ask" or config.get("confirmsIdentifier") != capture.get("variable"):
            raise ValueError(f"Expected an existing confirmation gate for {ask_id}")
        entity = config.setdefault("entity", {})
        patterns = entity.setdefault("synonymPatterns", {})
        patterns.update(yes=[YES], no=[NO])
        # Keep the complete spoken identifier; shorten only the trailing
        # question so callers can answer before a long instruction finishes.
        for locale, question in (("en", "Is that correct?"), ("hi", "क्या यह सही है?")):
            localized = (config.get("readback") or {}).get(locale)
            if isinstance(localized, dict):
                localized["question"] = question
    return result


def upgrade(definition: dict, terminal_ids: list[str] = ()) -> dict:
    result = copy.deepcopy(definition)
    nodes, edges = result["nodes"], result["edges"]
    by_id = {n["id"]: n for n in nodes}
    if all(by_id.get(n, {}).get("config", {}).get("identifierConfirmationNode") for n in ASKS):
        return repair_confirmation_nodes(result)
    if not all(n in by_id for n in ASKS):
        raise ValueError("Expected all four Wordline TID collection nodes")
    abort = "n_identifier_unconfirmed"
    if abort in by_id:
        raise ValueError("Confirmation rollout is only partially present")
    nodes.append({"id": abort, "kind": "end", "label": "Identifier not confirmed", "config": {
        "text": "I could not confirm your Terminal ID. Please check the complete ID and contact us again. Thank you.",
        "responseMode": "fixed", "textByLanguage": {
            "en": "I could not confirm your Terminal ID. Please check the complete ID and contact us again. Thank you.",
            "hi": "आपकी Terminal ID confirm नहीं हो पाई। कृपया पूरी ID check करके दोबारा संपर्क कीजिए। धन्यवाद।"}}})
    for ask_id in ASKS:
        node = by_id[ask_id]
        config = node.setdefault("config", {})
        variable = config["variable"]
        confirmation_id, condition_id, reset_id = (ask_id + "__" + x for x in ("confirm", "confirmed", "reset"))
        answer = ask_id + "__confirmation_answer"
        normal = [e for e in edges if e.get("from") == ask_id and e.get("label", "").lower() not in ("fallback", "failure", "error")]
        if len(normal) != 1:
            raise ValueError(f"Expected one collection-success edge for {ask_id}")
        bridge = normal[0]
        following = by_id.get(bridge["to"], {})
        # Preserve the existing "I do not have a TID" exit before readback.
        if (following.get("kind") == "condition"
                and following.get("config", {}).get("variable") == variable
                and following.get("config", {}).get("value") == "no_tid"):
            bridge = next(e for e in edges if e.get("from") == following["id"]
                          and e.get("label") == "false")
        target = bridge["to"]
        bridge["to"] = confirmation_id
        config["entity"] = {**config.get("entity", {}), "dataType": "identifier",
                            "preserveAlphanumeric": True, "regexPattern": TID_PATTERN}
        config["identifierConfirmationNode"] = confirmation_id
        if "retry" not in ask_id:
            question = "Please tell me your complete Terminal ID, including any letters. Say letters as A for Apple, for example."
            hindi = "कृपया अपनी पूरी Terminal ID, उसमें आने वाले letters के साथ बताइए। Letter हो तो A for Apple जैसे बोलिए।"
        else:
            question = "No matching terminal record was found. Please repeat the complete Terminal ID slowly, including any letters."
            hindi = "इस ID से terminal record नहीं मिला। कृपया पूरी Terminal ID letters के साथ धीरे-धीरे दोबारा बताइए।"
        config.update(question=question, responseMode="fixed", textByLanguage={"en": question, "hi": hindi})
        values = {}
        for tid in terminal_ids:
            digits = re.sub(r"\D", "", tid)
            variants = {tid, digits, " ".join(tid), " ".join(digits)}
            if tid.endswith("A"):
                variants.update({digits + " ए", digits + "A", digits + "8"})
            if tid.startswith("A"):
                variants.add("8" + digits)
            for variant in variants:
                # Speak only what was captured, including a wrong eight.
                display = variant.replace("ए", "A").replace(" ", "")
                values[variant] = "Terminal ID " + spoken_readback(display) + "."
        readback = {locale: {
            "fields": [{"variable": variable, "template": "Terminal ID {value}.", "values": values}],
            "question": ("Is that correct? Please say yes or no, or repeat the complete corrected ID."
                         if locale == "en" else "क्या यह सही है? कृपया हाँ या ना बोलिए, या पूरी सही ID दोबारा बताइए।")
        } for locale in ("en", "hi")}
        nodes.extend([
            {"id": confirmation_id, "kind": "ask", "label": "Confirm captured Terminal ID", "config": {
                "variable": answer, "confirmsIdentifier": variable, "responseMode": "fixed",
                "question": "Is the Terminal ID I read back correct? Please say yes or no.",
                "textByLanguage": {"en": "Is the Terminal ID I read back correct? Please say yes or no.",
                                   "hi": "क्या बताई गई Terminal ID सही है? कृपया हाँ या ना बोलिए।"},
                "entity": {"dataType": "text", "synonymPatterns": {"yes": [YES], "no": [NO]}},
                "readback": readback,
                "unmatchedReply": "Please say yes if that ID is correct, or repeat the complete corrected ID.",
                "unmatchedReplyByLanguage": {"en": "Please say yes if that ID is correct, or repeat the complete corrected ID.",
                                              "hi": "ID सही हो तो हाँ बोलिए, वरना पूरी सही ID दोबारा बताइए।"},
                "alsoCapture": [
                    {"variable": variable, "clear": True,
                     "entity": {"dataType": "text", "synonymPatterns": {"clear": [NO]}}},
                    {"variable": variable, "overwrite": True, "entity": copy.deepcopy(config["entity"])},
                ]}},
            {"id": condition_id, "kind": "condition", "label": "Caller confirmed ID?", "config": {
                "variable": answer, "operator": "equals", "value": "yes"}},
            {"id": reset_id, "kind": "message", "label": "", "config": {
                "setSlots": {answer: None}, "text": "", "responseMode": "fixed"}},
        ])
        edges.extend([
            {"id": confirmation_id + "__answer", "from": confirmation_id, "to": condition_id},
            {"id": confirmation_id + "__fallback", "from": confirmation_id, "to": abort, "label": "fallback"},
            {"id": condition_id + "__yes", "from": condition_id, "to": target, "label": "true"},
            {"id": condition_id + "__no", "from": condition_id, "to": reset_id, "label": "false"},
            {"id": reset_id + "__collect", "from": reset_id, "to": ask_id},
        ])
    return repair_confirmation_nodes(result)


PROMPT_APPENDIX = """\n\n# IDENTIFIER AND PHONE NUMBER CONFIRMATION
The workflow now reads back each caller-provided Terminal ID and asks for confirmation BEFORE
the merchant lookup. Keep every digit and letter, including leading zeroes. Read digits
individually and distinguish letters explicitly, such as letter A. Never replace 8 with A
or A with 8 based on a guess. A successful merchant lookup does not replace this confirmation.
Proceed only after an explicit affirmative reply. A rejection, a different ID, or a correction
requires another confirmation. A clear letter suffix may amend the captured value only when
the complete amended ID satisfies its configured format; otherwise request the complete ID.
Never treat a suffix correction as confirmation. Unclear answers and questions do not confirm the ID. The same rule applies
when collecting a new phone number. Do not add another confirmation when the workflow already
asked it or when verifying an unchanged, already confirmed value. Let the workflow control
lookup, retry and progression; never claim those steps completed while confirmation is pending.
"""
