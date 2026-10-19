"""Future-date/time validation for ask nodes (node config ``validation``).

A workflow that captures an appointment day and time as SPOKEN TEXT (regex
captures such as "कल", "10 October", "शाम 5 बजे") has no idea whether that
moment is still ahead. This module turns those texts into a calendar check
against the current time in a configured timezone, so a flow can refuse a
past or impossible value and ask again — without inventing anything: an
expression it cannot parse is accepted as spoken.

Node config (opt-in, nothing happens without it)::

    "validation": {
        "type": "future_datetime",
        "timezone": "Asia/Kolkata",
        "dayVariables": ["visit_day"],          # first = the value checked;
        "timeVariables": ["visit_time"],        # all cleared on rejection
        "replyByLanguage": {                    # optional overrides
            "day_past": {"hi": "...", "en": "..."}, "day_invalid": {...},
            "time_past": {...}, "time_ambiguous": {...}, "time_invalid": {...}
        }
    }

Rules (``now`` in the configured zone):

* day: today/aaj/आज, kal/कल/tomorrow (= tomorrow — the scheduling reading
  of the ambiguous Hindi word), parso/परसों (= day after tomorrow), weekday
  names (next occurrence, never today), ``DD tarikh`` (this month, else next),
  ``DD Month`` / ``Month DD`` (en/hi months; a date more than ~6 months back
  is read as next year), ``DD/MM[/YYYY]``, ``YYYY-MM-DD``. A date that does
  not exist → ``day_invalid``; one before today → ``day_past``.
* time (only checked when the day is TODAY): hour with am/pm or a period
  word (subah/सुबह, dopahar/दोपहर, sham/शाम, raat/रात, morning …), 24-hour
  values, or a period alone. Not after now → ``time_past``; a bare hour that
  is past as AM but ahead as PM → ``time_ambiguous``; impossible → ``time_invalid``.
* anything else (a future day, an unparseable phrase, no time) → ok.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from shared.orchestration.time_context import resolve_zone

DEFAULT_TIMEZONE = "Asia/Kolkata"
VALIDATION_TYPE = "future_datetime"

_DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def now_in(timezone_name: str | None) -> datetime:
    """The current time in the zone (module-level so tests can patch it)."""
    return datetime.now(resolve_zone(timezone_name or DEFAULT_TIMEZONE))


def _norm(text) -> str:
    return " ".join(str(text or "").translate(_DEV_DIGITS).lower().split())


# ── vocab ────────────────────────────────────────────────────────────────────

_RELATIVE = {
    0: ("today", "aaj", "ajj", "आज"),
    1: ("tomorrow", "kal", "कल"),
    2: ("day after tomorrow", "parso", "parson", "parsoon", "परसों"),
}
_WEEKDAYS = {
    0: ("monday", "somvar", "somwar", "सोमवार"),
    1: ("tuesday", "mangalvar", "mangalwar", "मंगलवार"),
    2: ("wednesday", "budhvar", "budhwar", "बुधवार"),
    3: ("thursday", "guruvar", "guruwar", "brihaspativar", "गुरुवार", "बृहस्पतिवार"),
    4: ("friday", "shukravar", "shukrawar", "शुक्रवार"),
    5: ("saturday", "shanivar", "shaniwar", "शनिवार"),
    6: ("sunday", "ravivar", "raviwar", "रविवार"),
}
_MONTHS = {
    1: ("january", "jan", "जनवरी"), 2: ("february", "feb", "फरवरी", "फ़रवरी"),
    3: ("march", "mar", "मार्च"), 4: ("april", "apr", "अप्रैल"), 5: ("may", "मई"),
    6: ("june", "jun", "जून"), 7: ("july", "jul", "जुलाई"), 8: ("august", "aug", "अगस्त"),
    9: ("september", "sept", "sep", "सितंबर", "सितम्बर"),
    10: ("october", "oct", "अक्टूबर", "अक्तूबर"), 11: ("november", "nov", "नवंबर", "नवम्बर"),
    12: ("december", "dec", "दिसंबर", "दिसम्बर"),
}
_PERIODS = {
    "morning": ("morning", "subah", "subha", "सुबह"),
    "afternoon": ("afternoon", "dopahar", "dopehar", "दोपहर", "noon"),
    "evening": ("evening", "sham", "shaam", "शाम"),
    "night": ("night", "raat", "रात"),
}
# Minute of day at which each period is over (for a period given alone).
_PERIOD_ENDS = {"morning": 12 * 60, "afternoon": 17 * 60, "evening": 21 * 60, "night": 24 * 60}


def _word(words: tuple[str, ...]) -> re.Pattern:
    # Latin words need \b; Devanagari has no \w boundary, so guard with a
    # "not a Devanagari letter" look-around instead.
    parts = []
    for w in sorted(words, key=len, reverse=True):
        if re.search(r"[ऀ-ॿ]", w):
            parts.append(rf"(?<![ऀ-ॿ]){re.escape(w)}(?![ऀ-ॿ])")
        else:
            parts.append(rf"\b{re.escape(w)}\b")
    return re.compile("|".join(parts), re.I)


_RELATIVE_RE = {n: _word(w) for n, w in _RELATIVE.items()}
_WEEKDAY_RE = {n: _word(w) for n, w in _WEEKDAYS.items()}
_MONTH_WORDS = {w: m for m, words in _MONTHS.items() for w in words}
_MONTH_ALT = "|".join(sorted((re.escape(w) for w in _MONTH_WORDS), key=len, reverse=True))
_DAY_MONTH_RE = re.compile(
    rf"(?<![\d])(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({_MONTH_ALT})(?![a-zऀ-ॿ])"
    rf"(?:,?\s*(\d{{4}}))?", re.I)
_MONTH_DAY_RE = re.compile(
    rf"(?<![a-zऀ-ॿ])({_MONTH_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s*(\d{{4}}))?", re.I)
_TARIKH_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:tarikh|taarikh|tareekh|tarik|तारीख)", re.I)
_NUMERIC_RE = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?(?!\d)")
_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_PERIOD_RE = {name: _word(words) for name, words in _PERIODS.items()}
_HOUR_RE = re.compile(
    r"(?<![\d:])(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?\s?m\b|p\.?\s?m\b|baje|bje|बजे|o'?\s?clock|hrs|hours)?",
    re.I)


# ── day ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DayResult:
    kind: str                 # ok | past | invalid | unknown
    value: date | None = None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_day(text, today: date) -> DayResult:
    """Resolve a spoken day against ``today``; unknown phrasing is 'unknown'."""
    t = _norm(text)
    if not t:
        return DayResult("unknown")
    # Longest phrase first: "day after tomorrow" contains "tomorrow".
    for offset in sorted(_RELATIVE_RE, reverse=True):
        if _RELATIVE_RE[offset].search(t):
            return DayResult("ok", today + timedelta(days=offset))
    m = _ISO_RE.search(t)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return _judge(d, today)
    m = _NUMERIC_RE.search(t)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if month > 12:
            return DayResult("invalid")
        if year:
            y = int(year)
            y = y + 2000 if y < 100 else y
            return _judge(_safe_date(y, month, day), today)
        return _judge_yearless(month, day, today)
    m = _DAY_MONTH_RE.search(t) or None
    month = day = year = None
    if m:
        day, month, year = int(m.group(1)), _MONTH_WORDS[m.group(2).lower()], m.group(3)
    else:
        m = _MONTH_DAY_RE.search(t)
        if m:
            month, day, year = _MONTH_WORDS[m.group(1).lower()], int(m.group(2)), m.group(3)
    if month is not None:
        if year:
            return _judge(_safe_date(int(year), month, day), today)
        return _judge_yearless(month, day, today)
    m = _TARIKH_RE.search(t)
    if m:
        day = int(m.group(1))
        year, month = today.year, today.month
        if day < today.day:  # "10 tarikh" on the 19th → next month's 10th
            month = month % 12 + 1
            year += month == 1
        if day > calendar.monthrange(year, month)[1]:
            return DayResult("invalid")
        return DayResult("ok", date(year, month, day))
    for weekday, pattern in _WEEKDAY_RE.items():
        if pattern.search(t):
            delta = (weekday - today.weekday()) % 7 or 7
            return DayResult("ok", today + timedelta(days=delta))
    return DayResult("unknown")


def _judge(d: date | None, today: date) -> DayResult:
    if d is None:
        return DayResult("invalid")
    return DayResult("past" if d < today else "ok", d)


def _judge_yearless(month: int, day: int, today: date) -> DayResult:
    d = _safe_date(today.year, month, day)
    if d is None:
        # 29 Feb on a non-leap year is still invalid next year in most cases;
        # 31 Feb / 32 Oct never exist.
        return DayResult("invalid")
    if d < today and (today - d).days > 180:
        nxt = _safe_date(today.year + 1, month, day)
        return DayResult("ok", nxt) if nxt else DayResult("invalid")
    return _judge(d, today)


# ── time ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TimeResult:
    kind: str                       # exact | ambiguous | period | invalid | unknown
    candidates: tuple[int, ...] = ()  # minutes from midnight (1 for exact, 2 for ambiguous)
    period: str | None = None


def parse_time(text) -> TimeResult:
    t = _norm(text)
    if not t:
        return TimeResult("unknown")
    period = next((name for name, p in _PERIOD_RE.items() if p.search(t)), None)
    m = _HOUR_RE.search(t)
    if m is None or not m.group(1):
        if re.search(r"\bnoon\b", t):
            return TimeResult("exact", (12 * 60,), "afternoon")
        return TimeResult("period", period=period) if period else TimeResult("unknown")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    suffix = (m.group(3) or "").replace(".", "").replace(" ", "").lower()
    if hour > 23 or minute > 59:
        return TimeResult("invalid")
    if suffix in ("am", "pm"):
        hour = hour % 12 + (12 if suffix == "pm" else 0)
        return TimeResult("exact", (hour * 60 + minute,), period)
    if hour >= 13 or hour == 0:
        return TimeResult("exact", (hour * 60 + minute,), period)
    if period == "morning":
        return TimeResult("exact", (hour * 60 + minute,), period)
    if period == "afternoon":
        return TimeResult("exact", ((hour if hour == 12 else hour + 12) * 60 + minute,), period)
    if period == "evening":
        return TimeResult("exact", ((hour + 12 if hour < 12 else hour) * 60 + minute,), period)
    if period == "night":
        return TimeResult("exact", ((hour + 12 if 6 <= hour < 12 else hour) * 60 + minute,), period)
    if hour == 12:
        return TimeResult("exact", (12 * 60 + minute,))
    return TimeResult("ambiguous", (hour * 60 + minute, (hour + 12) * 60 + minute))


# ── verdict ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None   # day_past | day_invalid | time_past | time_ambiguous | time_invalid
    clear: tuple[str, ...] = field(default_factory=tuple)


def validate_visit(day_text, time_text, now: datetime, *,
                   accept_unparsed_day: bool = False) -> tuple[bool, str | None]:
    """(ok, reason) for a spoken day/time against ``now`` (zone-aware).

    A day the parser cannot read ("next week", "agle hafte") is
    ``day_unclear`` — the flow asks for a specific day rather than confirming
    an unverified one — unless ``accept_unparsed_day`` opts into the old
    accept-as-spoken behaviour. An EMPTY day is simply "not given yet".
    """
    today = now.date()
    day = parse_day(day_text, today)
    if day.kind == "invalid":
        return False, "day_invalid"
    if day.kind == "past":
        return False, "day_past"
    if day.kind == "unknown" and _norm(day_text) and not accept_unparsed_day:
        return False, "day_unclear"
    if day.kind != "ok" or day.value != today or not _norm(time_text):
        return True, None
    now_minutes = now.hour * 60 + now.minute
    t = parse_time(time_text)
    if t.kind == "invalid":
        return False, "time_invalid"
    if t.kind == "exact":
        return (True, None) if t.candidates[0] > now_minutes else (False, "time_past")
    if t.kind == "ambiguous":
        ahead = [c for c in t.candidates if c > now_minutes]
        if not ahead:
            return False, "time_past"
        return (True, None) if len(ahead) == len(t.candidates) else (False, "time_ambiguous")
    if t.kind == "period":
        return (True, None) if _PERIOD_ENDS[t.period] > now_minutes else (False, "time_past")
    return True, None


def _first_filled(slots: dict, names: list) -> str:
    for name in names:
        value = slots.get(str(name))
        if value is not None and str(value).strip():
            return str(value)
    return ""


def validate_ask(config: dict, slots: dict, now: datetime | None = None) -> Verdict:
    """Apply a node's ``validation`` config to the slots as they stand."""
    if not isinstance(config, dict) or config.get("type") != VALIDATION_TYPE:
        return Verdict(True)
    day_vars = [str(v) for v in (config.get("dayVariables") or []) if str(v).strip()]
    time_vars = [str(v) for v in (config.get("timeVariables") or []) if str(v).strip()]
    current = now or now_in(config.get("timezone") or DEFAULT_TIMEZONE)
    ok, reason = validate_visit(
        _first_filled(slots, day_vars), _first_filled(slots, time_vars), current,
        accept_unparsed_day=config.get("acceptUnparsedDay") is True,
    )
    if ok:
        return Verdict(True)
    clear = tuple(day_vars if reason.startswith("day_") else time_vars)
    return Verdict(False, reason, clear)


