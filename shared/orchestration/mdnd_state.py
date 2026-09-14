"""Compatibility adapter for the MDND four-fact collector.

Canonical slots are checkpointed alongside the existing ticket/report fields.
Only definitions explicitly opting into ``mdnd_v1`` use this adapter.
"""

FIELDS = {
    "customer_called": "m_called_customer",
    "reached_location": "m_reached_location",
    "delivery_handoff": "m_handover_recipient",
    "cx_support_called": "m_cx_support_call",
}
YES_NO = {
    "customer_called": ("yes (called the customer)", "no (did not call)"),
    "reached_location": ("yes (reached the location)", "no (did not reach the location)"),
    "cx_support_called": ("yes (received CX support call)", "no (no CX support call)"),
}
HANDOFF = {
    "customer": "customer (direct)", "guard": "guard / security",
    "family_member": "relative (other)", "doorstep": "left at door",
    "other": "someone else",
}
CONTROLLED = frozenset((*FIELDS, *FIELDS.values(), "m_drop_location"))


def enabled(definition):
    return any((node.get("config") or {}).get("semanticSlots") == "mdnd_v1"
               for node in (definition or {}).get("nodes") or [])


def llm_extraction_enabled(definition):
    """Whether the per-turn LLM slot extractor runs for this definition.

    ``semanticSlots: mdnd_v1`` alone enables the deterministic state guards
    (never summarize incomplete slots, grounded summary fallback). The LLM
    extractor is a separate opt-in — ``semanticExtraction: "llm"`` on the same
    node — because it adds a model round-trip to every workflow turn and its
    patches are only as reliable as the model; the authored matchers stay the
    baseline either way.
    """
    return any(
        (node.get("config") or {}).get("semanticSlots") == "mdnd_v1"
        and str((node.get("config") or {}).get("semanticExtraction") or "").lower() == "llm"
        for node in (definition or {}).get("nodes") or []
    )


def canonical_slots(slots):
    result = {}
    for name, legacy in FIELDS.items():
        value = str(slots.get(legacy) or "")
        if name in YES_NO:
            result[name] = "yes" if value.startswith("yes") else "no" if value.startswith("no") else "unknown"
        else:
            result[name] = next((key for key, val in HANDOFF.items() if val == value), "unknown")
            if value in ("mother", "father", "brother", "relative (other)"):
                result[name] = "family_member"
            elif value in ("place (kept at a spot)", "not handed over"):
                result[name] = "other"
    return result


def merge_extraction(slots, result, audit, node):
    """Apply validated facts/retractions atomically; absence never erases.

    Returns the legacy slot names the extractor decided this turn (patched
    or explicitly retracted) — the caller shields exactly those from the
    authored keyword captures, nothing else.
    """
    before = dict(slots)
    patch = result.get("patch") or {}
    retracted = set(result.get("explicit_retractions") or [])
    decided: set[str] = set()
    for name, value in patch.items():
        if name not in FIELDS:
            continue
        legacy = FIELDS[name]
        if value != "unknown" or name in retracted:
            decided.add(legacy)
        if value == "unknown":
            if name not in retracted:
                continue
            slots.pop(legacy, None)
        elif name in YES_NO and value in ("yes", "no"):
            slots[legacy] = YES_NO[name][value == "no"]
        elif name == "delivery_handoff" and value in HANDOFF:
            detail = result.get("recipient_detail")
            slots[legacy] = HANDOFF[value]
            if value == "family_member" and detail in ("mother", "father", "brother"):
                slots[legacy] = detail
            if value == "other" and result.get("handoff_type") == "not_handed_over":
                slots[legacy] = "not handed over"
            elif value == "other" and result.get("drop_location"):
                slots[legacy] = "place (kept at a spot)"
            elif value == "other" and detail == "not handed over":
                slots[legacy] = detail
        if name == "delivery_handoff":
            place = result.get("drop_location")
            if value in ("doorstep", "other") and place:
                slots["m_drop_location"] = place
            elif value != "doorstep" or before.get(legacy) != slots.get(legacy):
                slots.pop("m_drop_location", None)
            if before.get(legacy) != slots.get(legacy):
                slots.pop("m_guard_name", None)
    for key in set(before) | set(slots):
        if key in FIELDS or before.get(key) == slots.get(key):
            continue
        audit.append({"action": "also_cleared" if key not in slots else
                      "also_updated" if key in before else "also_captured",
                      "node": node, "variable": key, "source": "mdnd_semantic",
                      "evidence": (result.get("evidence") or {}).get(
                          next((k for k, v in FIELDS.items() if v == key), ""), "")})
    slots.update(canonical_slots(slots))
    if "m_handover_recipient" in decided:
        decided.add("m_drop_location")
    return decided


