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
            config["readback"] = {
                locale: {"formats": {"docket_number": "identifier"}, "groups": [
                    {"requires": ["docket_number"], "template": text},
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