# ``day_missing`` is not a rejection: the turn carried a time but no day (the
# ask's own slot is still empty), so the flow keeps the time and asks for the
# day instead of handing the turn to the model.
_DEFAULT_REPLIES = {
    "day_unclear": {
        "en": "I did not quite catch the date. Please tell me the day, for example tomorrow, the day after, or a specific date.",
        "hi": "मुझे date ठीक से समझ नहीं आई। आप दिन बताइए, जैसे कल, परसों, या कोई तारीख।",
    },
    "day_missing": {
        "en": "Noted the time. And on which day would you like the visit, for example tomorrow or the day after?",
        "hi": "समय note कर लिया। और visit किस दिन करवाना चाहेंगे, जैसे कल या परसों?",
    },
    "day_past": {
        "en": "That date has already passed. Please tell me an upcoming date, for example tomorrow or the day after.",
        "hi": "ये date निकल चुकी है। आप आने वाली कोई date बताइए, जैसे कल या परसों।",
    },
    "day_invalid": {
        "en": "That does not seem to be a valid date. Please tell me the date again, for example tomorrow or the day after.",
        "hi": "ये date सही नहीं लग रही। आप date फिर से बताइए, जैसे कल या परसों।",
    },
    "time_past": {
        "en": "That time today has already passed. Please tell me a later time today, or another day.",
        "hi": "आज का वो समय निकल चुका है। आप आज का कोई बाद का समय बताइए, या कोई और दिन।",
    },
    "time_ambiguous": {
        "en": "For today, do you mean the morning or the evening? Please tell me the time, for example 4 PM.",
        "hi": "आज सुबह या शाम? आप समय बताइए, जैसे शाम 4 बजे।",
    },
    "time_invalid": {
        "en": "I could not understand that time. Please tell me the time again, for example 4 PM.",
        "hi": "ये समय समझ नहीं आया। आप समय फिर से बताइए, जैसे शाम 4 बजे।",
    },
}


def rejection_reply(config: dict, reason: str, lang: str) -> str:
    """The configured reply for a rejection reason (``replyByLanguage``),
    else the built-in bilingual default. English for en-*, Hindi otherwise."""
    locale = (lang or "").split("-")[0].lower() or "hi"
    configured = ((config or {}).get("replyByLanguage") or {}).get(reason) or {}
    if isinstance(configured, dict):
        text = configured.get(locale) or configured.get("hi") or configured.get("en")
        if text:
            return str(text)
    elif isinstance(configured, str) and configured.strip():
        return configured
    defaults = _DEFAULT_REPLIES.get(reason) or {}
    return defaults.get(locale) or defaults.get("hi") or ""


__all__ = [
    "DEFAULT_TIMEZONE", "VALIDATION_TYPE", "DayResult", "TimeResult", "Verdict",
    "now_in", "parse_day", "parse_time", "validate_visit", "validate_ask", "rejection_reply",
]
