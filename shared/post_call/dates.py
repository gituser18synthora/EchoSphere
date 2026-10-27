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
_DAY_THEN_MONTH_RE = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+([A-Za-zऀ-ॿ]+)")
_MONTH_THEN_DAY_RE = re.compile(r"\b([A-Za-z]+)\s+\d{1,2}(?:st|nd|rd|th)?\b")
# Date words resolve_relative_date does not resolve (it is forward-looking)
# but that still name a date: past-looking words, "DD tarikh", numeric dates.
# Devanagari words carry their own boundaries (\b does not know the script).
_DATE_MENTION_RE = re.compile(
    r"\b(?:yesterday|last\s+week|this\s+week|pichh?le\s+(?:hafte|saptah)|is\s+hafte|"
    r"\d{1,2}\s*(?:tarikh|tareekh|taarikh))\b|"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
    r"(?<![\u0900-\u097f])(?:कल|परसों|आज|पिछले\s*(?:हफ़्ते|हफ्ते|सप्ताह)|इस\s*(?:हफ़्ते|हफ्ते|सप्ताह)|"
    r"\d{1,2}\s*(?:तारीख़|तारीख|तारिख))(?![\u0900-\u097f])",
    re.I,
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



# Tense markers that disambiguate "kal"/"कल" (yesterday or tomorrow) and
# "parso"/"परसों" (two days either way) inside one clause.
_PAST_TENSE_RE = re.compile(
    r"\b(?:tha|thi|the|diya|diye|di|kiya|kiye|ki|gaya|gaye|gayi|hua|hui|hue|chuka|chuki|chuke|liya|"
    r"updated|changed|did|was|were|had|already|yesterday)\b"
    r"|(?<![\u0900-\u097f])(?:था|थी|थे|दिया|दिये|दी|किया|किये|की|गया|गये|गई|हुआ|हुई|हुए|चुका|चुकी|चुके|लिया)(?![\u0900-\u097f])",
    re.I,
)
_FUTURE_TENSE_RE = re.compile(
    r"\b(?:karna|karunga|karungi|karenge|karega|karegi|kar\s+do|kar\s+dena|kar\s+dijiye|karo|kijiye|"
    r"dena|dunga|denge|hoga|hogi|will|shall|going\s+to|tomorrow)\b"
    r"|(?<![\u0900-\u097f])(?:करना|करूँगा|करूंगा|करेंगे|करेगा|कर\s*दो|कर\s*देना|कर\s*दीजिए|करो|कीजिए|देना|दूँगा|दूंगा|देंगे|होगा|होगी)(?![\u0900-\u097f])",
    re.I,
)
_RELATIVE_DAY_RE = re.compile(
    r"\b(?:tomorrow|yesterday|kal|parso|parson)\b|(?<![\u0900-\u097f])(?:कल|परसों)(?![\u0900-\u097f])", re.I,
)


def relative_day_offset(text: str | None) -> int | None:
    """Days from the reference day that a clause's relative day word means.

    "tomorrow" → +1, "yesterday" → −1; "kal"/"कल" → +1 with a future-tense
    clause ("kal update karna"), −1 with a past-tense one ("kal update kar
    diya tha"); "parso"/"परसों" likewise ±2. None when the clause has no such
    word, or its tense is missing or mixed — the caller then cannot check.
    """
    if not text:
        return None
    s = " ".join(str(text).split())
    match = _RELATIVE_DAY_RE.search(s)
    if match is None:
        return None
    word = match.group(0).lower()
    if word == "tomorrow":
        return 1
    if word == "yesterday":
        return -1
    magnitude = 2 if word in ("parso", "parson", "परसों") else 1
    past, future = bool(_PAST_TENSE_RE.search(s)), bool(_FUTURE_TENSE_RE.search(s))
    if past == future:
        return None
    return -magnitude if past else magnitude


def contains_date_expression(text: str | None) -> bool:
    """Whether the text names a date at all: everything resolve_relative_date
    understands (today/tomorrow words, weekdays, next week, month end, "in N
    days", ISO, day + month) plus past-looking words (yesterday, last week,
    पिछले हफ्ते), "DD tarikh" and numeric dates. Used to check that a date the
    analyst attached to a field was actually said in the clause it cites."""
    if not text:
        return False
    s = " ".join(str(text).split())
    if (
        _ISO_DATE_RE.search(s) or _NEXT_WEEK_RE.search(s) or _MONTH_END_RE.search(s)
        or _IN_N_DAYS_RE.search(s) or _DATE_MENTION_RE.search(s)
    ):
        return True
    tokens = set(re.findall(r"[a-zऀ-ॿ']+", s.lower()))
    if tokens & (_TODAY_WORDS | _TOMORROW_WORDS | _DAY_AFTER_WORDS) or tokens & set(_WEEKDAYS):
        return True
    # Day + month in either order, scanned separately: the combined pattern's
    # leftmost match ("address 25") would hide a later "25 October".
    return any(
        _month_number(match.group(1))
        for pattern in (_DAY_THEN_MONTH_RE, _MONTH_THEN_DAY_RE)
        for match in pattern.finditer(s)
    )
