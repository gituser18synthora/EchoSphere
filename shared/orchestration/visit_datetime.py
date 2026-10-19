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
        },
        "normalizedDayVariable": "visit_date_iso",    # optional: where the
        "normalizedTimeVariable": "visit_time_24h"    # resolved values go
    }

Normalized companions (opt-in): every accepted answer also writes the day
the parser resolved — against the clock at THAT utterance, in the configured
zone, so "kal" said after midnight is the right calendar day — as
``YYYY-MM-DD``, and the time as ``HH:MM`` (24-hour) for an exact time or the
period name (``morning`` | ``afternoon`` | ``evening`` | ``night``) for a
window, '' when no time was given. The spoken slots are untouched (readbacks
keep the caller's words); a rejection clears both companions.

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
# Devanagari letters and marks — the danda (। ॥) is punctuation, so a word
# before it ("परसों।", "बजे।", as Sarvam ends sentences) still ends there.
_DEV = "\u0900-\u0963\u0966-\u097f"


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
_PERIOD_ENDS = {"morning": 12 * 60, "afternoon": 17 * 60, "evening": 21 * 60, "night": 24 * 60,
                "a": 12 * 60, "p": 24 * 60}  # a/p = a bare "AM"/"PM" answer


def _word(words: tuple[str, ...]) -> re.Pattern:
    # Latin words need \b; Devanagari has no \w boundary, so guard with a
    # "not a Devanagari letter" look-around instead.
    parts = []
    for w in sorted(words, key=len, reverse=True):
        if re.search(r"[ऀ-ॿ]", w):
            parts.append(rf"(?<![{_DEV}]){re.escape(w)}(?![{_DEV}])")
        else:
            parts.append(rf"\b{re.escape(w)}\b")
    return re.compile("|".join(parts), re.I)


_RELATIVE_RE = {n: _word(w) for n, w in _RELATIVE.items()}
_WEEKDAY_RE = {n: _word(w) for n, w in _WEEKDAYS.items()}
_MONTH_WORDS = {w: m for m, words in _MONTHS.items() for w in words}
_MONTH_ALT = "|".join(sorted((re.escape(w) for w in _MONTH_WORDS), key=len, reverse=True))
_DAY_MONTH_RE = re.compile(
    rf"(?<![\d])(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({_MONTH_ALT})(?![a-z{_DEV}])"
    rf"(?:,?\s*(\d{{4}}))?", re.I)
_MONTH_DAY_RE = re.compile(
    rf"(?<![a-z{_DEV}])({_MONTH_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s*(\d{{4}}))?", re.I)
DAY_WORDS_ROMAN = {
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
    "chhe": 6, "che": 6, "saat": 7, "sat": 7, "aath": 8, "ath": 8, "nau": 9, "das": 10, "dus": 10,
    "gyarah": 11, "gyara": 11, "barah": 12, "bara": 12, "terah": 13, "tera": 13, "chaudah": 14,
    "chauda": 14, "pandrah": 15, "pandra": 15, "solah": 16, "sola": 16, "satrah": 17, "satra": 17,
    "atharah": 18, "athara": 18, "unnis": 19, "unees": 19, "bees": 20, "bis": 20, "ikkis": 21,
    "ikkees": 21, "bais": 22, "baees": 22, "teis": 23, "tees": 30, "chaubis": 24, "chobis": 24,
    "pachees": 25, "pachchis": 25, "pachis": 25, "chhabbis": 26, "chabbis": 26, "sattais": 27,
    "satais": 27, "atthais": 28, "athais": 28, "untis": 29, "untees": 29, "ikattis": 31, "ektis": 31,
}
_DAY_WORD_ALT = "|".join(sorted((re.escape(w) for w in DAY_WORDS_ROMAN), key=len, reverse=True))
_TARIKH_RE = re.compile(
    rf"(?<![\d])(\d{{1,2}}|\b(?:{_DAY_WORD_ALT})\b)\s*(?:tarikh|taarikh|tareekh|tarik|तारीख)", re.I)
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
        raw = m.group(1)
        day = int(raw) if raw.isdigit() else DAY_WORDS_ROMAN[raw.lower()]
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


# Spoken hour words — Sarvam writes Hindi numbers as WORDS ("कल सुबह नौ बजे",
# "कल सुबह दस बजे" on live calls), so a time capture must read them. These
# tables are the single vocabulary: the Jeeves setup script builds the capture
# regexes from them, and the parser below reads what those regexes capture.
HOUR_WORDS_HI = {
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पाँच": 5, "पांच": 5, "पाच": 5,
    "छह": 6, "छः": 6, "छे": 6, "सात": 7, "आठ": 8, "नौ": 9, "दस": 10,
    "ग्यारह": 11, "बारह": 12,
}
HOUR_WORDS_ROMAN = {
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4,
    "paanch": 5, "panch": 5, "pach": 5, "chhe": 6, "che": 6, "cheh": 6, "chah": 6,
    "saat": 7, "sat": 7, "aath": 8, "ath": 8, "nau": 9, "das": 10, "dus": 10,
    "gyarah": 11, "gyara": 11, "barah": 12, "bara": 12,
}
HOUR_WORDS_EN = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
HOUR_WORDS = {**HOUR_WORDS_HI, **HOUR_WORDS_ROMAN, **HOUR_WORDS_EN}
# Fractions spoken BEFORE the hour: value = (hour offset, minutes).
FRACTION_WORDS = {
    "साढ़े": (0, 30), "साढे": (0, 30), "साडे": (0, 30), "sadhe": (0, 30), "saadhe": (0, 30),
    "sade": (0, 30), "saade": (0, 30), "half past": (0, 30),
    "सवा": (0, 15), "sawa": (0, 15), "sava": (0, 15), "quarter past": (0, 15),
    "पौने": (-1, 45), "paune": (-1, 45), "pone": (-1, 45), "quarter to": (-1, 45),
}
# Whole spoken times: value = (hour, minutes), ambiguous AM/PM like a bare hour.
SPECIAL_TIMES = {
    "ढाई": (2, 30), "dhai": (2, 30), "dhaai": (2, 30),
    "डेढ़": (1, 30), "डेढ": (1, 30), "dedh": (1, 30), "derh": (1, 30),
}
# Minutes spoken AFTER the hour: English words after an hour word ("four
# twenty"), Hindi / romanised words after बजकर / bajkar ("चार बजकर बीस मिनट").
# Single-digit minutes are not read from bare words ("four five" is a
# conflict, never 4:05); after बजकर any number word counts.
MINUTE_WORDS_EN = {
    "ten": 10, "fifteen": 15, "twenty": 20, "twenty five": 25, "twenty-five": 25,
    "thirty": 30, "thirty five": 35, "thirty-five": 35, "forty": 40,
    "forty five": 45, "forty-five": 45, "fifty": 50, "fifty five": 55, "fifty-five": 55,
}
MINUTE_WORDS_HI = {
    "दस": 10, "पंद्रह": 15, "पन्द्रह": 15, "बीस": 20, "पच्चीस": 25, "तीस": 30, "पैंतीस": 35,
    "चालीस": 40, "पैंतालीस": 45, "पचास": 50, "पचपन": 55,
    "das": 10, "dus": 10, "pandrah": 15, "pandra": 15, "bees": 20, "bis": 20, "pachees": 25,
    "pachis": 25, "tees": 30, "tis": 30, "paintees": 35, "paintis": 35, "chalis": 40,
    "chaalis": 40, "paintalis": 45, "paintaalis": 45, "pachas": 50, "pachpan": 55,
}
_MINUTE_MARKERS = frozenset({"बजकर", "बजके", "bajkar", "bajke", "bajakar"})   # minutes follow
_HOUR_MARKERS = frozenset({"बजे", "baje", "bje"})                      # minutes follow only with a unit
_MINUTE_UNITS = frozenset({"मिनट", "मिनिट", "minute", "minutes", "min", "mint"})
PERIOD_WORDS = {name: words for name, words in _PERIODS.items()}
_AMPM_RE = re.compile(r"(?<![a-z])([ap])\.?\s?m\.?(?![a-z])", re.I)
_TOKEN_RE = re.compile(rf"\d{{1,2}}(?:[:.]\d{{2}})?|[a-z]+(?:'[a-z]+)?|[{_DEV}]+")


def _phrase_hit(phrases: dict, t: str) -> str | None:
    """Longest phrase from ``phrases`` present in the text (word-bounded)."""
    for phrase in sorted(phrases, key=len, reverse=True):
        if _word((phrase,)).search(t):
            return phrase
    return None


def _minute_number(tok: str) -> int | None:
    """A minute count named by one token after बजकर / बजे (digits or words)."""
    if tok.isdigit():
        value = int(tok)
        return value if value <= 59 else None
    for table in (MINUTE_WORDS_HI, HOUR_WORDS, DAY_WORDS_ROMAN, MINUTE_WORDS_EN):
        if tok in table:
            return table[tok]
    return None


_MINUTE_PHRASES_EN = sorted(((re.split(r"[\s-]+", words), value) for words, value in MINUTE_WORDS_EN.items()),
                            key=lambda item: -len(item[0]))


def _clock_readings(tokens: list[str]) -> list[tuple[int, int]]:
    """Every (hour, minute) a token list names, in order: "4", "4:30",
    "four twenty", "4 30 बजे", "4 बजकर 30 मिनट", "4 baje 30 minute". Two
    different readings mean the text is not one time."""
    readings: list[tuple[int, int]] = []
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok[0].isdigit():
            h, _, m = tok.partition(":") if ":" in tok else tok.partition(".")
            hour, minute, explicit = int(h), int(m or 0), bool(m)
        elif tok in HOUR_WORDS:
            hour, minute, explicit = HOUR_WORDS[tok], 0, False
        else:
            i += 1
            continue
        j = i + 1
        if not explicit:
            if not tok[0].isdigit():                      # "four twenty"
                for phrase, value in _MINUTE_PHRASES_EN:
                    if tokens[j:j + len(phrase)] == phrase:
                        minute, j = value, j + len(phrase)
                        break
            elif j < n and tokens[j].isdigit() and 10 <= int(tokens[j]) <= 59:   # "4 30 बजे"
                minute, j = int(tokens[j]), j + 1
            if minute == 0 and j < n and (tokens[j] in _MINUTE_MARKERS or tokens[j] in _HOUR_MARKERS):
                k = j + 1
                value = _minute_number(tokens[k]) if k < n else None
                if value is not None:
                    unit = k + 1 < n and tokens[k + 1] in _MINUTE_UNITS
                    if tokens[j] in _MINUTE_MARKERS or unit:   # बजकर 30 | बजे 30 मिनट
                        minute, j = value, k + 1 + (1 if unit else 0)
        readings.append((hour, minute))
        i = j
    return readings


def parse_time(text) -> TimeResult:
    """Read a spoken time: digits or hour words, fraction words, periods, am/pm.

    Minutes are kept whole: "4:30", "four twenty", "4 30 बजे", "4 बजकर 30
    मिनट", "4 baje 30 minute", साढ़े/सवा/पौने and half/quarter past. A text
    naming two different readings ("5 बजे या 6 बजे", "4 5 बजे", "4 बजे 30")
    or two different periods ("सुबह या शाम"), or "रात 12" (midnight,
    ambiguous about the date), is ``unknown``: said, not understood — the
    flow asks again rather than keeping either reading or dropping a part.
    """
    t = _norm(text)
    if not t:
        return TimeResult("unknown")
    periods = [name for name, p in _PERIOD_RE.items() if p.search(t)]
    if len(periods) > 1:
        return TimeResult("unknown")
    period = periods[0] if periods else None
    readings = _clock_readings(_TOKEN_RE.findall(t))
    if len(set(readings)) > 1:
        return TimeResult("unknown")
    ampm = _AMPM_RE.search(t)
    ampm = ampm.group(1).lower() if ampm else None
    hour = minute = None
    special = _phrase_hit(SPECIAL_TIMES, t)
    if special:
        if readings:
            return TimeResult("unknown")   # "ढाई बजे या तीन बजे"
        hour, minute = SPECIAL_TIMES[special]
    elif readings:
        hour, minute = readings[0]
        fraction = _phrase_hit(FRACTION_WORDS, t)
        if fraction and minute == 0:
            offset, minute = FRACTION_WORDS[fraction]
            hour = hour + offset
            if hour == 0:
                hour = 12
    if hour is None:
        if re.search(r"\bnoon\b", t):
            return TimeResult("exact", (12 * 60,), "afternoon")
        if period:
            return TimeResult("period", period=period)
        if ampm:
            return TimeResult("period", period=ampm)
        return TimeResult("unknown")
    if hour > 23 or minute > 59:
        return TimeResult("invalid")
    if ampm:
        hour = hour % 12 + (12 if ampm == "p" else 0)
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
        if hour == 12:
            return TimeResult("unknown")   # midnight: which night? ask
        return TimeResult("exact", ((hour + 12 if 6 <= hour < 12 else hour) * 60 + minute,), period)
    if hour == 12:
        return TimeResult("exact", (12 * 60 + minute,))
    return TimeResult("ambiguous", (hour * 60 + minute, (hour + 12) * 60 + minute))


def compose_time(answer_text, pending_text) -> str | None:
    """The clarification answer joined with the hour it clarifies.

    After "4 baje" was refused as ambiguous, "शाम" / "evening" / "PM" alone is
    the missing half: the composed value keeps the caller's own words
    ("शाम 4 baje", "4 baje PM"). None when the answer is not a bare period.
    """
    answer = " ".join(str(answer_text or "").split())
    pending = " ".join(str(pending_text or "").split())
    if not answer or not pending:
        return None
    parsed = parse_time(answer)
    if parsed.kind != "period":
        return None
    if parsed.period in ("a", "p"):
        return f"{pending} {parsed.period.upper()}M"
    english = re.search(r"\b(morning|afternoon|evening|night)\b", answer, re.I)
    if english:
        return f"{pending} in the {english.group(1).lower()}"
    return f"{answer} {pending}"


# ── capture: candidate spans chosen by the parser ────────────────────────────
#
# The capture regexes are built from the vocabulary above, so the words the
# parser reads are exactly the words the capture finds. They only FIND
# candidate spans; choosing among them is the parser's job (extract_time /
# extract_day), never the regex engine's leftmost rule: on live call
# cv_0b1e4d99ac69 "कल कर दो शाम के पाँच बजे" the leftmost match was "दो शाम" —
# the verb particle of "कर दो" plus the period word — read as 2 PM, while the
# complete "शाम के पाँच बजे" started later in the sentence.

def _guarded(words) -> str:
    """Alternation of whole words; Devanagari words get letter look-arounds
    (no \b there), multi-word phrases accept any whitespace."""
    parts = []
    for w in sorted(words, key=len, reverse=True):
        esc = re.escape(w).replace(r"\ ", r"\s+")
        if re.search(r"[\u0900-\u097f]", w):
            parts.append(rf"(?<![{_DEV}]){esc}(?![{_DEV}])")
        else:
            parts.append(rf"\b{esc}\b")
    return "(?:" + "|".join(parts) + ")"


PERIOD_SURFACE = [w for words in PERIOD_WORDS.values() for w in words
                  if w != "noon"] + ["सवेरे", "savere", "subha"]


def time_capture_regex() -> str:
    """Regex matching one spoken time expression (digits or hour words,
    fractions, periods before/after with को/के/ko/ke, बजे/baje/am/pm, "at
    four", "four thirty", 16:30, a bare period or AM/PM). Every alternative
    is anchored so a number inside a date ("25 October") is never a time."""
    period = _guarded(PERIOD_SURFACE)
    ko = r"(?:\s+(?:ko|को|mein|में|me))?"
    hour_digit = r"(?<![\d:.])\d{1,2}(?:[:.]\d{2}|\s[1-5]\d)?(?![\d])"
    hour = f"(?:{hour_digit}|{_guarded(HOUR_WORDS)})"
    frac = _guarded(FRACTION_WORDS)
    special = _guarded(SPECIAL_TIMES)
    min_en = rf"(?:\s+{_guarded(MINUTE_WORDS_EN)})?"
    ampm = r"(?:\s*(?<![a-z])[ap]\.?\s?m\.?(?![a-z]))"
    minute_number = rf"(?:\d{{1,2}}(?![\d])|{_guarded({**MINUTE_WORDS_HI, **HOUR_WORDS, **DAY_WORDS_ROMAN, **MINUTE_WORDS_EN})})"
    unit = rf"(?:\s*{_guarded(_MINUTE_UNITS)})"
    suffix = (rf"(?:\s*{_guarded(_MINUTE_MARKERS)}(?:\s+{minute_number}{unit}?)?"      # बजकर 30 [मिनट]
              rf"|\s*(?:{_guarded(_HOUR_MARKERS)}|o'?\s?clock|hrs)(?:\s+{minute_number}{unit}|\s+[1-5]\d(?![\d]))?)"   # बजे 30 मिनट / बजे 30 (asked again)
              rf"{ampm}?|{ampm}")
    trail = rf"(?:\s+(?:in\s+the\s+)?{period}{ko})?"
    lead = rf"{period}\s+(?:ko\s+|को\s+|ke\s+|के\s+)?"
    alternatives = [
        rf"{lead}(?:{frac}\s+)?{hour}{min_en}(?:{suffix})?{trail}",     # सुबह नौ बजे / subah 10
        rf"{frac}\s+{hour}{min_en}(?:{suffix})?{trail}",                # साढ़े चार बजे / half past four
        rf"(?:{lead})?{special}(?:{suffix})?{trail}",                    # ढाई बजे / दोपहर डेढ़ बजे
        rf"{hour}{min_en}(?:{suffix}){trail}",                           # 4 PM / nau baje subah
        rf"{hour}{min_en}\s+(?:in\s+the\s+)?{period}{ko}",            # 10 in the morning / nau sham ko
        rf"(?<=\bat ){hour}{min_en}(?:{suffix})?{trail}",                 # (at) four
        rf"{_guarded(HOUR_WORDS_EN)}\s+(?:thirty|fifteen|forty[\s-]?five)\b",  # four thirty
        r"(?<![\d:.])(?:1[3-9]|2[0-3])[:.][0-5]\d(?![\d])",            # 16:30
        rf"{period}(?!\s+(?:ko\s+|को\s+|ke\s+|के\s+)?(?:{frac}|{hour}|{special}))|(?<![a-z])[ap]\.?\s?m\.?(?![a-z])",  # bare period / PM
    ]
    return "(?i)(" + "|".join(alternatives) + ")"


_WEEK_WORDS = ("week", "hafte", "hafta", "हफ्ते", "हफ़्ते", "सप्ताह")
_NEXT_WORDS = ("this", "next", "coming", "agle", "agla", "aane wale", "is", "अगले", "आने वाले", "इस")
_TARIKH = r"(?:tarikh|taarikh|tareekh|tarik|तारीख)"


def day_capture_regex() -> str:
    """Regex matching one spoken day expression: today/kal/परसों, weekdays
    (with next/agle/अगले), "next week"/"agle hafte" (captured so validation
    can ask for a specific day), DD tarikh (digits or romanised day words),
    DD Month / Month DD (en/hi months, optional year), DD/MM[/YYYY], ISO."""
    months = _guarded(list(_MONTH_WORDS))
    weekdays = _guarded([w for ws in _WEEKDAYS.values() for w in ws])
    relative = _guarded([w for ws in _RELATIVE.values() for w in ws])
    alternatives = [
        rf"(?<!\d)\d{{1,2}}(?:st|nd|rd|th)?\s*(?:of\s+)?{months}(?:,?\s*\d{{4}})?",
        rf"{months}\s+\d{{1,2}}(?:st|nd|rd|th)?(?!\d)(?:,?\s*\d{{4}})?",
        r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)",
        r"(?<!\d)\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?(?!\d)",
        rf"(?<!\d)(?:\d{{1,2}}|{_guarded(list(DAY_WORDS_ROMAN))})\s*{_TARIKH}",
        rf"(?:{_guarded(_NEXT_WORDS)}\s+)?(?:{weekdays}|{_guarded(_WEEK_WORDS)})",
        relative,
    ]
    return "(?i)(" + "|".join(alternatives) + ")"


