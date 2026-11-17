"""Caller confirmation of collected identifiers, shared by every workflow.

Candidates stay in private checkpoint state until the caller confirms them.
They are never included in exported slots, audit entries or tool arguments.
"""
from __future__ import annotations

import re

from shared.orchestration.router import looks_like_question

_REFERENCE_WORDS = {"id", "identifier", "reference", "ref", "serial", "tid", "mid"}
_PHONE_WORDS = {"phone", "mobile", "telephone"}
_SECRET_WORDS = {"otp", "password", "passcode", "pin", "cvv", "cvc"}
_NON_REFERENCE_TYPES = {"date", "date_range", "time", "duration", "currency",
                        "percentage", "spoken_day", "spoken_time", "boolean"}
_MEASUREMENT_WORDS = {"amount", "price", "cost", "quantity", "count", "age", "floor",
                      "guests", "people", "weight", "height", "distance", "duration"}
_IDENTIFIER_TYPES = {"identifier", "reference", "account_number", "policy_number", "claim_number"}
_LETTERS = dict(zip(
    ("ए", "बी", "सी", "डी", "ई", "एफ", "जी", "एच", "आई", "जे", "के", "एल",
     "एम", "एन", "ओ", "पी", "क्यू", "आर", "एस", "टी", "यू", "वी", "डब्ल्यू", "एक्स", "वाई", "ज़ेड"),
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
))
_DIGITS = dict(zip("0123456789", "zero one two three four five six seven eight nine".split()))
_NEGATIVE = re.compile(r"(?<![\w./_-])(?:no|not|wrong|incorrect|nahi|nahin|nhi|galat|नहीं|नही|गलत|ग़लत|இல்லை|അല്ല)(?![\w/_-])", re.I)
_CORRECTION = re.compile(r"(?<!\w)(?:but|actually|instead|except|ending|last|first|replace|correction|lekin|लेकिन|आखिरी|आख़िरी|बदलो|बदल|सुधार)(?!\w)", re.I)
_YES = re.compile(
    r"^(?:(?:yes|yeah|yep|correct|right|absolutely|haan|han|haa|ha|haanji|hanji|ji|"
    r"sahi|hai|hain|bilkul|sir|madam|it|is|that's|that|हाँ|हां|हा|हाँजी|हांजी|जी|सही|है|हैं|"
    r"बिल्कुल|सर|ஆமாம்|சரி|അതെ|ശരി)[\s,.!।]*)+$", re.I,
)
_AFFIRMATIVE = re.compile(r"(?<!\w)(?:yes|yeah|yep|correct|right|absolutely|haan|han|haa|ha|haanji|hanji|ji|sahi|bilkul|हाँ|हां|हा|हाँजी|हांजी|जी|सही|बिल्कुल|ஆமாம்|சரி|അതെ|ശരി)(?!\w)", re.I)


def _field_words(value: str) -> set[str]:
    # Both bookingId and complaintID are common in existing tenant schemas.
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(value))
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return set(re.findall(r"[a-z]+|[0-9]+", value.lower()))


def identifier_kind(node: dict, variable: str) -> str | None:
    """Schema semantics first, generic field-name compatibility second.

    No tenant, bot, or business-specific ID name is needed. Opaque custom
    fields opt in with dataType=identifier or requireConfirmation=true.
    The default false on older entities never disables the common ID rule.
    """
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    entity = config.get("entity") if isinstance(config.get("entity"), dict) else {}
    patterns = entity.get("synonymPatterns") or entity.get("synonym_patterns")
    names = [_field_words(variable), _field_words(entity.get("name") or "")]
    words = set().union(*names)
    dtype = str(entity.get("dataType") or entity.get("data_type")
                or config.get("entityType") or config.get("dataType") or "text").lower()
    semantics = str(entity.get("semanticType") or config.get("semanticType") or "").lower()
    if (config.get("prefillOnly") or config.get("confirmsIdentifier")
            or words & _SECRET_WORDS or _field_words(dtype) & _SECRET_WORDS
            or semantics in {"secret", "authentication"}
            or any({"card", "number"} <= name for name in names)
            or (isinstance(patterns, dict) and {str(k).lower() for k in patterns} == {"yes", "no"})):
        return None
    from shared.orchestration.ask_resolution import _yes_no_canonicals
    if (_yes_no_canonicals(entity) is not None
            or {str(v).lower() for v in entity.get("allowedValues") or []} == {"yes", "no"}):
        return None
    if dtype == "phone" or semantics == "phone":
        return "phone"
    if (dtype in _IDENTIFIER_TYPES or semantics in {"identifier", "reference"}
            or entity.get("requireConfirmation") is True or config.get("requireConfirmation") is True):
        return "identifier"
    if dtype in _NON_REFERENCE_TYPES:
        return None
    if words & _PHONE_WORDS or {"contact", "number"} <= words:
        return "phone"
    if words & _MEASUREMENT_WORDS:
        return None
    if words & _REFERENCE_WORDS:
        return "identifier"
    # ticketNo, complaint_number, shipment-number, etc. share a reference
    # shape; amounts and measurements remain ordinary numeric answers.
    if (words & {"number", "no"} and len(words) > 1
            and any(re.search(r"(?:number|no)\d*$", re.sub(r"[^A-Za-z0-9]", "", name), re.I)
                    for name in (variable, str(entity.get("name") or "")))):
        return "identifier"
    return None


