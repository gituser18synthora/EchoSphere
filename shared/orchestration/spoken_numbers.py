"""Spoken-number understanding (hi / hinglish / en), shared platform-wide.

Two consumers with one lexicon:

- **Reference/number capture** (``voice_runtime.call_policy``): a caller reads
  a UTR/transaction/phone/OTP value out loud — "nine nine zero one two three",
  "नौ नौ शून्य एक दो तीन", "double nine…", "नौ सौ निन्यानवे चार सौ छत्तीस",
  mixed Hindi/English digit words, grouped with pauses. ``verbalized_digits``
  rewrites those spans into digit strings while leaving every other word
  untouched, so downstream extraction regexes see plain digits.

- **Language routing** (``ConversationBrain._maybe_switch_language``): number
  words and code-switched business terms ("UTR", "payment", "account") are
  NUMERIC/TECHNICAL PAYLOAD, not evidence of the conversation's language. A
  Hindi/Hinglish caller saying "UTR number hai nine nine zero one two three"
  has not switched to English. ``meaningful_language_words`` returns the
  tokens that MAY vote in a language decision — everything else is stripped.

Number-language and conversation-language are deliberately two separate
concepts; nothing here ever rewrites the caller's words for the LLM or the
transcript — both consumers work on derived copies.
"""

from __future__ import annotations

import re

# ── digit words ─────────────────────────────────────────────────────────────

_EN_DIGITS = {
    "zero": 0, "oh": 0, "nought": 0,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}

# Romanized Hindi digit words, with common STT spelling variants.
_HI_ROMAN_DIGITS = {
    "shunya": 0, "sifar": 0, "jeero": 0, "ziro": 0,
    "ek": 1, "aik": 1,
    "do": 2,
    "teen": 3, "tin": 3,
    "char": 4, "chaar": 4,
    "paanch": 5, "panch": 5, "panc": 5,
    "chhe": 6, "che": 6, "chha": 6, "cheh": 6,
    "saat": 7, "sat": 7,
    "aath": 8, "ath": 8,
    "nau": 9,
}

_HI_DEVANAGARI_DIGITS = {
    "शून्य": 0, "शुन्य": 0, "जीरो": 0, "ज़ीरो": 0,
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4,
    "पाँच": 5, "पांच": 5, "छह": 6, "छे": 6, "छः": 6,
    "सात": 7, "आठ": 8, "नौ": 9,
}

# "double nine" / "triple two" / "डबल नौ" — repetition prefixes.
_REPEAT_WORDS = {
    "double": 2, "dabal": 2, "डबल": 2,
    "triple": 3, "tripal": 3, "ट्रिपल": 3, "ट्रिपल्": 3,
}

# Hindi 0–99 (Devanagari), Indian-system counting — the same list the TTS
# verbalizer uses (voice_runtime.call_policy imports it from here now).
ONES_HI = (
    "शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह "
    "सोलह सत्रह अठारह उन्नीस बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस "
    "सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस "
    "सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चौवालीस "
    "पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन "
    "चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ इकसठ बासठ तिरसठ चौंसठ "
    "पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर "
    "पचहत्तर छिहत्तर सतहत्तर अठहत्तर उन्यासी अस्सी इक्यासी बयासी तिरासी "
    "चौरासी पचासी छियासी सतासी अठासी नवासी नब्बे इक्यानबे बानबे तिरानबे "
    "चौरानबे पचानबे छियानबे सत्तानबे अट्ठानबे निन्यानबे"
).split()

# Common orthographic variants ("निन्यानवे" vs list's "निन्यानबे").
_ONES_HI_VARIANTS = {
    "निन्यानवे": 99, "अठानवे": 98, "सत्तानवे": 97, "छियानवे": 96,
    "पचानवे": 95, "चौरानवे": 94, "तिरानवे": 93, "बानवे": 92, "इक्यानवे": 91,
}

_HI_COMPOUND = {word: value for value, word in enumerate(ONES_HI)}
_HI_COMPOUND.update(_ONES_HI_VARIANTS)

_EN_TEENS_TENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

# Romanized Hindi tens/compounds. Deliberately EXCLUDES romanizations that
# collide with common non-number words: "das" (the surname Das), "saath"
# (साथ, "with"), "bees" is kept — an English "bees" on a collections call is
# vanishingly rare, a spoken बीस is not.
_HI_ROMAN_TENS = {
    "dus": 10, "gyarah": 11, "barah": 12, "terah": 13,
    "chaudah": 14, "pandrah": 15, "solah": 16, "satrah": 17, "atharah": 18,
    "unnis": 19, "bees": 20, "tees": 30, "chalis": 40, "chaalis": 40,
    "pachas": 50, "sattar": 70, "assi": 80, "nabbe": 90,
    "ninyanve": 99, "ninyanbe": 99,
}

_MAGNITUDES = {
    "hundred": 100, "sau": 100, "सौ": 100,
    "thousand": 1000, "hazaar": 1000, "hazar": 1000, "हज़ार": 1000, "हजार": 1000,
    "lakh": 100_000, "lac": 100_000, "लाख": 100_000,
    "crore": 10_000_000, "करोड़": 10_000_000, "करोड": 10_000_000,
}

