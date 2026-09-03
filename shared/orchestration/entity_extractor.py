"""Deterministic entity extraction used by the intent/entity test consoles
and the runtime slot pre-fill. Regex- and lexicon-based — no LLM calls.

Masking: values of entities flagged sensitive (pii/masking_enabled) are
masked before they are returned to logs or transcripts.
"""

import functools
import re
from functools import lru_cache
from typing import Any

# Built-in patterns per data_type. Deliberately conservative: false negatives
# are recoverable in conversation, false positives are not.
_TYPE_PATTERNS: dict[str, str] = {
    "number": r"-?\d+(?:[.,]\d+)?",
    "integer": r"-?\d+",
    "decimal": r"-?\d+[.,]\d+",
    "date": r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}(?:,? \d{4})?)\b",
    "time": r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm|hrs|hours)?\b",
    "duration": r"\b\d+\s?(?:minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)\b",
    "currency": r"(?:₹|rs\.?|inr|\$|usd|€|eur)\s?\d+(?:[.,]\d+)*(?:\s?(?:lakhs?|crores?|thousand|k|million|m))?",
    "percentage": r"\b\d+(?:\.\d+)?\s?(?:%|percent)\b",
    "phone": r"(?:\+?\d{1,3}[\s-]?)?(?:\d[\s-]?){9,11}\d",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "account_number": r"\b\d{9,18}\b",
    "policy_number": r"\b[A-Z]{2,4}[-/]?\d{6,12}\b",
    "claim_number": r"\b(?:CLM|CL|C)[-/]?\d{6,12}\b",
    "card_last4": r"\b(?:ending(?: in)?|last(?: four| 4)?(?: digits)?)\s*:?\s*(\d{4})\b",
}

_MASK = "•••"


def _mask_value(value: str, data_type: str) -> str:
    if data_type in ("card_last4",):
        return f"{_MASK}{value[-4:]}" if len(value) >= 4 else _MASK
    if len(value) > 4:
        return value[:2] + _MASK + value[-2:]
    return _MASK


# Data types whose values are digit sequences: for these, a failed match is
# retried on the spoken-number normalization of the utterance ("six zero one
# zero double one" / "छह शून्य…" / "6 0 1 0 1 1" → "601011"), so callers can
# dictate identifiers in any supported language. Explicit regex entities opt
# in the same way when their pattern visibly expects digits.
_DIGIT_DATA_TYPES = frozenset({
    "number", "integer", "decimal", "currency", "percentage", "phone",
    "account_number", "policy_number", "claim_number", "card_last4",
})
_DIGIT_PATTERN_HINT = re.compile(r"\\d|\[0-9|\[\^?0-9")


def _expects_digits(entity: dict[str, Any]) -> bool:
    """Whether this entity's value is a numeric sequence (id/OTP/phone/…)."""
    data_type = entity.get("dataType") or entity.get("data_type") or "text"
    if data_type in _DIGIT_DATA_TYPES:
        return True
    pattern = entity.get("regexPattern") or entity.get("regex_pattern") or ""
    return bool(_DIGIT_PATTERN_HINT.search(str(pattern)))


# Bounds used when configuration provides none: wide enough for any real
# identifier, tight enough to keep buffers finite.
_DEFAULT_MIN_DIGITS = 4
_DEFAULT_MAX_DIGITS = 32
# Varied probe digits so a pattern with value constraints still sees plausible
# input (a run of one repeated digit can defeat e.g. "no all-same" rules).
_LENGTH_PROBE = "98765432101234567890987654321012"


@lru_cache(maxsize=256)
def _pattern_length_bounds(pattern: str) -> tuple[int, int] | None:
    """Digit-count bounds an authored pattern accepts, derived by PROBING.

    Quantifier parsing breaks on alternations and lookarounds
    ("([0-9]{10}|[0-9]{7})", "(?<![0-9])…"); probing the compiled pattern with
    pure digit strings of every candidate length asks the pattern itself. A
    length is acceptable only when the match consumes the WHOLE probe — a
    12-digit probe that merely contains a 10-digit match means 12 is NOT a
    valid identifier length. Patterns that need non-digit context (literal
    prefixes like "BK\\d{6}") match no probe and fall back to wide defaults.
    """
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None
    acceptable = [
        length
        for length in range(1, _DEFAULT_MAX_DIGITS + 1)
        if (m := compiled.search(_LENGTH_PROBE[:length])) is not None
        and m.end() - m.start() == length
    ]
    if not acceptable:
        return None
    return min(acceptable), max(acceptable)


def identifier_length_bounds(entity: dict[str, Any]) -> tuple[int, int]:
    """(min, max) digit-count an identifier entity accepts, from its config.

    Derived from the authored regex when present (by probing — see
    :func:`_pattern_length_bounds`), from the values in ``allowedValues`` when
    they are digit strings, and from conservative per-type defaults otherwise.
    Used to bound dictation buffers and to detect impossible (overflowing)
    candidates — never to validate a value (the entity's own matcher stays
    authoritative).
    """
    pattern = str(entity.get("regexPattern") or entity.get("regex_pattern") or "")
    if pattern:
        bounds = _pattern_length_bounds(pattern)
        if bounds is not None:
            return bounds
    allowed = entity.get("allowedValues") or entity.get("allowed_values") or []
    digit_values = [str(v) for v in allowed if str(v).isdigit()]
    if digit_values:
        lengths = [len(v) for v in digit_values]
        return min(lengths), max(lengths)
    data_type = entity.get("dataType") or entity.get("data_type") or "text"
    if data_type == "phone":
        return 8, 15  # E.164 bounds
    if data_type == "card_last4":
        return 4, 4
    if data_type == "account_number":
        return 9, 18
    return _DEFAULT_MIN_DIGITS, _DEFAULT_MAX_DIGITS


