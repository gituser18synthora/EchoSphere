"""Pure configuration repair for the November 17 merchant-support call audit.

This module never connects to a service or saves a document. The caller must
review the returned payload and apply it through the platform's versioned API.
"""
from __future__ import annotations

import copy

from tenant.Wordline.setup.identifier_confirmation import repair_confirmation_nodes


OPTIONS = {
    "en": "Sir, would you prefer SMS instructions, IVR instructions, or should I raise the paper roll request for you?",
    "hi": "Sir, paper roll के लिए SMS का तरीका बताऊँ, IVR का तरीका बताऊँ, या आपकी तरफ़ से request raise करूँ?",
}

CONFIRMATION_HEADING = "# IDENTIFIER AND PHONE NUMBER CONFIRMATION"
CONFIRMATION_INSTRUCTIONS = """# IDENTIFIER AND PHONE NUMBER CONFIRMATION
Collect the complete caller-provided identifier or phone number in the format required by
the current workflow. Preserve leading zeroes, every digit and every letter. Never replace
A with 8, B with another sound-alike, or silently discard an uncertain character.
Wait while the caller continues dictating digits or spelling a letter. Phrases such as
A for Apple or B for Boy describe the identifier; they do not request a language change.
Keep the established conversation language unless the caller actually changes language.
Read the complete value back digit by digit, name letters explicitly, and ask one short
confirmation question. Natural clear affirmatives such as Yes, It's correct, Okay yes it's
correct, and Haan sahi hai can confirm the current unchanged value. A question, negation,
conflicting value or correction never confirms it, even when it contains the word yes.
An unambiguous suffix continuation may amend a candidate only after workflow validation.
Read the complete amended value back and obtain a fresh confirmation before proceeding.
If a correction is ambiguous or the resulting value fails validation, ask for the complete
corrected value. Never invent missing digits or guess a letter from merchant records.
The workflow controls confirmation, lookup, retry and progression. Do not call a lookup or
claim verification/request completion while confirmation is pending. Do not add a second
confirmation for an unchanged value the workflow has already confirmed. Apply the same
rule when collecting a new phone, booking, complaint or other reference number.
Whenever generating a reply that reads an ID, reference, docket, phone number or SMS
destination, say every digit separately in English, including in Hindi replies: zero, one,
two, three, four, five, six, seven, eight, nine. Name alphabetic characters explicitly,
for example letter A. Keep leading zeroes. This rule does not change the pronunciation
of amounts, quantities, percentages, durations or terminal model numbers such as Linura 7220.
"""


DOCKET_SLOT = "ctx_docket_number"


def repair_workflow(definition: dict) -> dict:
    result = repair_confirmation_nodes(definition)
    for node in result["nodes"]:
        if node.get("id") in {"n_options", "n_options_unverified", "n_pr_again"}:
            config = node.setdefault("config", {})
            config["prompt"] = OPTIONS["en"]
            config["textByLanguage"] = {**config.get("textByLanguage", {}), **OPTIONS}
        if node.get("id") in {"n_sms", "n_both"}:
            config = node.get("config", {})
            for key in ("text", "prompt"):
                if isinstance(config.get(key), str):
                    config[key] = config[key].replace("575758", "five, seven, five, seven, five, eight")
            config["textByLanguage"] = {
                locale: text.replace("575758", "five, seven, five, seven, five, eight")
                for locale, text in config.get("textByLanguage", {}).items()
            }
    return result


