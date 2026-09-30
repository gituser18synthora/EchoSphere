"""Sarvam STT key-term biasing (``keyterms``) — the single rule set.

Sarvam's ``saaras:v4`` accepts a list of domain terms (names, places, brands,
technical vocabulary) that bias recognition toward them. The documented
contract (docs.sarvam.ai, sarvamai SDK 0.1.35, re-verified 2026-10-01):

- supported ONLY with ``model=saaras:v4`` (REST, batch and streaming);
- up to 50 terms, each up to 64 characters;
- one term or phrase per list item ("New Delhi"), never comma-joined strings;
- streaming sends them as ONE JSON-encoded array in the ``keyterms`` query
  parameter of the WebSocket URL; REST/batch send a JSON array;
- biasing only — a term is never guaranteed to appear in the transcript.

The live endpoint does not reject over-limit or comma-joined lists at
handshake time (probed 2026-10-01: a 51-term list, a 65-character term and a
plain comma-joined string all connected), so these limits are enforced here,
once, for the API (save-time validation), the realtime adapter and the REST
transcriber alike. Every other Sarvam STT model is treated as unsupported and
never receives the parameter.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

#: Sarvam STT models that accept ``keyterms``.
KEYTERMS_SUPPORTED_MODELS: frozenset[str] = frozenset({"saaras:v4"})

#: Documented limits.
MAX_KEYTERMS = 50
MAX_KEYTERM_CHARS = 64

#: The ``stt_settings`` key the feature lives under (bot STT configuration).
KEYTERMS_SETTING = "keyterms"


def supports_keyterms(model: str | None) -> bool:
    """True when the Sarvam STT model accepts key-term biasing."""
    return (model or "").strip().lower() in KEYTERMS_SUPPORTED_MODELS


def normalize_keyterms(raw: object) -> list[str]:
    """Clean a configured list: trim, drop blanks, drop exact duplicates.

    Order is preserved. Non-list input (``None``, a bare string, a number)
    yields an empty list — the strict type check happens in
    :func:`keyterm_problems`, this helper only shapes a usable value. No
    truncation is done here on purpose: silently dropping an operator's
    terms would hide a configuration error that validation must report.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        term = " ".join(item.split())
        if not term or term in seen:
            continue
        seen.add(term)
        cleaned.append(term)
    return cleaned


def keyterm_problems(raw: object, *, prefix: str = "STT") -> list[str]:
    """Validation messages for a configured ``keyterms`` value (empty = OK).

    Checks the raw value (not the normalized one) so a bad entry type is
    reported rather than quietly discarded; limits are evaluated on the
    normalized list so whitespace and duplicates never count against them.
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        return [f"{prefix}: 'keyterms' must be a list of terms."]
    problems: list[str] = []
    if any(not isinstance(item, str) for item in raw):
        problems.append(f"{prefix}: every 'keyterms' entry must be a string.")
        return problems
    terms = normalize_keyterms(raw)
    if len(terms) > MAX_KEYTERMS:
        problems.append(
            f"{prefix}: 'keyterms' allows at most {MAX_KEYTERMS} terms "
            f"({len(terms)} given)."
        )
    too_long = [t for t in terms if len(t) > MAX_KEYTERM_CHARS]
    if too_long:
        problems.append(
            f"{prefix}: each 'keyterms' entry may have at most {MAX_KEYTERM_CHARS} "
            f"characters (too long: '{too_long[0][:40]}…')."
        )
    comma_joined = [t for t in terms if "," in t]
    if comma_joined:
        problems.append(
            f"{prefix}: put one term or phrase per 'keyterms' entry — "
            f"'{comma_joined[0][:40]}' looks like several comma-joined terms."
        )
    return problems


def encode_keyterms_query(terms: Iterable[str]) -> str:
    """The streaming wire form: one JSON-encoded array string.

    Non-ASCII terms (Devanagari brand names, etc.) are kept as-is — the SDK
    URL-encodes the query value, and the endpoint accepted them in the probe.
    """
    return json.dumps(list(terms), ensure_ascii=False)
