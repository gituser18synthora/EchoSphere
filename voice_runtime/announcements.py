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
    r"|(?:(?:yeh|ye|yah|aapki)\s+)?\bcall\s+record\s+(?:ho\s+rahi\s+hai|ki\s+ja\s+rahi\s+hai)",
    re.IGNORECASE,
)

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