def stage_dynamic_readbacks(definition: dict) -> dict:
    """Ready after code push: explicit slot formatting unsupported by old code."""
    result = copy.deepcopy(definition)
    for node in result["nodes"]:
        config = node.get("config", {})
        variable = config.get("confirmsIdentifier")
        readback = config.get("readback") or {}
        if variable:
            for localized in readback.values():
                for field in localized.get("fields") or []:
                    if field.get("variable") == variable:
                        field["format"] = "identifier"
                        field.pop("values", None)  # Runtime formatter replaces the finite lookup table.
        if node.get("id") in {"n_bat_contact", "n_bat_visit_contact", "n_pr_visit_contact"}:
            for localized in readback.values():
                localized["formats"] = {**localized.get("formats", {}), "contact_number": "phone"}
        if node.get("id") in {"n_bat_visit_record", "n_pr_visit_record"}:
            for localized in readback.values():
                localized["formats"] = {**localized.get("formats", {}), "terminal_id": "identifier"}
        if node.get("id") in {"n_bat_docket", "n_low_docket"}:
            # The docket reaches the workflow as the prefilled slot
            # ``ctx_docket_number`` (n_bat_docket_read / n_low_docket_read,
            # ``prefillFromContext: docket_number``); ``docket_number`` itself
            # is only a call-context placeholder the runtime fills after the
            # engine. A group requiring that name never applied, so the whole
            # docket message was dropped from the reply (offline replay of the
            # live v54 definition, 2026-11-17).
            config["readback"] = {
                locale: {"formats": {DOCKET_SLOT: "identifier"}, "groups": [
                    {"requires": [DOCKET_SLOT],
                     "template": text.replace("{docket_number}", "{" + DOCKET_SLOT + "}")},
                ]}
                for locale, text in config.get("textByLanguage", {}).items()
            }
    return result


def repair_prompt(full_prompt: str) -> str:
    """Replace the existing audit appendix once and clarify verification scope."""
    old = ("After Terminal ID lookup, the flow confirms outlet name, bank, terminal model "
           "and registered contact number individually.")
    new = ("After Terminal ID lookup, follow the workflow's field-by-field verification. "
           "Only describe details actually confirmed by the caller as verified. "
           "Confirm the contact number when the workflow requests it before a request or visit.")
    text = full_prompt.replace(old, new)
    if text.count(CONFIRMATION_HEADING) != 1:
        raise ValueError("Expected exactly one existing identifier-confirmation appendix")
    before, section = text.split(CONFIRMATION_HEADING, 1)
    following = section.find("\n# ")
    remainder = section[following + 1:] if following >= 0 else ""
    return before.rstrip() + "\n\n" + CONFIRMATION_INSTRUCTIONS + ("\n" + remainder if remainder else "")


# ── 17 November evening review (25 calls after 13:11 UTC, workflow v54) ─────
# Pure transforms again: nothing here connects to a service.

MORE_HELP_ID = "n_more_help"
MORE_HELP = {
    "en": "Sure Sir, please tell me what else I can help you with.",
    "hi": "जी Sir, बताइए, मैं आपकी और किस चीज़ में मदद कर सकता हूँ?",
}
# Only plain confirmations. "बताइए" / "batao" are deliberately absent: as a
# literal token they would outrank a named option ("IVR का तरीका बताइए").
MORE_HELP_YES = (
    "Yes/Yes please/Yes sir/Yes I need help/Haan/Haan ji/Ha ji/Ji haan/Haan chahiye/"
    "One more thing/Ek aur cheez/Aur madad chahiye/"
    "हाँ/हां/हाँ जी/हां जी/जी हाँ/जी हां/हाँ चाहिए/हां चाहिए/एक और चीज़/और मदद चाहिए"
)
ANYTHING_ELSE_HUBS = ("n_anything_else", "n_low_anything_else", "n_sms", "n_ivr", "n_both")
# After one option was explained the caller may still ask for another one.
OPTION_FOLLOW_UPS = {
    "n_sms": ("n_ivr", "n_raise_has_record"),
    "n_ivr": ("n_sms", "n_raise_has_record"),
    "n_both": ("n_raise_has_record",),
}
PAPER_ROLL_DOCKET = "WL458921"


def _edge(definition: dict, source: str, target: str) -> dict | None:
    return next((e for e in definition["edges"]
                 if e.get("from") == source and e.get("to") == target), None)


def _add_edge(definition: dict, edge_id: str, source: str, target: str, label: str) -> None:
    if _edge(definition, source, target) is None:
        definition["edges"].append({"id": edge_id, "from": source, "to": target, "label": label})