_TIME_CAPTURE_RE = re.compile(time_capture_regex())
_DAY_CAPTURE_RE = re.compile(day_capture_regex())
_TIME_MARKER_RE = re.compile(r"बजे|बजकर|बजके|\bbaje\b|\bbje\b|\bbajka?r\b|\bbajke\b|o'?\s?clock|\bhrs\b|(?<![a-z])[ap]\.?\s?m\.?(?![a-z])", re.I)
# "X नहीं" / "X nahi" / "X not" right after a span (an optional तो/ही/भी/to/hi
# between) negates it; English "not X" / "no X" before it does too. A Hindi
# नहीं BEFORE a span is the answer "no" followed by the correction, not a
# negation of it ("नहीं, कल कर दो"), and a bare English "no" AFTER one is
# usually "no problem" — neither counts.
_NEG_AFTER_RE = re.compile(
    rf"^\s*,?\s*(?:(?:to|hi|bhi|ko|me|mein|par|तो|ही|भी|को|में|पर)\s+)?(?:nahi|nahin|nahee|nhi|mat|not|नहीं|नही|मत)(?![a-z{_DEV}])", re.I)
_NEG_BEFORE_RE = re.compile(r"(?:^|[^a-z])(?:not|no)\s+(?:(?:on|at|for)\s+)?$", re.I)
_NO_AFTER_RE = re.compile(r"\s*,?\s*(?:no|nope)\b", re.I)
_TOKEN_START_RE = re.compile(r"(?:^|(?<=[\s,.;:!?।(\-]))\S")


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    text: str
    parsed: object      # TimeResult | DayResult

    def overlaps(self, other: "_Span") -> bool:
        return self.start < other.end and other.start < self.end


