"""Opt-in read-back of caller-collected workflow fields; no tenant rules here."""
from __future__ import annotations

import re

from shared.orchestration import lang

_REQUEST = lang.compile_alternation("value_readback_request")
_REFERENCE = lang.compile_alternation("caller_value_reference")
_NEGATION = lang.compile_alternation("value_readback_negation")
_CAPTURED = {"slot_filled", "entry_slot_filled", "also_captured", "also_updated",
             "identifier_corrected"}


def caller_value_request(text: str) -> bool:
    """A value the caller supplied is not the bot's last spoken sentence."""
    return bool(_REQUEST and _REFERENCE and _REQUEST.search(text) and _REFERENCE.search(text))


def mentions_alias(text: str, aliases: list) -> bool:
    return any(
        isinstance(alias, str) and alias.strip()
        and re.search(r"(?<![\w" + lang.letters() + r"])"
                      + re.escape(alias.strip())
                      + r"(?![\w" + lang.letters() + r"])", text, re.I)
        for alias in aliases
    )


def configured_readback(nodes: list[dict], text: str, slots: dict,
                        audit: list[dict], language: str,
                        values: dict | None = None) -> tuple[str, str] | None:
    """Return (variable, authored reply), only for one explicitly enabled field.

    Values must have caller-capture provenance in this workflow checkpoint.
    Prefilled/context/API values are not eligible. No value is added to audit.
    Full and last4 are explicit choices; absent/invalid config does nothing.
    """
    if not _REQUEST or not _REQUEST.search(text) or (_NEGATION and _NEGATION.search(text)):
        return None
    matches = []
    for node in nodes:
        config = node.get("config") or {}
        spec = config.get("valueReadback")
        if node.get("kind") != "ask" or not isinstance(spec, dict):
            continue
        if spec.get("mode") not in ("full", "last4"):
            continue
        exclusions = spec.get("excludeAliases")
        if isinstance(exclusions, list) and mentions_alias(text, exclusions):
            continue
        aliases = spec.get("aliases")
        if isinstance(aliases, list) and mentions_alias(text, aliases):
            matches.append((config, spec))
    if len(matches) != 1:
        return None  # ambiguous: never choose a field arbitrarily
    config, spec = matches[0]
    variable = str(config.get("variable") or "")
    locale = language.split("-")[0].lower()
    responses = spec.get("responses")
    if not isinstance(responses, dict):
        return None
    localized = responses.get(locale)
    if not isinstance(localized, dict):
        localized = responses.get("en")
    if not variable or not isinstance(localized, dict):
        return None
    slot = slots.get(variable)
    value = None
    retained = (values or {}).get(variable, {})
    if retained.get("slot") == slot and retained.get("mode") == spec["mode"]:
        value = retained.get("value")
    captured = any(e.get("variable") == variable and e.get("action") in _CAPTURED for e in audit)
    if (not captured or not isinstance(value, (str, int)) or isinstance(value, bool)
            or not str(value) or any(c in str(value) for c in "•*")):
        reply = localized.get("missing")
    else:
        value = str(value)
        if spec["mode"] == "last4":
            value = value[-4:]
        if spec.get("speakDigits") is True:
            value = " ".join(value)
        template = localized.get("template")
        reply = template.replace("{value}", value) if isinstance(template, str) else None
    return (variable, reply) if isinstance(reply, str) and reply.strip() else None


def retain_readback_values(nodes: list[dict], slots: dict, entries: list[dict],
                           text: str, pending: dict, previous: dict) -> dict:
    """Keep only each opted-in field's permitted display value in the session.

    Ordinary PII slots stay masked; these values are not exported as slots to
    the LLM, recorder or tool arguments. last4 never retains the full value.
    """
    from shared.orchestration.ask_resolution import _ask_entity
    from shared.orchestration.entity_extractor import extract_entity
    from shared.orchestration.spoken_numbers import spoken_digit_sequence

    values = {}
    for node in nodes:
        config = node.get("config") or {}
        spec = config.get("valueReadback")
        if node.get("kind") != "ask" or not isinstance(spec, dict) or spec.get("mode") not in ("full", "last4"):
            continue
        variable = str(config.get("variable") or "")
        old = previous.get(variable, {})
        if old.get("slot") == slots.get(variable) and old.get("mode") == spec["mode"]:
            values[variable] = old
        captures = [e for e in entries if e.get("variable") == variable and e.get("action") in _CAPTURED]
        if not captures:
            continue
        values.pop(variable, None)
        offered = text
        if captures[-1].get("accumulated_digits"):
            offered = str(pending.get(node["id"], "")) + spoken_digit_sequence(text)
        entity = _ask_entity(node, variable)
        # Re-extract only a successfully collected, explicitly opted-in field.
        # Check the masked result too, so incidental digits cannot replace it.
        matched = extract_entity(offered, entity)
        if not matched.get("matched") and offered == slots.get(variable):
            # A literal free-text ask has no entity matcher. Its accepted
            # answer is exactly this utterance, not a context/API value.
            matched = {"value": offered}
        if (matched.get("value") or matched.get("maskedValue")) != slots.get(variable):
            continue
        raw = matched.get("value") or extract_entity(offered, {**entity, "pii": False,
                                     "maskingEnabled": False, "masking_enabled": False}).get("value")
        if raw is not None:
            values[variable] = {"slot": slots[variable], "mode": spec["mode"],
                                "value": str(raw)[-4:] if spec["mode"] == "last4" else str(raw)}
    return values