def summary_fallback(slots, language):
    """The final confirmation remains grounded even if wording generation fails."""
    facts = canonical_slots(slots)
    if "unknown" in facts.values():
        return ""
    english = (language or "").startswith("en")
    called, reached, cx = (facts[key] == "yes" for key in
                          ("customer_called", "reached_location", "cx_support_called"))
    target = facts["delivery_handoff"]
    place = slots.get("m_drop_location")
    # The recorded recipient, not just its class: "customer की माँ" — never a
    # generic "family member" when the partner named the relative.
    recipient = str(slots.get("m_handover_recipient") or "")
    if english:
        handoff = {"customer": "you handed the order to the customer",
                   "guard": "you handed the order to the security guard",
                   "family_member": {
                       "mother": "you handed the order to the customer's mother",
                       "father": "you handed the order to the customer's father",
                       "brother": "you handed the order to the customer's brother",
                   }.get(recipient, "you handed the order to a member of the customer's household"),
                   "doorstep": "you left the order at the doorstep",
                   "other": "you handed the order to another person"}[target]
        if place and target in ("doorstep", "other"):
            handoff = f"you left the order at the place you described: {place}"
        if slots.get("m_handover_recipient") == "not handed over":
            handoff = "you did not hand over or leave the order"
        return ("Let me quickly confirm the details you shared. "
                f"You {'called' if called else 'did not call'} the customer, "
                f"you {'reached' if reached else 'did not reach'} the delivery location, "
                f"{handoff}, and you {'received' if cx else 'did not receive'} a call from CX Support. "
                "Is all of this correct?")
    handoff = {"customer": "order customer को सौंपा था",
               "guard": "order guard को सौंपा था",
               "family_member": {
                   "mother": "order customer की माँ को सौंपा था",
                   "father": "order customer के पिता को सौंपा था",
                   "brother": "order customer के भाई को सौंपा था",
               }.get(recipient, "order customer के घर के किसी member को सौंपा था"),
               "doorstep": "order doorstep पर रखा था",
               "other": "order किसी और को सौंपा था"}[target]
    if place and target in ("doorstep", "other"):
        handoff = f"order {place} रखा था"
    if slots.get("m_handover_recipient") == "not handed over":
        handoff = "order किसी को सौंपा या कहीं छोड़ा नहीं था"
    return ("आपके द्वारा दी गई जानकारी को एक बार confirm कर लेता हूँ। "
            f"आपने customer को call {'किया था' if called else 'नहीं किया था'}, "
            f"आप customer की location पर {'पहुँचे थे' if reached else 'नहीं पहुँचे थे'}, "
            f"आपने {handoff}, और आपको CX Support से call {'आया था' if cx else 'नहीं आया था'}। "
            "क्या ये सब सही है?")


def semantic_node(node, decided=None):
    """Prevent legacy keyword captures from overwriting semantic decisions.

    Only the slots the extractor decided THIS turn (``decided``) are shielded;
    fields it said nothing about keep their deterministic matchers, so a
    missed, failed or timed-out extraction never silences the authored
    patterns. ``decided=None`` keeps the original behaviour (shield every
    controlled slot).
    """
    shielded = CONTROLLED if decided is None else set(decided)
    config = dict(node.get("config") or {})
    config["alsoCapture"] = [spec for spec in config.get("alsoCapture") or []
                             if spec.get("variable") not in shielded]
    return {**node, "config": config}
