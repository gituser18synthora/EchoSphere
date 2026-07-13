"""Prepare LLM text for TTS without altering Indic scripts or spoken content."""

from __future__ import annotations

import re

_INVISIBLE_RE = re.compile(
    r"[\u200b-\u200f\u2028-\u202f\u2060-\u2064\u2066-\u2069\ufeff\u00ad]+"
)

_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

_TERMINAL_PUNCT = frozenset(".?!…।॥")

# Fixed-width lookbehinds only — just digit and single capital letter.
# Abbreviations are handled by _is_abbreviation_boundary() below.
_SENTENCE_END_RE = re.compile(
    r"(?<!\d)(?<![A-Z])[.?!…।॥](?:\s|$)"
)

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


def sanitize_for_tts(text: str, *, ensure_terminal_punct: bool = False) -> str:
    if not text:
        return ""
    t = text.replace("\u00a0", " ")
    t = _INVISIBLE_RE.sub("", t)
    t = re.sub(r"[\r\n\t]+", " ", t)
    t = re.sub(r" +", " ", t).strip()
    if ensure_terminal_punct and t and not _has_terminal_punct(t):
        t = t + _default_terminal_punct(t)
    return t


def _has_terminal_punct(text: str) -> bool:
    s = text.rstrip()
    return bool(s) and s[-1] in _TERMINAL_PUNCT


def _default_terminal_punct(text: str) -> str:
    if _DEVANAGARI_RE.search(text):
        return "।"
    return "."


def _is_lead_in_only(sentence: str) -> bool:
    return bool(_LEAD_IN_ONLY_RE.match(sentence.strip()))


def _contains_list_item(text: str) -> bool:
    return bool(_LIST_ITEM_RE.search(text))


def _is_abbreviation_boundary(text: str, match_start: int) -> bool:
    """
    Return True if the punctuation at match_start is part of an abbreviation
    and should NOT be treated as a sentence boundary.
    Checks the word immediately before the punctuation character.
    """
    before = text[:match_start].rstrip()
    if not before:
        return False
    # Extract the last word before the punctuation
    last_word_match = re.search(r"(\w[\w.]*)$", before)
    if not last_word_match:
        return False
    last_word = last_word_match.group(1).lower()
    return last_word in _ABBREVIATIONS


def _split_sentences(text: str) -> list[str]:
    """
    Split text on sentence boundaries, skipping abbreviation periods.
    Terminal punctuation stays attached to its sentence.
    """
    results = []
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
    """
    Split LLM output into TTS-ready chunks handling:
      - Numbered/lettered lists  (1. 2. a. B.) — kept as one chunk
      - Lead-in affirmations     (Certainly. Sure! Okay.) — joined with next sentence
      - Abbreviations            (Mr. Mrs. Dr. etc.) — not split
      - Normal sentence splits   on . ? ! … । ॥
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


if __name__ == "__main__":
    # Numbered list — must NOT split
    result = split_for_tts("Here are three plans. 1. Jeevan Anand 2. Tech Term 3. Bima Jyoti")
    assert len(result) == 1, f"Expected 1 chunk for list, got {result}"

    # Lead-in join — Certainly. must merge with next sentence
    result = split_for_tts("Certainly. Here are the following policies.")
    assert len(result) == 1, f"Expected 1 chunk for lead-in, got {result}"

    # Sure! join
    result = split_for_tts("Sure! This is your email.")
    assert len(result) == 1, f"Expected 1 chunk for Sure!, got {result}"

    # Normal split — two real sentences
    result = split_for_tts("Jeevan Anand covers your family. It also builds savings.")
    assert len(result) == 2, f"Expected 2 chunks, got {result}"

    # Decimal — must not split
    result = split_for_tts("The premium is 3.5 lakhs per year.")
    assert len(result) == 1, f"Decimal split incorrectly: {result}"

    # Abbreviation — must not split
    result = split_for_tts("Speak to Dr. Sharma about this.")
    assert len(result) == 1, f"Abbreviation split incorrectly: {result}"

    # Empty input
    result = split_for_tts("")
    assert result == [], f"Expected empty list, got {result}"

    # No terminal punctuation
    result = split_for_tts("This is a sentence without punctuation")
    assert len(result) == 1, f"Expected 1 chunk for no-punct, got {result}"

    print("All assertions passed.")