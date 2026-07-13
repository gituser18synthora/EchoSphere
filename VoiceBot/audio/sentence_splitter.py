"""Split LLM response text into speakable sentences (stdlib only)."""

from __future__ import annotations

import re

_ABBREV_ENDINGS = (
    "Mr.",
    "Mrs.",
    "Ms.",
    "Dr.",
    "Sr.",
    "Jr.",
    "vs.",
    "etc.",
    "e.g.",
    "i.e.",
)

_SPLIT_RE = re.compile(r"(?<=[.?!])\s+")


def _ends_with_continuator(segment: str) -> bool:
    s = segment.rstrip()
    for tok in _ABBREV_ENDINGS:
        if s.endswith(tok):
            return True
    return bool(re.search(r"\d\.\d$", s))


def split_into_sentences(text: str) -> list[str]:
    stripped = (text or "").strip()
    if not stripped:
        return []

    parts = _SPLIT_RE.split(stripped)
    merged: list[str] = []
    current = ""
    for part in parts:
        if not current:
            current = part
        else:
            current = f"{current} {part}"
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


if __name__ == "__main__":
    assert split_into_sentences("Hello world. How are you?") == [
        "Hello world.",
        "How are you?",
    ]
    assert split_into_sentences("Contact Dr. Smith today.") == [
        "Contact Dr. Smith today.",
    ]
    assert split_into_sentences("The value is 3.5 units.") == [
        "The value is 3.5 units.",
    ]
    assert split_into_sentences("No terminal punctuation here") == [
        "No terminal punctuation here",
    ]
    assert split_into_sentences("") == []
    assert split_into_sentences("   ") == []