def spoken_identifier_text(text: str) -> str:
    """Normalize dictated letters without guessing sound-alike substitutions."""
    from shared.orchestration.spoken_numbers import spoken_digit_text

    value = spoken_digit_text(text)
    value = re.sub(r"\b([A-Za-z])\s+(?:for|as\s+in)\s+[A-Za-z]+\b", lambda m: m[1].upper(), value, flags=re.I)
    for word, letter in _LETTERS.items():
        value = re.sub(r"(?<!\w)" + re.escape(word) + r"(?!\w)", letter, value)
    # Join only adjacent single letters / digit runs, leaving prose alone.
    token = r"(?:\d+|[A-Za-z])"
    pattern = re.compile(r"(?<!\w)(" + token + r"(?:[\s,.;।-]+" + token + r")+)(?!\w)")
    return pattern.sub(lambda m: re.sub(r"[\s,.;।-]+", "", m[1]).upper(), value)


def canonical_identifier(value: str) -> str:
    return re.sub(r"\s+", "", str(value)).upper()


def identifier_sequence(text: str) -> str:
    normalized = spoken_identifier_text(text)
    runs = re.findall(r"(?<!\w)[A-Za-z]*\d+[A-Za-z0-9]*(?!\w)", normalized)
    if runs:
        return "".join(runs).upper()
    letter = re.fullmatch(r"\s*(?:letter\s+)?([A-Za-z])[\s,.!।]*", normalized, re.I)
    return letter[1].upper() if letter else ""


def identifier_dictation(text: str) -> bool:
    from shared.orchestration.spoken_numbers import digits_dominant
    normalized = spoken_identifier_text(text)
    sequence = identifier_sequence(text)
    return bool(sequence) and (digits_dominant(text)
                              or re.sub(r"[^A-Za-z0-9]", "", normalized).upper() == sequence)


def spoken_readback(value: str) -> str:
    punctuation = {"+": "plus", "-": "hyphen", "/": "slash", "_": "underscore",
                   ".": "dot", ":": "colon", "#": "hash"}
    return ", ".join(_DIGITS.get(c, punctuation.get(c, "letter " + c))
                     for c in canonical_identifier(value))


def confirmation_prompt(value: str, kind: str, language: str) -> str:
    from shared.orchestration.phrases import canned
    label = "phone number" if kind == "phone" else "ID"
    return canned("wf_identifier_confirm", language).replace("{label}", label).replace("{value}", spoken_readback(value))


def recapture_prompt(kind: str, language: str) -> str:
    from shared.orchestration.phrases import canned
    return canned("wf_phone_recapture" if kind == "phone" else "wf_identifier_recapture", language)


def raw_candidate(node: dict, variable: str, text: str) -> str | None:
    from shared.orchestration.ask_resolution import _ask_entity
    from shared.orchestration.entity_extractor import extract_entity

    entity = {**_ask_entity(node, variable), "pii": False, "maskingEnabled": False, "masking_enabled": False}
    result = extract_entity(text, entity)
    value = result.get("value") if result.get("matched") else None
    if value is None:
        return None
    canonical = canonical_identifier(value)
    # Semantic outcomes such as no_tid are authored labels, not dictated
    # references. Literal/letter-spelled allowed IDs (including all-letter
    # IDs) still qualify. Workflows may also name unavailable values directly.
    config = node.get("config") or {}
    unavailable = entity.get("unavailableValues") or config.get("unavailableValues") or []
    if canonical in {canonical_identifier(v) for v in unavailable}:
        return None
    if (result.get("method") in {"lexicon", "lexicon_pattern"}
            and canonical not in canonical_identifier(spoken_identifier_text(text))):
        return None
    # Legacy numeric matchers may intentionally store only an ID's numeric
    # lookup key. Still read the caller's attached letters back faithfully.
    sequence = identifier_sequence(text)
    if (identifier_kind(node, variable) == "identifier" and not any(c.isalpha() for c in canonical)
            and any(c.isalpha() for c in sequence)
            and re.sub(r"\D", "", sequence) == re.sub(r"\D", "", canonical)):
        canonical = sequence
    return canonical or None