def _candidate_spans(text: str, pattern: re.Pattern) -> list[tuple[int, int, str]]:
    """Every match starting at a token boundary (overlapping spans allowed)."""
    out, seen = [], set()
    for m in _TOKEN_START_RE.finditer(text):
        hit = pattern.match(text, m.start())
        if hit and hit.group(0).strip():
            span = (hit.start(), hit.end(), hit.group(0).strip())
            if span not in seen:
                seen.add(span)
                out.append(span)
    return out


def _negated(text: str, start: int, end: int) -> bool:
    return bool(_NEG_AFTER_RE.search(text[end:]) or _NEG_BEFORE_RE.search(text[:start]))


def _hour_word_unanchored(span: str) -> bool:
    """A Hindi / romanised-Hindi hour WORD that is neither followed by a
    time marker nor preceded by a period or fraction word — "दो" in "कर दो
    शाम", "ek" in "ek baar" — is a verb particle or a determiner, not an hour.
    Digits and English hour words ("four in the evening") stand on their own."""
    tokens = _norm(span).split()
    if not tokens:
        return True
    first = tokens[0]
    hindi_hour = first in HOUR_WORDS_HI or first in HOUR_WORDS_ROMAN
    return hindi_hour and not _TIME_MARKER_RE.search(span)


def extract_time(text) -> str | None:
    """The one spoken time expression in ``text`` the parser stands behind,
    as the caller said it — or None.

    All candidate spans are read (not just the leftmost), negated spans are
    dropped, and the most complete reading wins (exact > bare hour > period;
    with a बजे/baje/AM/PM marker; longest). A bare hour in one place and a
    bare period in another ("5 बजे … शाम को") are composed into one exact
    time. Two readings that disagree ("5 बजे या 6 बजे", "सुबह या शाम") are
    returned together, as said, which :func:`parse_time` cannot read — so
    validation asks for the time again instead of either reading being kept.
    """
    raw = str(text or "")
    spans: list[_Span] = []
    for start, end, span in _candidate_spans(raw, _TIME_CAPTURE_RE):
        if _hour_word_unanchored(span) or _negated(raw, start, end):
            continue
        # A span the parser refuses ("रात 12 बजे") stays a candidate: as the
        # fullest reading of its mention it must not degrade into a shorter
        # one it contains ("12 बजे" = noon); validation will ask again.
        spans.append(_Span(start, end, span, parse_time(span)))
    spans = _drop_corrected(raw, spans)
    if not spans:
        return None
    rank = {"exact": 3, "ambiguous": 2, "period": 1, "unknown": 0, "invalid": 0}

    def score(s: _Span):
        return (rank[s.parsed.kind], bool(_TIME_MARKER_RE.search(s.text)),
                s.parsed.period is not None, len(s.text))

    mentions = _mentions(spans, lambda s: (len(s.text), score(s)))   # fullest reading per mention
    best, *others = sorted(mentions, key=score, reverse=True)
    if best.parsed.kind in ("unknown", "invalid"):
        return best.text
    if best.parsed.kind == "ambiguous":
        period = next((s for s in others if s.parsed.kind == "period"), None)
        if period is not None:
            composed = compose_time(period.text, best.text)
            if composed and parse_time(composed).kind == "exact":
                rest = [s for s in others if s is not period and not _agrees(s, period)]
                if rest:
                    return _together(raw, [best, period, *rest])
                return composed
    disagreeing = [s for s in others if not _agrees(s, best)]
    if disagreeing:
        return _together(raw, [best, *disagreeing])
    return best.text


