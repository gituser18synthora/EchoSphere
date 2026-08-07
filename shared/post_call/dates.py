"""Resolve relative spoken date expressions to absolute dates.

Callers commit to relative moments — "tomorrow", "Monday", "अगले हफ़्ते",
"कल", "परसों" — and a commitment stored as its original string is useless on
the next call ("Monday" has passed, or means a different Monday). The analysis
LLM is asked to emit ISO dates directly (it is told today's date); this module
is the deterministic safety net that resolves or sanity-checks what it
returns, so a stored commitment date is always absolute.

Deliberately small: Hindi/Hinglish/English words for the platform's calling
base, forward-looking resolution (a promise is about the future — "Monday"
means the NEXT Monday, today included only if the word was "today"). Anything
unrecognized resolves to None and the raw expression is kept alongside.
"""

import re
from datetime import date, datetime, timedelta

# Weekday vocabulary → Python weekday() index (Monday = 0).
_WEEKDAYS: dict[str, int] = {
    # English
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
    "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
    # Hindi (Devanagari)
    "सोमवार": 0, "मंगलवार": 1, "बुधवार": 2, "गुरुवार": 3, "बृहस्पतिवार": 3,
    "शुक्रवार": 4, "शनिवार": 5, "रविवार": 6, "इतवार": 6,
    # Romanized Hindi
    "somvar": 0, "somwar": 0, "mangalvar": 1, "mangalwar": 1,
    "budhvar": 2, "budhwar": 2, "guruvar": 3, "guruwar": 3, "brihaspativar": 3,
    "shukravar": 4, "shukrawar": 4, "shanivar": 5, "shaniwar": 5,
    "ravivar": 6, "raviwar": 6, "itvar": 6, "itwar": 6,
}

_TODAY_WORDS = frozenset({"today", "आज", "aaj", "abhi", "अभी", "tonight"})
_TOMORROW_WORDS = frozenset({"tomorrow", "कल", "kal", "tmrw"})
_DAY_AFTER_WORDS = frozenset({"परसों", "parso", "parson", "parso"})
_NEXT_WEEK_RE = re.compile(
    r"next\s+week|अगले\s*(?:हफ़्ते|हफ्ते|सप्ताह)|agle\s*(?:hafte|saptah)", re.I
)
_MONTH_END_RE = re.compile(
    r"month\s*end|end\s+of\s+(?:the\s+)?month|महीने\s*के\s*(?:अंत|आख़िर|आखिर)|"
    r"mahine\s*ke\s*(?:ant|aakhir|akhir)", re.I
)
_IN_N_DAYS_RE = re.compile(r"(?:in|after)\s+(\d{1,2})\s+days?|(\d{1,2})\s+din", re.I)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# 15 August / 15th Aug / August 15
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "जनवरी": 1, "फरवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6,
    "जुलाई": 7, "अगस्त": 8, "सितंबर": 9, "अक्टूबर": 10, "नवंबर": 11, "दिसंबर": 12,
}
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-zऀ-ॿ]+)|"
    r"\b([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b"
)


def _month_number(word: str) -> int | None:
    lowered = word.strip().lower()
    for key, number in _MONTHS.items():
        if lowered.startswith(key) or word.strip().startswith(key):
            return number
    return None


def resolve_relative_date(
    expression: str | None, *, reference: datetime | date
) -> date | None:
    """Absolute date for a spoken relative expression, or None.

    ``reference`` is the moment the words were spoken (call start/end), which
    anchors "tomorrow"/"कल". Resolution is forward-looking: bare weekday names
    mean the NEXT such day after the reference date.
    """
    if not expression:
        return None
    text = str(expression).strip()
    if not text:
        return None
    today = reference.date() if isinstance(reference, datetime) else reference

    match = _ISO_DATE_RE.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    lowered = text.lower()
    tokens = set(re.findall(r"[a-zऀ-ॿ']+", lowered))
    if tokens & _TODAY_WORDS:
        return today
    if tokens & _DAY_AFTER_WORDS:  # before "kal": "kal parso" reads day-after
        return today + timedelta(days=2)
    if tokens & _TOMORROW_WORDS:
        return today + timedelta(days=1)
    if _NEXT_WEEK_RE.search(text):
        return today + timedelta(days=7)
    if _MONTH_END_RE.search(text):
        next_month = date(today.year + (today.month == 12), (today.month % 12) + 1, 1)
        return next_month - timedelta(days=1)

    match = _IN_N_DAYS_RE.search(text)
    if match:
        days = int(match.group(1) or match.group(2))
        return today + timedelta(days=days)

    for token in tokens:
        weekday = _WEEKDAYS.get(token)
        if weekday is not None:
            ahead = (weekday - today.weekday()) % 7
            return today + timedelta(days=ahead or 7)

    match = _DAY_MONTH_RE.search(text)
    if match:
        if match.group(1) and match.group(2):
            day, month = int(match.group(1)), _month_number(match.group(2))
        else:
            month, day = _month_number(match.group(3) or ""), int(match.group(4) or 0)
        if month and 1 <= day <= 31:
            year = today.year
            try:
                resolved = date(year, month, day)
            except ValueError:
                return None
            if resolved < today:  # a promise is forward-looking
                try:
                    resolved = date(year + 1, month, day)
                except ValueError:
                    return None
            return resolved
    return None