@functools.lru_cache(maxsize=2048)
def _compiled(pattern: str) -> "re.Pattern | None":
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def _match_synonym_patterns(text: str, patterns: Any) -> str | None:
    """Structured canonical matching: ``synonymPatterns`` = {canonical: [regex…]}.

    Spoken answers rarely repeat a fixed surface string: "माँ को दे दिया",
    "उनके माँ को प्रोडक्ट दिया", "mummy ke paas de diya" all hand the order to
    the mother, while "माँ के पास दे दो" (the customer's instruction) and "माँ
    को नहीं दिया" do not. A per-canonical regex expresses that structure
    (recipient → postposition → optional object words → past-tense handover
    verb, negation excluded) once, instead of enumerating word orders.
    Canonicals are tried in authored order, so negative/"no" canonicals may
    be listed first to win over their positive counterparts; the first
    canonical with any matching pattern wins.
    """
    if not isinstance(patterns, dict):
        return None
    for canonical, alternatives in patterns.items():
        if isinstance(alternatives, str):
            alternatives = [alternatives]
        for pattern in alternatives or []:
            compiled = _compiled(str(pattern))
            if compiled is not None and compiled.search(text):
                return str(canonical)
    return None


def _match_entity_text(text: str, entity: dict[str, Any]) -> tuple[str | None, str]:
    """One matching pass over one candidate text: regex → patterns → lexicon → builtin."""
    kind = entity.get("kind", "custom")
    data_type = entity.get("dataType") or entity.get("data_type") or "text"
    lowered = text.lower()

    # 1. Explicit regex wins. ``regexPatterns`` (a list) is tried in order,
    #    each pattern with its own capturing group — the way to extract one
    #    value ("the guard's name") from several unrelated phrasings without
    #    a giant alternation whose group is None on the other branches.
    patterns = entity.get("regexPatterns") or entity.get("regex_patterns") or []
    pattern = entity.get("regexPattern") or entity.get("regex_pattern")
    if kind == "regex" and not pattern:
        pattern = None
    for candidate in ([pattern] if pattern else []) + [str(x) for x in patterns if x]:
        compiled = _compiled(candidate)
        m = compiled.search(text) if compiled is not None else None
        if m:
            value = m.group(1) if m.groups() else m.group(0)
            if value:
                return value, "regex"

    # 1b. Structured per-canonical patterns (order-tolerant, negation-aware).
    canonical = _match_synonym_patterns(
        text, entity.get("synonymPatterns") or entity.get("synonym_patterns")
    )
    if canonical is not None:
        return canonical, "lexicon_pattern"

    # 2. Allowed values + synonyms lexicon.
    allowed = entity.get("allowedValues") or entity.get("allowed_values") or []
    synonyms = entity.get("synonyms") or {}
    lexicon: list[tuple[str, str]] = [(v, v) for v in allowed]
    for canonical, alts in (synonyms or {}).items():
        lexicon.append((canonical, canonical))
        for alt in alts or []:
            lexicon.append((alt, canonical))
    for surface, canonical in sorted(lexicon, key=lambda p: -len(p[0])):
        if surface and surface.lower() in lowered:
            return canonical, "lexicon"

    # 3. Built-in data-type pattern.
    builtin = _TYPE_PATTERNS.get(data_type)
    if builtin:
        m = re.search(builtin, text, re.IGNORECASE)
        if m:
            return (m.group(1) if m.groups() else m.group(0)), "type_pattern"

    return None, ""


def extract_entity(text: str, entity: dict[str, Any]) -> dict[str, Any]:
    """Extract a single entity definition from text.

    entity: {name, kind, dataType, regexPattern, allowedValues, synonyms,
             maskingEnabled, pii}
    Returns {name, matched, value, maskedValue, method, normalized} — `value`
    is None when not matched; masked entities never expose the raw value in
    maskedValue. `normalized` is True when the match came from the
    spoken-number rewrite of the utterance rather than the raw transcript.
    """
    name = entity.get("name", "")
    data_type = entity.get("dataType") or entity.get("data_type") or "text"

    matched_value, method = _match_entity_text(text, entity)
    normalized = False

    # Spoken-number fallback, digit-expecting entities only: the raw
    # transcript may carry the value as digit words or spaced digit groups.
    if matched_value is None and text and _expects_digits(entity):
        from shared.orchestration.spoken_numbers import spoken_digit_text

        rewritten = spoken_digit_text(text)
        if rewritten != text:
            matched_value, method = _match_entity_text(rewritten, entity)
            normalized = matched_value is not None

    sensitive = bool(entity.get("pii") or entity.get("maskingEnabled")
                     or entity.get("masking_enabled"))
    masked = _mask_value(matched_value, data_type) if (matched_value and sensitive) else matched_value
    return {
        "name": name,
        "matched": matched_value is not None,
        "value": None if sensitive and matched_value else matched_value,
        "maskedValue": masked,
        "sensitive": sensitive,
        "method": method,
        "normalized": normalized,
    }


def extract_entities(text: str, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [extract_entity(text, e) for e in entities]