def repair_anything_else_routing(definition: dict) -> dict:
    """"Anything else?" answered with yes must not end the call.

    The closing edge of these hubs lists "theek hai" / "ठीक है" among its
    declines. Those tokens classify as ``affirm``, so the engine treated the
    edge as the hub's affirmative branch and every "Yes" / "हाँ, बताइए" went
    to the goodbye node (cv_c5a83af7fa57, cv_8358a2aef66b, cv_c6b2d1b8daac).
    A dedicated yes edge carries a literal token and therefore wins; the
    closing edge and its tokens stay untouched ("ठीक है" still closes).

    After the SMS or IVR steps the caller could only decline or raise a
    battery problem: "IVR बताओ" was answered "please call again" and "can you
    raise the request yourself" ended the call on its "नहीं" (cv_b79eba6aa27f,
    cv_49d1b4053aa9). The option edges are copied from the verified hub
    ``n_pr_again``; ``optionHub`` lets a named option beat a generic yes/no.
    """
    from shared.orchestration.router import classify_user_signal
    from shared.orchestration.workflow_engine import _edge_tokens

    result = copy.deepcopy(definition)
    by_id = {node["id"]: node for node in result["nodes"]}
    source_hub = by_id.get("n_anything_else")
    if source_hub is None or not all(name in by_id for name in ANYTHING_ELSE_HUBS):
        raise ValueError("Expected the five anything-else hubs")
    if MORE_HELP_ID not in by_id:
        result["nodes"].append({
            "id": MORE_HELP_ID, "kind": "intent", "label": "Yes, something else — what do you need?",
            "x": source_hub.get("x", 0) + 40, "y": source_hub.get("y", 0) + 160,
            "config": {"prompt": MORE_HELP["en"], "responseMode": "fixed",
                       "textByLanguage": dict(MORE_HELP)},
        })
        for edge in [e for e in result["edges"] if e.get("from") == "n_anything_else"]:
            label = str(edge.get("label") or "")
            if edge.get("to") == "n_end":
                # A second bare "yes" here must be re-asked, never closed.
                # The engine also splits a phrase at commas ("Theek hai, nahi
                # chahiye" carries the affirmative token "theek hai").
                label = "/".join(
                    phrase for phrase in label.split("/")
                    if all(classify_user_signal(token) != "affirm" for token in _edge_tokens(phrase))
                )
            _add_edge(result, f"e_{MORE_HELP_ID}__{edge['to']}", MORE_HELP_ID, edge["to"], label)
    for hub in ANYTHING_ELSE_HUBS:
        _add_edge(result, f"e_{hub}__{MORE_HELP_ID}__yes", hub, MORE_HELP_ID, MORE_HELP_YES)
    for hub, targets in OPTION_FOLLOW_UPS.items():
        by_id[hub].setdefault("config", {})["optionHub"] = True
        for target in targets:
            template = _edge(result, "n_pr_again", target)
            if template is None:
                raise ValueError(f"Expected n_pr_again → {target}")
            _add_edge(result, f"e_{hub}__{target}__follow_up", hub, target, template["label"])
    return result


def repair_fixed_reference_readbacks(definition: dict) -> dict:
    """Spell the hard-coded paper-roll docket with the runtime formatter.

    ``n_pr_docket`` carries the docket as literal text, so no slot format can
    reach it; it was sent to TTS as ``WL458921``. The words come from
    ``english_identifier_readback`` — the same function the engine applies to
    typed slots — never from a hand-written table.
    """
    from shared.orchestration.spoken_numbers import english_identifier_readback

    result = copy.deepcopy(definition)
    spoken = english_identifier_readback(PAPER_ROLL_DOCKET)
    for node in result["nodes"]:
        if node.get("id") != "n_pr_docket":
            continue
        config = node.get("config", {})
        for key in ("text", "prompt"):
            if isinstance(config.get(key), str):
                config[key] = config[key].replace(PAPER_ROLL_DOCKET, spoken)
        config["textByLanguage"] = {
            locale: text.replace(PAPER_ROLL_DOCKET, spoken)
            for locale, text in (config.get("textByLanguage") or {}).items()
        }
    return result


