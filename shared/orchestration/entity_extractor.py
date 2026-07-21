"""Deterministic entity extraction used by the intent/entity test consoles
and the runtime slot pre-fill. Regex- and lexicon-based — no LLM calls.

Masking: values of entities flagged sensitive (pii/masking_enabled) are
masked before they are returned to logs or transcripts.
"""

import re
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


def extract_entity(text: str, entity: dict[str, Any]) -> dict[str, Any]:
    """Extract a single entity definition from text.

    entity: {name, kind, dataType, regexPattern, allowedValues, synonyms,
             maskingEnabled, pii}
    Returns {name, matched, value, maskedValue, method} — `value` is None when
    not matched; masked entities never expose the raw value in maskedValue.
    """
    name = entity.get("name", "")
    kind = entity.get("kind", "custom")
    data_type = entity.get("dataType") or entity.get("data_type") or "text"
    lowered = text.lower()

    matched_value: str | None = None
    method = ""

    # 1. Explicit regex wins.
    pattern = entity.get("regexPattern") or entity.get("regex_pattern")
    if kind == "regex" and not pattern:
        pattern = None
    if pattern:
        try:
            m = re.search(pattern, text, re.IGNORECASE)
        except re.error:
            m = None
        if m:
            matched_value = m.group(1) if m.groups() else m.group(0)
            method = "regex"

    # 2. Allowed values + synonyms lexicon.
    if matched_value is None:
        allowed = entity.get("allowedValues") or entity.get("allowed_values") or []
        synonyms = entity.get("synonyms") or {}
        lexicon: list[tuple[str, str]] = [(v, v) for v in allowed]
        for canonical, alts in (synonyms or {}).items():
            lexicon.append((canonical, canonical))
            for alt in alts or []:
                lexicon.append((alt, canonical))
        for surface, canonical in sorted(lexicon, key=lambda p: -len(p[0])):
            if surface and surface.lower() in lowered:
                matched_value = canonical
                method = "lexicon"
                break

    # 3. Built-in data-type pattern.
    if matched_value is None:
        builtin = _TYPE_PATTERNS.get(data_type)
        if builtin:
            m = re.search(builtin, text, re.IGNORECASE)
            if m:
                matched_value = m.group(1) if m.groups() else m.group(0)
                method = "type_pattern"

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
    }


def extract_entities(text: str, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [extract_entity(text, e) for e in entities]
