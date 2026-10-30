"""Current date/time grounding for LLM turns — generic, config-gated.

A voice bot has no inherent sense of "now": questions involving today,
tomorrow, next Monday or an upcoming booking date hallucinate unless the
prompt carries the actual current date and time. This module renders that
fact as a system-prompt section, computed at GENERATION time (never cached
per call) in the tenant's configured timezone.

Enabled per bot via ``llm_settings.time_context_enabled`` — existing bots
are untouched until a tenant opts in. Nothing here is domain-specific: the
section states the clock fact and the grounding rule, and the bot's own
prompt decides what to do with it.
"""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TIME_CONTEXT_SETTING = "time_context_enabled"


def resolve_zone(timezone_name: str | None) -> ZoneInfo:
    """The tenant's IANA timezone; UTC when unset or invalid (fail open)."""
    try:
        return ZoneInfo(timezone_name or "UTC")
    except Exception:  # noqa: BLE001 — a config typo must not kill the call
        return ZoneInfo("UTC")


def time_context_section(
    timezone_name: str | None, *, now: datetime | None = None
) -> str:
    """The ``# Current date and time`` system-prompt block.

    ``now`` exists for tests; production callers omit it. The wording keeps
    every relative-date answer anchored to this value instead of the model's
    training-data guess.
    """
    zone = resolve_zone(timezone_name)
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    offset = current.strftime("%z")
    offset_label = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    stamp = current.strftime("%A, %d %B %Y, %I:%M %p").replace(" 0", " ")
    return (
        "\n\n# Current date and time\n"
        f"Right now it is {stamp} ({zone.key}, {offset_label}). "
        f"Today's date is {current.strftime('%Y-%m-%d')}.\n"
        "This is the ONLY source of the current date and time. Ground every "
        "relative expression — today, tonight, tomorrow, yesterday, weekday "
        "names, next week, 'in two days' — in this value, and compare any "
        "known date (a booking, check-in, due or delivery date) against it "
        "when the caller asks whether something is past, today, or upcoming. "
        "Never guess the current date from the conversation or from examples."
    )


# A caller asking what day/date/time it is RIGHT NOW is answered by the
# time context above, never by tenant-knowledge retrieval (which would miss
# and produce a canned "couldn't find that"). Deliberately conservative and
# multilingual (en / hinglish / Devanagari) — plain mentions of dates or
# times do not match, only questions about the current one.
_CURRENT_DATETIME_PATTERNS: tuple[re.Pattern, ...] = (
    # "what is today's date", "what's the date today", "today date?"
    re.compile(
        r"\btoday'?s?\s+date\b|\bdate\s+today\b"
        r"|\bwhat\s+(?:is\s+the\s+|is\s+)?date\b"
        r"|\bcurrent\s+(?:date|time|day)\b"
        r"|\bwhat\s+day\s+is\s+(?:it|today)\b|\bwhich\s+day\s+is\s+today\b"
        r"|\bwhat\s+time\s+is\s+it\b|\btime\s+right\s+now\b",
        re.I,
    ),
    # "aaj ki tarikh/date", "aaj kya date hai", "aaj kaun sa din hai"
    re.compile(
        r"\baaj\s+(?:ki|kya|kaun\s*si|konsi)\s+(?:tareekh|tarikh|date|din)\b"
        r"|\baaj\s+kaun\s*sa\s+din\b"
        r"|\babhi\s+(?:kya|kitna)\s+(?:time|samay|baje)\b"
        r"|\bkitne\s+baje\s+(?:hai|hain|he)\b",
        re.I,
    ),
    # Devanagari: "आज की तारीख", "आज कौन सा दिन है", "अभी क्या समय है"
    re.compile(
        r"आज\s+(?:की|क्या)\s+(?:तारीख़?|डेट|दिनांक)"
        r"|आज\s+कौन\s*सा\s+(?:दिन|वार)"
        r"|अभी\s+(?:क्या|कितना)\s+(?:समय|टाइम|बजे)"
        r"|कितने\s+बजे\s+(?:है|हैं)",
    ),
)


def asks_current_datetime(text: str) -> bool:
    """Whether the utterance asks for the CURRENT date, day or time."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return any(p.search(stripped) for p in _CURRENT_DATETIME_PATTERNS)
