"""Utterance-completeness heuristics for adaptive endpointing.

End-of-turn detection is a trade-off with exactly two failure modes: wait too
long and the bot feels slow; fire too early and the caller gets cut off
mid-thought. A single silence timeout has to be tuned for the worst case, which
is why a fixed window always feels sluggish on "हाँ" and still clips someone
dictating an account number.

This module lets the brain pick the window per utterance instead. It answers
one question — *does the text so far look like a finished thought?* — using
lexical cues for the three languages the platform serves (Hindi, English and
romanized Hinglish):

- :func:`ends_with_continuation_cue` — the utterance trails off on a
  conjunction, a filler, a comma or an unfinished number, so more is coming and
  the FULL pause window must be honoured;
- :func:`utterance_looks_complete` — a known short reply, or a sentence that
  closes on terminal punctuation, so the short window is safe.

Anything in between is "unknown" and keeps today's behaviour: wait for the turn
controller. Firing early is additionally underwritten by the brain's late-final
merge (a straggler segment rolls the turn back and re-runs it combined), so an
optimistic endpoint degrades into a merge rather than a talk-over.
"""

import re

from shared.orchestration.router import detect_hangup

# Words that essentially never end a finished spoken sentence in Hindi,
# Hinglish or English. A caller pausing right after one of these is thinking,
# not done. Kept deliberately conservative: a false "continuing" verdict only
# costs the normal pause window, while a false "complete" verdict risks a
# talk-over.
_CONTINUATION_WORDS = (
    # Devanagari
    "और", "लेकिन", "क्योंकि", "अगर", "तो", "कि", "मतलब", "फिर", "या",
    "जब", "पर", "इसलिए", "बल्कि", "जैसे", "अं", "हम्म",
    # Romanized Hindi / Hinglish
    "aur", "lekin", "kyunki", "kyuki", "kyonki", "agar", "toh", "ki",
    "matlab", "phir", "ya", "jab", "par", "isliye", "balki", "jaise",
    "yaani", "yani", "bas",
    # English
    "and", "but", "because", "if", "so", "that", "when", "or", "then",
    "however", "also", "actually", "like", "while", "since", "although",
    "though", "plus", "um", "umm", "uh", "uhh", "hmm", "well", "mean",
    "my", "the", "a", "an", "to", "of", "for", "with", "about",
)
_CONTINUATION_RE = re.compile(
    r"(?:^|\s)(" + "|".join(re.escape(word) for word in _CONTINUATION_WORDS) + r")$",
    re.IGNORECASE,
)
# Sentence-final punctuation, including the Devanagari danda.
_TERMINAL_RE = re.compile(r"[।.!?]$")
# Trailing digits: a caller reading out an amount, a date or an account number
# pauses between groups, and cutting in there is the worst possible moment.
_TRAILING_DIGITS_RE = re.compile(r"\d[\d\s,.-]*$")
_TRAILING_COMMA_RE = re.compile(r"[,;:—–-]$")
# STT providers commonly put terminal punctuation on every finalized segment,
# including a caller's lead-in immediately before dictating an identifier:
# "haan, order ID hai." / "हाँ, ऑर्डर आईडी है।".  The punctuation is not
# evidence that the thought is complete in this shape — the value is still to
# come, so honour the full natural-pause window instead of starting the bot.
# Keep this suffix generic (ID/number rather than any tenant/domain label) so
# booking, order, account, policy, claim and phone-number flows behave alike.
_IDENTIFIER_LEAD_IN_RE = re.compile(
    r"(?:^|\s)(?:"
    r"i[\s.\-]*d|number|no\.?|"
    r"आई\s*डी|आइ\s*डी|नंबर|नम्बर"
    r")\s*(?:is|hai|he|है|हैं)\s*$",
    re.IGNORECASE,
)

# Replies that are complete utterances on their own, matched against the WHOLE
# transcript. This is deliberately NOT the router's signal lexicon: that one
# matches semantic signals anywhere in a sentence ("मुझे पेमेंट करना है" is a
# payment_intent), which says nothing about whether the caller has finished
# talking. Only an utterance that consists entirely of one of these earns the
# short window.
_SHORT_REPLY_RE = re.compile(
    r"^\s*(?:"
    # Hinglish / romanized
    r"haa+n?|ha|hanji|ji|ji haa+n?|ji nahi+n?|nahi+n?|nhi|na|"
    # ac+h+a covers acha / accha / achha / acchha
    r"theek hai|thik hai|theek|thik|sahi hai|bilkul|ac+h+a|"
    # English
    r"yes|yeah|yep|yup|no|nope|nah|ok|okay|okey|sure|right|correct|done|"
    r"got it|alright|"
    # Devanagari
    r"हाँ|हां|हा|जी|जी हाँ|जी हां|जी नहीं|नहीं|नही|ना|ठीक है|ठीक|"
    r"बिलकुल|बिल्कुल|अच्छा|सही है|हो गया|ओके|ओके जी|हम्म|हम"
    r")\s*[।.!?,]*\s*$",
    re.IGNORECASE,
)


def is_short_complete_reply(text: str) -> bool:
    """Whether the whole utterance is a self-contained short reply.

    "haan", "nahi", "ji", "yes", "no", "ok", "ठीक है"… — the replies where a
    fixed pause window is felt most acutely, and which carry no continuation
    risk of their own.
    """
    return bool(_SHORT_REPLY_RE.match((text or "").strip()))


def ends_with_continuation_cue(text: str) -> bool:
    """Whether the utterance trails off mid-thought.

    True means "more is coming": the full pause window must be honoured even
    if the rest of the text would otherwise look like a complete sentence.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _TRAILING_COMMA_RE.search(stripped):
        return True
    if _TRAILING_DIGITS_RE.search(stripped):
        return True
    # Check semantic incompleteness BEFORE terminal punctuation gets its
    # normal precedence: Sarvam may have supplied that full stop/danda merely
    # because it flushed this audio segment at a brief caller pause.
    without_terminal = _TERMINAL_RE.sub("", stripped).rstrip()
    if _IDENTIFIER_LEAD_IN_RE.search(without_terminal):
        return True
    # Terminal punctuation overrides a trailing cue word ("...aur." is closed).
    if _TERMINAL_RE.search(stripped):
        return False
    return bool(_CONTINUATION_RE.search(stripped))


def utterance_looks_complete(text: str) -> bool:
    """Whether the utterance is confidently a finished thought.

    Only two kinds of evidence qualify, both strong:

    - a **self-contained short reply** ("haan", "nahi", "ji", "yes", "no",
      "ok", "ठीक है") or an explicit hang-up request. These are precisely the
      replies where a fixed window feels worst, and a caller who says "haan"
      and then continues is recovered by the late-final merge;
    - a sentence that **closes on terminal punctuation** and does not trail off
      on a continuation cue.

    Everything else returns False and keeps the conservative window.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    if ends_with_continuation_cue(stripped):
        return False
    if is_short_complete_reply(stripped) or detect_hangup(stripped):
        return True
    return bool(_TERMINAL_RE.search(stripped))
