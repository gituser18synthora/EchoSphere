"""Content-word evidence for conversation-language decisions.

``meaningful_language_words`` (shared.orchestration.spoken_numbers) strips
the numeric/technical payload of an utterance; what remains still includes
fillers and bare acknowledgements — "hmm", "haan", "ok", "ya" — which carry
no language evidence of their own. Sarvam's auto-detector labels such
low-content segments essentially at random ("Hmm hmm try ya" → en-IN on a
Hindi call, 2026-09-18, vs_6Mh-x1iTM4glTaXTqv7KQvSa), and the two-word
minimum let that label flip the call's language.

:func:`content_words` removes those fillers so the language follower can
demand real linguistic content before switching. Pure lexical filter — no
detection is redesigned here.
"""

from __future__ import annotations

import re

# Interjections, hesitations and acknowledgement particles in the platform's
# languages (Latin/romanized and native script). Kept small and obvious: a
# word here is one nobody would identify a language from. Negations and
# plain yes/no ("nahi", "नहीं", "no", "yes") are deliberately NOT here: they
# are real words of their language ("नहीं मैं नहीं करूँगा" must still switch).
_FILLER_WORDS: frozenset[str] = frozenset({
    # hesitations / hums
    "hmm", "hm", "hmmm", "mm", "mmm", "mhm", "um", "umm", "uh", "uhh", "ah",
    "aa", "aah", "oh", "ohh", "eh", "er", "huh", "haha",
    # acknowledgement particles (Latin + romanized)
    "ya", "yah", "yaa", "yeah", "yep", "ok", "okay", "kay",
    "haan", "han", "ha", "hanji", "haanji", "ji", "jee",
    "achha", "acha", "accha", "theek", "thik", "hello", "hallo", "hi",
    "sir", "madam", "maam", "ma'am", "right", "fine", "so", "like", "bas",
    # Devanagari
    "हाँ", "हां", "जी", "हम्म", "हम", "अच्छा", "ठीक", "ओके",
    "हेलो", "हैलो", "सर", "मैडम", "बस",
    # Malayalam / Tamil acknowledgement particles
    "ശരി", "സരി", "சரி",
})

# Explicit punctuation set: a ``\W``-based strip would also remove Devanagari
# vowel signs and the chandrabindu (combining marks are not ``\w``), turning
# "हाँ" into "हा" and hiding it from the filler list.
_PUNCT = ".,!?;:।॥\"'`()[]{}<>-–—…/\\|*_~"


def content_words(words: list[str] | tuple[str, ...]) -> list[str]:
    """Tokens that carry linguistic content — fillers and bare acks removed.

    Input is the ``meaningful_language_words`` list (already lowercased and
    free of numeric/technical payload); output keeps the original tokens.
    """
    out: list[str] = []
    for token in words:
        word = str(token).strip(_PUNCT).lower()
        if not word or word in _FILLER_WORDS:
            continue
        # A repeated-letter hum ("aaa", "mmm") is not a word. ("I" and "a"
        # are real English words and stay.)
        if re.fullmatch(r"([a-z])\1+", word):
            continue
        out.append(token)
    return out
