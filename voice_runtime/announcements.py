"""Telephony recording announcements heard as caller speech.

Some carriers, dialers and handsets play "Call is now being recorded" (or a
close variant) on the CALLER's leg the moment a call connects. The STT hears
it exactly like speech, so without this filter the announcement word-confirms
a barge-in, cuts the greeting, becomes a user turn and reaches the LLM — which
then answers a recording notice in English (observed live on every Zepto
call of 2026-09-02).

The matcher is deliberately narrow: a fixed family of recording/monitoring
notices in English and Hindi. Anything else in the same segment ("Call is now
being recorded. बताइए।") is real speech and is kept.
"""

import re

RECORDING_ANNOUNCEMENT_RE = re.compile(
    # "call is (now) (being) recorded", "this call may be recorded",
    # "your call will be recorded and monitored for quality purposes"
    r"(?:\b(?:this|your|the)\s+)?\bcall\s+"
    r"(?:is\s+(?:now\s+)?(?:being\s+)?|may\s+be\s+|will\s+be\s+|might\s+be\s+)"
    r"(?:recorded|monitored)"
    r"(?:\s+(?:and|or)\s+(?:recorded|monitored))?"
    r"(?:\s+for\s+(?:quality|training|security|compliance)[^.।!?]*)?"
    # "this call is recorded" / "call recording is on"
    r"|(?:\b(?:this|your|the)\s+)?\bcall\s+recording\s+(?:is\s+)?(?:on|enabled|active)"
    # Hindi: "यह कॉल रिकॉर्ड की जा रही है", "कॉल रिकॉर्ड हो रही है"
    r"|(?:(?:यह|ये|आपकी)\s+)?कॉल\s+(?:अब\s+)?रिकॉर्ड\s+(?:की\s+जा\s+रही\s+है|हो\s+रही\s+है)"
    # Hinglish: "call record ho rahi hai", "yeh call record ki ja rahi hai"
    r"|(?:(?:yeh|ye|yah|aapki)\s+)?\bcall\s+record\s+(?:ho\s+rahi\s+hai|ki\s+ja\s+rahi\s+hai)"
    # Malayalam-script transliteration of the English notice, as a Sarvam
    # stream pinned to ml-IN writes it (observed 2026-09-23 on the mPokket
    # bot: "കോൾ ഈസ് നൗ ബി. recorded", "കോൾ ഈസ് നൗ ബീയിങ് റെക്കോർഡ്."):
    # "കോൾ ഈസ്/മേ ബി/വിൽ ബി (നൗ) (ബീയിങ്) recorded|റെക്കോർഡ്…"
    r"|(?:(?:ഈ|ദിസ്|യുവർ|ദ)\s+)?ക[ോാ]ൾ\s+"
    r"(?:ഈസ്|ഇസ്|മേ\s+ബ[ിീ]|വിൽ\s+ബ[ിീ]|വില്\s+ബ[ിീ])\s*"
    r"(?:ന[ൗവ][ഀ-ൿ]*\s*)?"
    r"(?:ബ[ിീ][ഀ-ൿ]*\.?\s*)?"
    r"(?:recorded|റെക്കോ[ഀ-ൿ]*|റിക്കോ[ഀ-ൿ]*|റെകോ[ഀ-ൿ]*)",
    re.IGNORECASE,
)

# The notice's trailing clause on its own. The barge-in flush (0.7 s while
# the bot speaks) can split the notice into two finals; the first half
# matches above and is dropped, but "for quality and training purposes"
# then arrives alone and used to become a caller turn (2026-09-24 telephony
# run). A segment that is NOTHING BUT this clause is announcement debris.
_TAIL_TOPICS = r"(?:quality|training|security|compliance|monitoring)"
RECORDING_ANNOUNCEMENT_TAIL_RE = re.compile(
    r"^\s*(?:(?:and|or)\s+)?(?:"
    # "for quality and training purposes" / "for training"
    r"for\s+" + _TAIL_TOPICS + r"(?:\s*(?:and|or|,)\s*" + _TAIL_TOPICS + r")*(?:\s+(?:assurance\s+)?purposes?)?"
    # "quality and training purposes" (the word "purposes" is required here)
    r"|" + _TAIL_TOPICS + r"(?:\s*(?:and|or|,)\s*" + _TAIL_TOPICS + r")*\s+(?:assurance\s+)?purposes?"
    r")[\s.,;:!?।]*$",
    re.IGNORECASE,
)
# A bare "recorded" / "monitored" is deliberately NOT a tail on its own: the
# brain reassembles "Call is now being" + "recorded" at dispatch and judges the
# whole utterance; rejecting the second half here would let the first half run
# as a caller turn.

_EDGE_PUNCT = re.compile(r"^[\s.,;:!?।\-–—]+|[\s.,;:!?।\-–—]+$")
_HAS_CONTENT = re.compile(r"[\wऀ-ॿ]")


def strip_recording_announcement(text: str) -> tuple[str, bool]:
    """Remove recording announcements from one STT segment.

    Returns ``(remainder, matched)``: ``matched`` is whether an announcement
    was found; ``remainder`` is whatever real speech is left, with dangling
    punctuation trimmed ("" when the segment was only the announcement).
    """
    stripped = (text or "").strip()
    if not stripped:
        return "", False
    remainder, count = RECORDING_ANNOUNCEMENT_RE.subn(" ", stripped)
    if not count:
        if _HAS_CONTENT.search(stripped) and RECORDING_ANNOUNCEMENT_TAIL_RE.match(stripped):
            # Only the notice's trailing clause survived a split final.
            return "", True
        return stripped, False
    remainder = _EDGE_PUNCT.sub("", " ".join(remainder.split()))
    if not _HAS_CONTENT.search(remainder):
        remainder = ""
    return remainder, True


def is_recording_announcement(text: str) -> bool:
    """Whether the segment is NOTHING BUT a recording announcement."""
    remainder, matched = strip_recording_announcement(text)
    return matched and not remainder


def speech_word_count(text: str) -> int:
    """Words in the segment that are actual speech (announcements excluded).

    Used by the barge-in word gate so a recording notice can never confirm an
    interruption of the bot's audio on its own.
    """
    remainder, _matched = strip_recording_announcement(text)
    return len(remainder.split())


__all__ = [
    "RECORDING_ANNOUNCEMENT_RE",
    "is_recording_announcement",
    "speech_word_count",
    "strip_recording_announcement",
]
