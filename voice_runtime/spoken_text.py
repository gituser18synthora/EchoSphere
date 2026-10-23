"""What the bot has just said, as comparable words — the reference the Sarvam
realtime STT adapter uses to recognise its own echo in a transcript.

On a phone leg the bot's reply comes back on the caller's line (no AEC on
the telephony path). The caller-audio gate removes most of it, but what
passes reaches the STT as if the caller had said it. The TTS router feeds
every text it is about to speak into one ``SpokenTextReference`` per call;
the realtime adapter asks how many words of a transcript that arrived while
the bot was speaking (or within the echo tail after it stopped) are among the
words recently spoken. At or above ``ECHO_TEXT_OVERLAP`` the transcript is
the bot's own voice.

Words are compared after normalisation: lower-cased, Devanagari
transliterated to Latin letters (a compact table — this is a comparison key,
not a romanisation), punctuation and the danda stripped, repeated letters
collapsed ("haan" == "han" == the key of the Devanagari spelling).
"""

from __future__ import annotations

import re
from collections import deque

#: Share of a transcript's words that must be recently spoken bot words for
#: the transcript to count as echo.
ECHO_TEXT_OVERLAP = 0.6
#: A one-word transcript ("haan", "nahi") is too short to call echo.
ECHO_TEXT_MIN_WORDS = 2
#: Words kept: the TTS router adds a reply when it starts synthesising it,
#: well before its last sentence is played, so the window must hold a whole
#: long reply (the longest greeting in use is ~50 words).
DEFAULT_WINDOW_WORDS = 60

# Devanagari → Latin comparison keys. Inherent vowels are not inserted and
# aspiration/retroflex distinctions are flattened, which is what makes the
# key robust to Sarvam's own spelling variation.
_DEVANAGARI: dict[str, str] = {
    "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e",
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
    "ं": "n", "ँ": "n", "ः": "h",
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f", "य़": "y",
    "०": "0", "१": "1", "२": "2", "३": "3", "४": "4", "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",
}
_DEVANAGARI_BLOCK = re.compile(r"[ऀ-ॿ]")
_NOT_KEY = re.compile(r"[^a-z0-9]+")
_REPEATS = re.compile(r"(.)\1+")


def transliterate(text: str) -> str:
    """Devanagari characters → Latin comparison letters; others unchanged.
    Marks without a key (virama, nukta, danda) are dropped."""
    out: list[str] = []
    for ch in text:
        key = _DEVANAGARI.get(ch)
        if key is not None:
            out.append(key)
        elif _DEVANAGARI_BLOCK.match(ch):
            continue
        else:
            out.append(ch)
    return "".join(out)


def normalize_word(word: str) -> str:
    """The comparison key of one word ("" when nothing is left)."""
    key = transliterate(word.lower())
    key = _NOT_KEY.sub("", key)
    return _REPEATS.sub(r"\1", key)


def normalize_words(text: str) -> list[str]:
    """Comparison keys of the words of ``text`` (empty keys dropped)."""
    return [key for key in (normalize_word(w) for w in (text or "").split()) if key]


class SpokenTextReference:
    """The most recent words the bot was given to speak, for echo matching."""

    def __init__(self, window_words: int = DEFAULT_WINDOW_WORDS) -> None:
        self._words: deque[str] = deque(maxlen=max(1, int(window_words)))
        self._added = 0

    def add(self, text: str) -> None:
        """Record the words of a text the TTS is about to speak."""
        for key in normalize_words(text):
            self._words.append(key)
            self._added += 1

    @property
    def words(self) -> list[str]:
        return list(self._words)

    @property
    def added(self) -> int:
        """Words recorded over the call (evidence for the call review)."""
        return self._added

    def overlap(self, text: str) -> float:
        """Share of ``text``'s words that are among the recently spoken words."""
        words = normalize_words(text)
        if not words or not self._words:
            return 0.0
        spoken = set(self._words)
        return sum(1 for w in words if w in spoken) / len(words)

    def is_echo(self, text: str) -> bool:
        """Whether ``text`` is (mostly) the bot's own words.

        Echo is the bot's sentence from some point on, so it STARTS with a
        spoken word; a caller talking over the bot starts with their own
        ("haan, bataya gaya tha" repeats three bot words but is not echo).
        """
        words = normalize_words(text)
        if len(words) < ECHO_TEXT_MIN_WORDS or not self._words:
            return False
        if words[0] not in set(self._words):
            return False
        return self.overlap(text) >= ECHO_TEXT_OVERLAP

    def clear(self) -> None:
        self._words.clear()


__all__ = [
    "DEFAULT_WINDOW_WORDS", "ECHO_TEXT_MIN_WORDS", "ECHO_TEXT_OVERLAP",
    "SpokenTextReference", "normalize_word", "normalize_words", "transliterate",
]
