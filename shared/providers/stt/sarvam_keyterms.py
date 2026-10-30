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
import re
from collections.abc import Iterable, Mapping

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


# ── automatic key terms (no operator list configured) ────────────────────────
#: The ``stt_settings`` switch for automatic key terms (default on).
KEYTERMS_AUTO_SETTING = "keyterms_auto"
#: Opt-in: also bias toward the bot's yes/no words. OFF by default — replaying
#: two live calls (cv_0f224432545d, cv_2d1119054a92; 2026-10-23, two passes)
#: through saaras:v4 with the yes/no lexicon in the list produced 8 false
#: "हाँ" / "जी हाँ" / "Yes." finals per pass on hums, "तो" and line noise
#: (3 without it, 1–2 on saaras:v3) — a false yes confirms a yes/no question.
KEYTERMS_AUTO_YES_NO_SETTING = "keyterms_auto_yes_no"
#: Edge labels that are routing words, not something a caller says.
_ROUTING_LABELS = frozenset({"else", "true", "false", "default", "fallback", "otherwise"})
#: Alternatives inside one authored label ("haan/haan ji/yes").
_LABEL_SPLIT = re.compile(r"\s*[/|]\s*")
#: "yes (reached the location)" → "yes"
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")


def _phrases_of(
    workflows: Iterable[Mapping], slot_names: Iterable[str], intents: Iterable[Mapping] = (),
) -> list[str]:
    """Everything a caller is expected to SAY to this bot: the alternatives of
    every authored option label (intent-hub edges, node ``options``), every
    entity synonym and canonical value of ask nodes (including ``alsoCapture``
    entities), the slot names in words, and — so a bot without workflows still
    gets its vocabulary — the sample phrases of its configured intents."""
    phrases: list[str] = []

    def _add(text: object) -> None:
        for part in _LABEL_SPLIT.split(str(text or "")):
            part = " ".join(_PARENTHETICAL.sub("", part).split())
            if part and part.lower() not in _ROUTING_LABELS:
                phrases.append(part)

    for workflow in workflows or ():
        for edge in workflow.get("edges") or ():
            if isinstance(edge, Mapping):
                _add(edge.get("label"))
        for node in workflow.get("nodes") or ():
            if not isinstance(node, Mapping):
                continue
            config = node.get("config") or {}
            if not isinstance(config, Mapping):
                continue
            for option in config.get("options") or ():
                _add(option.get("label") if isinstance(option, Mapping) else option)
            entities = [config.get("entity")] + [
                extra.get("entity") for extra in config.get("alsoCapture") or ()
                if isinstance(extra, Mapping)
            ]
            for entity in entities:
                if not isinstance(entity, Mapping):
                    continue
                for canonical, synonyms in (entity.get("synonyms") or {}).items():
                    _add(canonical)
                    for synonym in synonyms or ():
                        _add(synonym)
    for name in slot_names or ():
        _add(str(name or "").replace("_", " "))
    for intent in intents or ():
        if isinstance(intent, Mapping):
            for sample in intent.get("samples") or ():
                _add(sample)
    return phrases


def derive_keyterms(
    *,
    workflows: Iterable[Mapping] = (),
    slot_names: Iterable[str] = (),
    intents: Iterable[Mapping] = (),
    languages: Iterable[str] = (),
    include_yes_no: bool = False,
    limit: int = MAX_KEYTERMS,
) -> list[str]:
    """Key terms for a bot that has none configured.

    The content words that recur most often across what callers are expected
    to say to the bot (:func:`_phrases_of`), ranked by how many distinct
    phrases use them. Grammatical glue, fillers and generic answer words of
    the bot's languages never qualify; nor do digits or one-/two-letter Latin
    tokens. ``include_yes_no`` (opt-in, see ``KEYTERMS_AUTO_YES_NO_SETTING``)
    puts the canonical yes/no forms of the bot's language packs first.
    Deterministic, within Sarvam's limits.
    """
    from shared.orchestration import lang as language_packs

    packs = language_packs.packs([str(code) for code in languages or ()] or None)
    lexicon: list[str] = []
    if include_yes_no:
        for pack in packs:
            for form in sorted(pack.canonical_yes) + sorted(pack.canonical_no):
                if form not in lexicon:
                    lexicon.append(form)
    noise: set[str] = set()
    for pack in packs:
        for attr in ("fillers", "strip_fillers", "stop_tokens", "generic_answer_tokens",
                     "bare_yes", "bare_no", "canonical_yes", "canonical_no", "function_words"):
            noise.update(word.lower() for word in getattr(pack, attr, ()) or ())
    token = re.compile(r"[\w" + language_packs.letters() + r"]+")
    document_frequency: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    surface: dict[str, str] = {}
    for phrase in dict.fromkeys(_phrases_of(workflows, slot_names, intents)):
        for word in dict.fromkeys(token.findall(phrase)):
            key = word.lower()
            if key in noise or any(ch.isdigit() for ch in key) or "_" in key:
                continue
            if key.isascii() and len(key) < 3:
                continue
            document_frequency[key] = document_frequency.get(key, 0) + 1
            first_seen.setdefault(key, len(first_seen))
            surface.setdefault(key, word if not word.isascii() else key)
    ranked = sorted(document_frequency, key=lambda k: (-document_frequency[k], first_seen[k]))
    terms = [t for t in lexicon if len(t) <= MAX_KEYTERM_CHARS]
    for key in ranked:
        if len(terms) >= limit:
            break
        word = surface[key]
        if len(word) <= MAX_KEYTERM_CHARS and word not in terms:
            terms.append(word)
    return normalize_keyterms(terms[:limit])