def start_confirmation(ctx, *, offered: str | None = None) -> bool:
    """Intercept an otherwise valid capture before it becomes a slot."""
    kind = identifier_kind(ctx.node, ctx.variable)
    if not kind or ctx.identifier_confirmed or ctx.config.get("_externalIdentifierConfirmation"):
        return False
    source = offered if offered is not None else (ctx.combined if ctx.accumulated else ctx.text)
    raw = raw_candidate(ctx.node, ctx.variable, source)
    if raw is None:
        return False  # e.g. an explicitly authored `no_tid` outcome
    ctx.slots.pop(ctx.variable, None)
    ctx.pending_digits.pop(ctx.node_id, None)
    ctx.pending_confirmations[ctx.node_id] = {"variable": ctx.variable, "value": ctx.value,
                                             "raw": raw, "kind": kind, "attempts": 0}
    ctx.audit.append({"action": "identifier_confirmation_requested", "node": ctx.node_id,
                      "variable": ctx.variable})
    ctx.replies.append(confirmation_prompt(raw, kind, ctx.lang))
    ctx.stay()
    return True


def resolve_confirmation(ctx) -> bool:
    """Corrections and rejection always outrank an affirmation or LLM label."""
    pending = ctx.pending_confirmations.get(ctx.node_id)
    if not pending or pending.get("variable") != ctx.variable:
        return False
    # An all-letter ID matcher can also accept YES / NO. Confirmation words
    # retain their conversational meaning instead of replacing the ID.
    control = ctx.text.replace("’", "'").strip(" \t\r\n,.!।")
    affirmative = bool(_YES.fullmatch(control) and _AFFIRMATIVE.search(control))
    candidate_source = ctx.text
    # With alphabetic references, a broad authored matcher can otherwise
    # capture "no" / "actually" instead of the replacement that follows.
    while True:
        candidate_source = candidate_source.lstrip(" \t\r\n,.!;:।")
        prefix = (_AFFIRMATIVE.match(candidate_source) or _NEGATIVE.match(candidate_source)
                  or _CORRECTION.match(candidate_source))
        if (not prefix or prefix.end() == len(candidate_source)
                or candidate_source[prefix.end()] not in " \t\r\n,!;:।"):
            break
        candidate_source = candidate_source[prefix.end():]
    candidate = (None if affirmative or _NEGATIVE.fullmatch(control)
                 else raw_candidate(ctx.node, ctx.variable, candidate_source))
    prose = ctx.text
    if candidate:
        prose = re.sub(re.escape(candidate), "", prose, flags=re.I)
    negative = bool(_NEGATIVE.search(prose))
    correction = bool(_CORRECTION.search(prose)) or bool(
        re.fullmatch(r"\s*(?:letter\s+)?[A-Za-z][\s,.!।]*", spoken_identifier_text(ctx.text), re.I))
    if candidate and candidate != pending["raw"]:
        from shared.orchestration.ask_resolution import _extract_ask_value
        ctx.value = _extract_ask_value(ctx.node, ctx.variable, candidate_source)
        start_confirmation(ctx, offered=candidate_source)
        return True
    if negative or correction:
        ctx.pending_confirmations.pop(ctx.node_id, None)
        ctx.pending_digits.pop(ctx.node_id, None)
        ctx.slots.pop(ctx.variable, None)
        ctx.audit.append({"action": "identifier_confirmation_rejected", "node": ctx.node_id,
                          "variable": ctx.variable})
        ctx.replies.append(recapture_prompt(pending["kind"], ctx.lang))
        ctx.stay()
        return True
    remainder = ctx.text.replace("’", "'")
    if candidate == pending["raw"]:
        remainder = re.sub(re.escape(candidate), "", spoken_identifier_text(remainder), flags=re.I)
    if (_YES.fullmatch(remainder.strip()) and _AFFIRMATIVE.search(remainder)
            and not looks_like_question(ctx.text)):
        ctx.value = pending["value"]
        ctx.identifier_confirmed = True
        ctx.pending_confirmations.pop(ctx.node_id, None)
        ctx.audit.append({"action": "identifier_confirmed", "node": ctx.node_id, "variable": ctx.variable})
        return False  # outcome_filled commits and advances once
    pending = {**pending, "attempts": pending.get("attempts", 0) + 1}
    ctx.pending_confirmations[ctx.node_id] = pending
    if pending["attempts"] > ctx.behavior.max_ask_retries:
        from shared.orchestration.phrases import canned
        ctx.pending_confirmations.pop(ctx.node_id, None)
        ctx.status = "handoff"
        ctx.current = ctx.awaiting = None
        ctx.replies.append(canned("wf_handover", ctx.lang))
    else:
        ctx.replies.append(confirmation_prompt(pending["raw"], pending["kind"], ctx.lang))
        ctx.stay()
    return True