def _agrees(s: _Span, best: _Span) -> bool:
    """Whether ``s`` is the same time as ``best`` said less completely:
    the same window ("शाम 5 बजे … शाम को"), the same bare hour ("5 बजे …
    शाम 5 बजे"), or the same reading."""
    a, b = s.parsed, best.parsed
    if a.kind == "period":
        return b.period == a.period
    if a.kind == "ambiguous" and b.kind == "exact":
        return b.candidates[0] in a.candidates
    return _time_value(s) == _time_value(best)


def _together(raw: str, spans: list[_Span]) -> str:
    """The caller's words from the first to the last disagreeing mention."""
    return raw[min(s.start for s in spans):max(s.end for s in spans)].strip()


def _drop_corrected(raw: str, spans: list[_Span]) -> list[_Span]:
    """A bare English "no" right after a span is a correction only when
    another span follows ("Tomorrow, no, make it Monday" → Monday); alone it
    is "no problem" and keeps the span."""
    kept = []
    for s in spans:
        if _NO_AFTER_RE.match(raw[s.end:]) and any(t.start >= s.end for t in spans if t is not s):
            continue
        kept.append(s)
    return kept


def _mentions(spans: list[_Span], score) -> list[_Span]:
    """Overlapping spans are readings of ONE mention ("शाम 5 बजे" also
    contains "5 बजे"): keep the best-scoring reading of each mention, best
    mention first."""
    kept: list[_Span] = []
    for s in sorted(spans, key=score, reverse=True):
        if not any(s.overlaps(k) for k in kept):
            kept.append(s)
    return kept