def repair_evening_review(definition: dict) -> dict:
    """Everything the deployed runtime (55168b83 and later) already supports."""
    result = stage_dynamic_readbacks(definition)
    result = repair_fixed_reference_readbacks(result)
    return repair_anything_else_routing(result)


DIGIT_RULE_OLD = (
    "destination, say every digit separately in English, including in Hindi replies: zero, one,\n"
    "two, three, four, five, six, seven, eight, nine. Name alphabetic characters explicitly,\n"
    "for example letter A. Keep leading zeroes."
)
DIGIT_RULE_NEW = (
    "destination, say every digit separately in English, including in Hindi replies: zero, one,\n"
    "two, three, four, five, six, seven, eight, nine. Put a comma after every digit and never\n"
    "group digits into pairs or larger numbers: say six, five, zero, seven, not sixty-five or\n"
    "six five zero seven. Never write such a value as numerals. Name alphabetic characters\n"
    "explicitly, for example letter A. Keep leading zeroes."
)


def repair_prompt_digit_rule(full_prompt: str) -> str:
    """One digit at a time: generated replies grouped them ("four five eight nine, two one")."""
    if DIGIT_RULE_NEW in full_prompt:
        return full_prompt
    if full_prompt.count(DIGIT_RULE_OLD) != 1:
        raise ValueError("Expected exactly one English digit readback rule")
    return full_prompt.replace(DIGIT_RULE_OLD, DIGIT_RULE_NEW)


# ── 18 November midday review (18 calls after 06:37 UTC, workflow v55) ──────

# "खुद" between the pronoun and the verb broke every authored raise phrase:
# only the 5-character "कर दो" matched and lost to the closing edge's "ठीक है".
RAISE_SELF_PHRASES = (
    "खुद कर दो/तुम खुद कर दो/आप खुद कर दो/खुद ही कर दो/खुद कर दीजिए/आप खुद कर दीजिए/"
    "खुद से कर दो/Khud kar do/Tum khud kar do/Aap khud kar do/Khud hi kar do/"
    "Khud kar dijiye/Aap khud kar dijiye"
)
RAISE_TARGET = "n_raise_has_record"


def repair_midday_review(definition: dict) -> dict:
    """Two routing defects seen on workflow v55 (18 November test calls).

    * "Anything else?" answered "जी।" still closed the call (cv_f3800137eb10,
      06:49:05 UTC). With an ``affirm`` label and no literal token the engine
      takes the FIRST edge whose tokens carry that signal, and the closing
      edge ("ठीक है", "fine") came before the yes edge. The yes edge is now the
      first outgoing edge of every anything-else hub; a literal closing token still wins
      because the longer token decides ("ठीक है", "नहीं", "thank you").
    * After the SMS steps "नहीं, मुझे तुम खुद कर दो … ठीक है सर" closed the call
      (cv_8bd254559c2f, 06:43:03 UTC): no raise phrase contains "खुद", so the
      closing token "ठीक है" was the longest match. The phrases are added to
      every edge that leads to the raise-request step.
    """
    result = copy.deepcopy(definition)
    if not all(_edge(result, hub, MORE_HELP_ID) for hub in ANYTHING_ELSE_HUBS):
        raise ValueError("Expected the yes edge on every anything-else hub (apply repair_evening_review first)")
    edges = result["edges"]
    for hub in ANYTHING_ELSE_HUBS:
        yes = _edge(result, hub, MORE_HELP_ID)
        first = next(e for e in edges if e.get("from") == hub)
        if first is not yes:
            edges.remove(yes)
            edges.insert(edges.index(first), yes)
    raise_edges = [e for e in edges if e.get("to") == RAISE_TARGET and "कर दो" in str(e.get("label") or "")]
    if not raise_edges:
        raise ValueError("Expected at least one raise-request option edge")
    for edge in raise_edges:
        have = set(edge["label"].split("/"))
        extra = [p for p in RAISE_SELF_PHRASES.split("/") if p not in have]
        if extra:
            edge["label"] = edge["label"] + "/" + "/".join(extra)
    return result
