"""TTS text preparation: sanitizing and sentence splitting.

Ported from the legacy VoiceBot ``audio/tts_text.py`` and
``audio/sentence_splitter.py``. Pure functions, stdlib only, Indic-script
aware (Devanagari danda/double-danda are treated as sentence terminators and
Devanagari text gets a danda when terminal punctuation must be added).
"""

from __future__ import annotations

import re

_INVISIBLE_RE = re.compile(
    r"[\u200b-\u200f\u2028-\u202f\u2060-\u2064\u2066-\u2069\ufeff\u00ad]+"
)

_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

_TERMINAL_PUNCT = frozenset(".?!…।॥")

# Fixed-width lookbehinds only — just digit and single capital letter.
# Abbreviations are handled by _is_abbreviation_boundary() below.
_SENTENCE_END_RE = re.compile(r"(?<!\d)(?<![A-Z])[.?!…।॥](?:\s|$)")

_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "sr", "jr", "vs",
    "etc", "e.g", "i.e", "prof", "rev", "gen",
    "fig", "approx", "dept", "est",
})

_LEAD_IN_WORDS = frozenset({
    "certainly", "sure", "absolutely", "ofcourse", "of course",
    "great", "okay", "ok", "alright", "noted", "understood",
    "yes", "no", "right", "exactly", "indeed", "perfect",
    "thanks", "thank you", "sorry", "apologies",
})

_LEAD_IN_ONLY_RE = re.compile(
    r"^(?:" + "|".join(re.escape(w) for w in _LEAD_IN_WORDS) + r")[.!?,\s]*$",
    re.IGNORECASE,
)

_LIST_ITEM_RE = re.compile(r"(?:^|\s)(?:\d+|[A-Za-z])[.)]\s+")

_ABBREV_ENDINGS = (
    "Mr.", "Mrs.", "Ms.", "Dr.", "Sr.", "Jr.", "vs.", "etc.", "e.g.", "i.e.",
)

_SIMPLE_SPLIT_RE = re.compile(r"(?<=[.?!])\s+")


def sanitize_for_tts(text: str, *, ensure_terminal_punct: bool = False) -> str:
    """Normalize LLM output for speech synthesis without altering spoken content."""
    if not text:
        return ""
    cleaned = text.replace("\u00a0", " ")
    cleaned = _INVISIBLE_RE.sub("", cleaned)
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    cleaned = re.sub(r" +", " ", cleaned).strip()
    if ensure_terminal_punct and cleaned and not _has_terminal_punct(cleaned):
        cleaned = cleaned + _default_terminal_punct(cleaned)
    return cleaned


def _has_terminal_punct(text: str) -> bool:
    stripped = text.rstrip()
    return bool(stripped) and stripped[-1] in _TERMINAL_PUNCT


def _default_terminal_punct(text: str) -> str:
    if _DEVANAGARI_RE.search(text):
        return "।"
    return "."


def _is_lead_in_only(sentence: str) -> bool:
    return bool(_LEAD_IN_ONLY_RE.match(sentence.strip()))


def _contains_list_item(text: str) -> bool:
    return bool(_LIST_ITEM_RE.search(text))


def _is_abbreviation_boundary(text: str, match_start: int) -> bool:
    """True when the punctuation at ``match_start`` belongs to an abbreviation."""
    before = text[:match_start].rstrip()
    if not before:
        return False
    last_word_match = re.search(r"(\w[\w.]*)$", before)
    if not last_word_match:
        return False
    return last_word_match.group(1).lower() in _ABBREVIATIONS


def _split_sentences(text: str) -> list[str]:
    """Split on sentence boundaries, skipping abbreviation periods.

    Terminal punctuation stays attached to its sentence.
    """
    results: list[str] = []
    last = 0
    for match in _SENTENCE_END_RE.finditer(text):
        if _is_abbreviation_boundary(text, match.start()):
            continue
        end = match.start() + 1
        chunk = text[last:end].strip()
        if chunk:
            results.append(chunk)
        last = end
    remainder = text[last:].strip()
    if remainder:
        results.append(remainder)
    return results


def split_for_tts(text: str) -> list[str]:
    """Split LLM output into TTS-ready chunks.

    Handles numbered/lettered lists (kept as one chunk), lead-in affirmations
    (joined with the next sentence), abbreviations (never split), and normal
    sentence boundaries on ``. ? ! … । ॥``.
    """
    if not text:
        return []
    text = sanitize_for_tts(text)
    if not text:
        return []

    if _contains_list_item(text):
        return [text]

    raw_sentences = _split_sentences(text)

    merged: list[str] = []
    i = 0
    while i < len(raw_sentences):
        sentence = raw_sentences[i].strip()
        if not sentence:
            i += 1
            continue
        if _is_lead_in_only(sentence) and i + 1 < len(raw_sentences):
            next_sentence = raw_sentences[i + 1].strip()
            merged.append(f"{sentence} {next_sentence}".strip())
            i += 2
        else:
            merged.append(sentence)
            i += 1

    return [s for s in merged if s]


def truncate_at_sentence_boundary(text: str, max_words: int = 100) -> str:
    """Truncate to at most ``max_words`` words, cutting at a sentence boundary."""
    words = text.split()
    if len(words) <= max_words:
        return text
    chunk = " ".join(words[:max_words])
    matches = [
        m for m in _SENTENCE_END_RE.finditer(chunk)
        if not _is_abbreviation_boundary(chunk, m.start())
    ]
    if matches:
        end = matches[-1].start() + 1
        return chunk[:end].strip()
    return sanitize_for_tts(chunk, ensure_terminal_punct=True)


def _ends_with_continuator(segment: str) -> bool:
    stripped = segment.rstrip()
    for token in _ABBREV_ENDINGS:
        if stripped.endswith(token):
            return True
    return bool(re.search(r"\d\.\d$", stripped))


def split_into_sentences(text: str) -> list[str]:
    """Lightweight sentence splitter on ``. ? !`` with abbreviation/decimal merging."""
    stripped = (text or "").strip()
    if not stripped:
        return []

    parts = _SIMPLE_SPLIT_RE.split(stripped)
    merged: list[str] = []
    current = ""
    for part in parts:
        current = part if not current else f"{current} {part}"
        if not _ends_with_continuator(current):
            piece = current.strip()
            if piece:
                merged.append(piece)
            current = ""

    tail = current.strip()
    if tail:
        merged.append(tail)

    if not merged:
        return [stripped]
    return merged