def _time_value(s: _Span):
    p = s.parsed
    return (p.kind, p.candidates, p.period if p.kind == "period" else None)


def extract_day(text, today: date | None = None) -> str | None:
    """The one spoken day expression in ``text``, as said — or None.

    Every candidate span is read against ``today`` (default: now in the
    default zone); negated spans are dropped ("कल नहीं परसों" → परसों, "not
    tomorrow, Monday" → Monday); a resolvable day beats one the parser cannot
    place ("next week"), which is still returned alone so validation can ask
    for a specific day. Two different resolvable days ("कल या परसों") give
    None, so the flow asks instead of picking one.
    """
    raw = str(text or "")
    today = today or now_in(DEFAULT_TIMEZONE).date()
    spans: list[_Span] = []
    for start, end, span in _candidate_spans(raw, _DAY_CAPTURE_RE):
        if _negated(raw, start, end):
            continue
        spans.append(_Span(start, end, span, parse_day(span, today)))
    spans = _drop_corrected(raw, spans)
    if not spans:
        return None
    rank = {"ok": 3, "past": 2, "invalid": 2, "unknown": 1}
    best, *others = _mentions(spans, lambda s: (rank[s.parsed.kind], len(s.text)))
    for s in others:
        if s.parsed.kind == "unknown":
            continue   # "next week … Monday": the vague one is refined, not contradicted
        if (s.parsed.kind, s.parsed.value) != (best.parsed.kind, best.parsed.value):
            return None
    return best.text