_SINGLE_DIGITS: dict[str, int] = {}
_SINGLE_DIGITS.update(_EN_DIGITS)
_SINGLE_DIGITS.update(_HI_ROMAN_DIGITS)
_SINGLE_DIGITS.update(_HI_DEVANAGARI_DIGITS)

_MULTI_DIGIT: dict[str, int] = {}
_MULTI_DIGIT.update(_EN_TEENS_TENS)
_MULTI_DIGIT.update(_HI_ROMAN_TENS)
_MULTI_DIGIT.update({w: v for w, v in _HI_COMPOUND.items() if v >= 10})

_DEVANAGARI_DIGIT_CHARS = str.maketrans("०१२३४५६७८९", "0123456789")

_STRIP_PUNCT = "।,.!?;:'\"()[]{}"


def _clean(token: str) -> str:
    return token.strip(_STRIP_PUNCT).lower()


def is_number_word(token: str) -> bool:
    """Whether one token is a spoken-number word in any supported form."""
    word = _clean(token)
    if not word:
        return False
    if word.translate(_DEVANAGARI_DIGIT_CHARS).replace(",", "").isdigit():
        return True
    return (
        word in _SINGLE_DIGITS
        or word in _MULTI_DIGIT
        or word in _MAGNITUDES
        or word in _REPEAT_WORDS
    )


# ── words → digit strings (reference/OTP/phone capture) ─────────────────────

def _flush_group(value: int | None, out: list[str]) -> None:
    if value is not None:
        out.append(str(value))


def verbalized_digits(text: str) -> str:
    """Rewrite spoken-number spans into digit strings; keep other words.

    Digit-by-digit words become single digits ("nine नौ zero" → "9 9 0").
    Repetitions expand ("double nine" → "9 9"). Compound values become their
    digit form ("निन्यानवे" → "99", "नौ सौ छत्तीस" → "936", "पचास हज़ार" →
    "50000"). Non-number words pass through untouched, so this is safe to run
    on a whole utterance before reference extraction.
    """
    normalized = (text or "").translate(_DEVANAGARI_DIGIT_CHARS)
    out: list[str] = []
    group: int | None = None  # value being composed by magnitudes (नौ सौ …)
    pending_repeat = 0

    for token in normalized.split():
        word = _clean(token)
        if pending_repeat and word in _SINGLE_DIGITS:
            out.extend([str(_SINGLE_DIGITS[word])] * pending_repeat)
            pending_repeat = 0
            continue
        pending_repeat = 0
        if word in _REPEAT_WORDS:
            _flush_group(group, out)
            group = None
            pending_repeat = _REPEAT_WORDS[word]
            continue
        if word in _MAGNITUDES:
            base = group if group is not None else None
            if base is None and out and out[-1].isdigit() and len(out[-1]) <= 2:
                # "9 सौ" — the digit was already emitted; pull it back in.
                base = int(out.pop())
            if base is None:
                base = 1
            group = base * _MAGNITUDES[word]
            continue
        if word in _MULTI_DIGIT:
            value = _MULTI_DIGIT[word]
            if group is not None:
                group += value
                _flush_group(group, out)
                group = None
            else:
                out.append(str(value))
            continue
        if word in _SINGLE_DIGITS:
            value = _SINGLE_DIGITS[word]
            if group is not None:
                # "नौ सौ ... एक" → units digit closes the composed group.
                group += value
                _flush_group(group, out)
                group = None
            else:
                out.append(str(value))
            continue
        # Not a number word: close any open group and pass the token through.
        _flush_group(group, out)
        group = None
        out.append(token)
    _flush_group(group, out)
    return " ".join(out)


# ── language-detection payload stripping ─────────────────────────────────────

# Code-switched business/technical terms that appear verbatim inside Hindi/
# Hinglish speech without meaning the caller switched to English. Deliberately
# unambiguous — everyday English words (no, yes, pay, call, today…) are NOT
# here, because they are genuine language evidence.
_CODE_SWITCH_TERMS = frozenset({
    "utr", "upi", "otp", "txn", "id", "ids", "atm", "kyc", "emi", "sms",
    "transaction", "transactions", "reference", "ref", "number", "no.",
    "account", "amount", "balance", "payment", "payments", "loan", "card",
    "debit", "credit", "bank", "banking", "paytm", "phonepe", "gpay",
    "google", "bhim", "neft", "imps", "rtgs", "cibil", "app", "link",
    "whatsapp", "rupees", "rupee", "rupaye", "rs", "screenshot", "cheque",
    "minimum", "total", "outstanding", "overdue", "penalty", "interest",
    "discount", "cashback", "voucher", "offer", "madam", "sir", "hello",
    "ok", "okay",
})


def meaningful_language_words(text: str) -> list[str]:
    """Tokens that may vote in a conversation-language decision.

    Strips digits, spoken-number words, and code-switched business terms —
    the numeric/technical payload of an utterance. What remains is the speech
    that actually carries the caller's language.
    """
    words: list[str] = []
    for token in (text or "").split():
        word = _clean(token)
        if not word:
            continue
        if is_number_word(word):
            continue
        if word in _CODE_SWITCH_TERMS:
            continue
        words.append(word)
    return words


def strip_numeric_payload(text: str) -> str:
    """The utterance minus numeric/technical payload, for script analysis."""
    return " ".join(meaningful_language_words(text))