# ── verdict ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None   # day_past | day_invalid | day_unclear | time_past | time_ambiguous | time_unclear | time_invalid
    clear: tuple[str, ...] = field(default_factory=tuple)


def normalize_time(t: TimeResult) -> str:
    """``HH:MM`` (24-hour) for an exact time, the period name for a window
    (``morning`` | ``afternoon`` | ``evening`` | ``night``), '' otherwise —
    a window is never turned into a clock time."""
    if t.kind == "exact" and t.candidates:
        return "%02d:%02d" % divmod(t.candidates[0] % (24 * 60), 60)
    if t.kind == "period" and t.period in PERIOD_WORDS:
        return t.period
    return ""


@dataclass(frozen=True)
class VisitResolution:
    """What the parser made of a spoken day/time pair against ``now``."""
    ok: bool
    reason: str | None = None
    day: DayResult = field(default_factory=lambda: DayResult("unknown"))
    time: TimeResult = field(default_factory=lambda: TimeResult("unknown"))

    @property
    def iso_date(self) -> str:
        """``YYYY-MM-DD`` of the resolved day ('' when none was resolved)."""
        return self.day.value.isoformat() if self.day.kind == "ok" and self.day.value else ""

    @property
    def normalized_time(self) -> str:
        return normalize_time(self.time)


def resolve_visit(day_text, time_text, now: datetime, *,
                  accept_unparsed_day: bool = False) -> VisitResolution:
    """Parse and judge a spoken day/time against ``now`` (zone-aware).

    A day the parser cannot read ("next week", "agle hafte") is
    ``day_unclear`` — the flow asks for a specific day rather than confirming
    an unverified one — unless ``accept_unparsed_day`` opts into the old
    accept-as-spoken behaviour. An EMPTY day is simply "not given yet". The
    time is only read once the day is known.
    """
    today = now.date()
    day = parse_day(day_text, today)
    if day.kind == "invalid":
        return VisitResolution(False, "day_invalid", day)
    if day.kind == "past":
        return VisitResolution(False, "day_past", day)
    if day.kind == "unknown" and _norm(day_text) and not accept_unparsed_day:
        return VisitResolution(False, "day_unclear", day)
    if day.kind != "ok" or not _norm(time_text):
        return VisitResolution(True, None, day)
    t = parse_time(time_text)
    if t.kind == "invalid":
        return VisitResolution(False, "time_invalid", day, t)
    if t.kind == "unknown":
        return VisitResolution(False, "time_unclear", day, t)   # said, not understood: ask, never guess
    is_today = day.value == today
    now_minutes = now.hour * 60 + now.minute
    if t.kind == "ambiguous":
        # A bare hour is never accepted: morning or evening is asked on any
        # day; when both readings are already gone today it is simply past.
        if is_today and all(c <= now_minutes for c in t.candidates):
            return VisitResolution(False, "time_past", day, t)
        return VisitResolution(False, "time_ambiguous", day, t)
    if not is_today:
        return VisitResolution(True, None, day, t)
    if t.kind == "exact" and t.candidates[0] <= now_minutes:
        return VisitResolution(False, "time_past", day, t)
    if t.kind == "period" and _PERIOD_ENDS.get(t.period, 24 * 60) <= now_minutes:
        return VisitResolution(False, "time_past", day, t)
    return VisitResolution(True, None, day, t)


def validate_visit(day_text, time_text, now: datetime, *,
                   accept_unparsed_day: bool = False) -> tuple[bool, str | None]:
    """(ok, reason) for a spoken day/time — see :func:`resolve_visit`."""
    r = resolve_visit(day_text, time_text, now, accept_unparsed_day=accept_unparsed_day)
    return r.ok, r.reason


def _first_filled(slots: dict, names: list) -> str:
    for name in names:
        value = slots.get(str(name))
        if value is not None and str(value).strip():
            return str(value)
    return ""


def pending_time_variable(config: dict) -> str:
    """Slot that holds a refused bare hour while its morning/evening is asked."""
    time_vars = [str(v) for v in (config.get("timeVariables") or []) if str(v).strip()]
    return str(config.get("pendingTimeVariable") or (f"{time_vars[0]}_pending" if time_vars else ""))


def validate_ask(config: dict, slots: dict, now: datetime | None = None) -> Verdict:
    """Apply a node's ``validation`` config to the slots as they stand.

    Mutates the pending-hour slot — a bare hour refused as ``time_ambiguous``
    is parked there, and a later bare period answer ("शाम", "PM") is composed
    with it into every time slot, so the caller's clarification completes the
    time instead of replacing it — and the optional normalized companions
    (``normalizedDayVariable`` / ``normalizedTimeVariable``): written from
    the resolution whenever a day is accepted ('' time for a day alone),
    cleared on every rejection. The spoken slots are never rewritten.
    """
    if not isinstance(config, dict) or config.get("type") != VALIDATION_TYPE:
        return Verdict(True)
    day_vars = [str(v) for v in (config.get("dayVariables") or []) if str(v).strip()]
    time_vars = [str(v) for v in (config.get("timeVariables") or []) if str(v).strip()]
    normalized = tuple(
        str(config.get(key) or "").strip()
        for key in ("normalizedDayVariable", "normalizedTimeVariable")
    )
    pending_var = pending_time_variable(config)
    pending = str(slots.get(pending_var) or "").strip() if pending_var else ""
    time_text = _first_filled(slots, time_vars)
    if pending and time_text:
        composed = compose_time(time_text, pending)
        if composed:
            for name in time_vars:
                slots[name] = composed
            time_text = composed
        slots.pop(pending_var, None)   # consumed, or superseded by a full time
    current = now or now_in(config.get("timezone") or DEFAULT_TIMEZONE)
    resolved = resolve_visit(
        _first_filled(slots, day_vars), time_text, current,
        accept_unparsed_day=config.get("acceptUnparsedDay") is True,
    )
    if resolved.ok:
        if resolved.iso_date:
            for name, value in zip(normalized, (resolved.iso_date, resolved.normalized_time)):
                if name:
                    slots[name] = value
        return Verdict(True)
    reason = resolved.reason or "day_unclear"
    if reason == "time_ambiguous" and pending_var and time_text:
        slots[pending_var] = time_text
    clear = tuple(day_vars if reason.startswith("day_") else time_vars) + tuple(n for n in normalized if n)
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
        "en": "Is that in the morning or in the evening? Please tell me, for example 4 PM.",
        "hi": "ये समय सुबह का है या शाम का? आप बताइए, जैसे शाम 4 बजे।",
    },
    "time_unclear": {
        "en": "I could not understand that time. Please tell me the time again, for example 4 PM.",
        "hi": "ये समय समझ नहीं आया। आप समय फिर से बताइए, जैसे शाम 4 बजे।",
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
    "DEFAULT_TIMEZONE", "VALIDATION_TYPE", "DayResult", "TimeResult", "Verdict", "VisitResolution",
    "HOUR_WORDS", "HOUR_WORDS_HI", "HOUR_WORDS_ROMAN", "HOUR_WORDS_EN", "FRACTION_WORDS",
    "SPECIAL_TIMES", "MINUTE_WORDS_EN", "MINUTE_WORDS_HI", "PERIOD_WORDS", "DAY_WORDS_ROMAN",
    "now_in", "parse_day", "parse_time", "normalize_time", "compose_time", "resolve_visit",
    "validate_visit", "validate_ask", "pending_time_variable", "rejection_reply",
    "time_capture_regex", "day_capture_regex", "extract_time", "extract_day",
]
