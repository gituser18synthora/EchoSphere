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
    # Sarvam sometimes transliterates English digit words into Devanagari
    # during a Hindi turn ("Seven zero one" -> "सेवन जीरो वन"). These are
    # still numeric payload, not a language switch. Spelling variants observed
    # in live transcripts ("सेवेन ज़ीरो ज़ीरो वन ज़ीरो ज़ीरो टू") included.
    "वन": 1, "टू": 2, "टु": 2, "थ्री": 3, "फोर": 4, "फ़ोर": 4,
    "फाइव": 5, "फ़ाइव": 5, "सिक्स": 6, "सेवन": 7, "सेवेन": 7,
    "एट": 8, "नाइन": 9, "नाईन": 9, "ज़ेरो": 0, "जेरो": 0,
}

# Single-digit words for the other platform-enabled Indian languages, keyed
# by language for auditability — one place to extend when a language is
# enabled. Native-script words are inherently unambiguous (a Tamil-script
# token IS Tamil); romanizations are curated so no entry collides with an
# English word, a common Hinglish token, or an Indian given name (excluded:
# "be"/"don"/"tran" [English], "anju"/"sunna" [name / "सुन ना"], "oru"
# [Tamil "a/an"]).
_INDIC_DIGITS_BY_LANGUAGE: dict[str, dict[str, int]] = {
    "mr": {  # Marathi (Devanagari; shares शून्य/एक/तीन/चार/सात/आठ with Hindi)
        "दोन": 2, "पाच": 5, "सहा": 6, "नऊ": 9,
        "shoonya": 0, "dona": 2, "saha": 6, "sahaa": 6,
    },
    "gu": {  # Gujarati
        "શૂન્ય": 0, "મીંડું": 0, "એક": 1, "બે": 2, "ત્રણ": 3, "ચાર": 4,
        "પાંચ": 5, "છ": 6, "સાત": 7, "આઠ": 8, "નવ": 9,
        "panch": 5, "nav": 9,
    },
    "pa": {  # Punjabi (Gurmukhi)
        "ਸਿਫ਼ਰ": 0, "ਸਿਫਰ": 0, "ਜ਼ੀਰੋ": 0, "ਇੱਕ": 1, "ਦੋ": 2, "ਤਿੰਨ": 3,
        "ਚਾਰ": 4, "ਪੰਜ": 5, "ਛੇ": 6, "ਸੱਤ": 7, "ਅੱਠ": 8, "ਨੌਂ": 9, "ਨੌ": 9,
        "ik": 1, "tinn": 3, "panj": 5, "satt": 7, "naun": 9,
    },
    "ta": {  # Tamil
        "பூஜ்யம்": 0, "சுழியம்": 0, "ஜீரோ": 0, "ஒன்று": 1, "இரண்டு": 2,
        "மூன்று": 3, "நான்கு": 4, "ஐந்து": 5, "ஆறு": 6, "ஏழு": 7,
        "எட்டு": 8, "ஒன்பது": 9,
        "onru": 1, "onnu": 1, "irandu": 2, "rendu": 2, "moonru": 3,
        "moonu": 3, "naangu": 4, "ainthu": 5, "aaru": 6, "ezhu": 7,
        "ettu": 8, "onbathu": 9, "onpathu": 9,
    },
    "te": {  # Telugu
        "సున్నా": 0, "ఒకటి": 1, "రెండు": 2, "మూడు": 3, "నాలుగు": 4,
        "ఐదు": 5, "ఆరు": 6, "ఏడు": 7, "ఎనిమిది": 8, "తొమ్మిది": 9,
        "okati": 1, "moodu": 3, "nalugu": 4, "aidu": 5, "enimidi": 8,
        "tommidi": 9,
    },
    "ml": {  # Malayalam
        "പൂജ്യം": 0, "ഒന്ന്": 1, "രണ്ട്": 2, "മൂന്ന്": 3, "നാല്": 4,
        "അഞ്ച്": 5, "ആറ്": 6, "ഏഴ്": 7, "എട്ട്": 8, "ഒമ്പത്": 9,
        "ഒൻപത്": 9,
        "poojyam": 0, "randu": 2, "moonnu": 3, "naalu": 4, "anchu": 5,
        "ombathu": 9,
    },
    "ur": {  # Urdu (Arabic script; romanized forms match the Hindi set)
        "صفر": 0, "ایک": 1, "دو": 2, "تین": 3, "چار": 4, "پانچ": 5,
        "چھ": 6, "چھے": 6, "سات": 7, "آٹھ": 8, "نو": 9, "نौ": 9,
    },
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
for _lang_words in _INDIC_DIGITS_BY_LANGUAGE.values():
    _SINGLE_DIGITS.update(_lang_words)

_MULTI_DIGIT: dict[str, int] = {}
_MULTI_DIGIT.update(_EN_TEENS_TENS)
_MULTI_DIGIT.update(_HI_ROMAN_TENS)
_MULTI_DIGIT.update({w: v for w, v in _HI_COMPOUND.items() if v >= 10})

_DEVANAGARI_DIGIT_CHARS = str.maketrans("०१२३४५६७८९", "0123456789")

_STRIP_PUNCT = "।,.!?;:'\"()[]{}"

# ── generic script-digit normalization ──────────────────────────────────────
# Any Unicode decimal digit (Devanagari ०, Gurmukhi ੬, Tamil ௬, Telugu ౬,
# Malayalam ൬, Gujarati ૬, Arabic-Indic ٦/۶, …) maps to its ASCII form via
# the Unicode character database — no per-script tables to maintain. The
# translation cache grows only with characters actually seen.

_script_digit_cache: dict[str, str] = {}


def normalize_script_digits(text: str) -> str:
    """Rewrite every non-ASCII decimal-digit character to ASCII 0-9."""
    import unicodedata

    out: list[str] = []
    for ch in text or "":
        if ch in _script_digit_cache:
            out.append(_script_digit_cache[ch])
            continue
        mapped = ch
        if not ch.isascii():
            value = unicodedata.decimal(ch, None)
            if value is not None:
                mapped = str(value)
        _script_digit_cache[ch] = mapped
        out.append(mapped)
    return "".join(out)


def _clean(token: str) -> str:
    return token.strip(_STRIP_PUNCT).lower()


def is_number_word(token: str) -> bool:
    """Whether one token is a spoken-number word in any supported form."""
    word = _clean(token)
    if not word:
        return False
    if normalize_script_digits(word).replace(",", "").isdigit():
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
    normalized = normalize_script_digits(text or "")
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


# ── numeric-identifier capture (booking IDs, OTPs, references) ───────────────
# Callers dictate identifiers digit-by-digit, chunked by pauses, in any of the
# platform languages, often with "double"/"triple" constructs. STT writes that
# as digit WORDS or as SPACED digit groups ("6 0 1 0 1 1", "60 10 11") — both
# invisible to `[0-9]{4,10}`-style entity patterns. These helpers rewrite an
# utterance so identifier regexes see one contiguous digit run, and classify
# whether an utterance IS a dictated number (the guard that keeps this
# normalization away from ordinary conversation).

# Digit groups separated by spaces / common dictation separators fuse into one
# run: "6 0 1-0 1 1" → "601011". Sarvam can terminate one numeric chunk with
# a Devanagari danda before emitting the remaining digits in the SAME final
# ("7001। 0 0 1"); a danda between digits is therefore a dictation separator,
# not a sentence boundary. A period is accepted only when followed by
# whitespace, which preserves the equivalent STT shape ("7001. 0 0 1") but
# does not reinterpret decimal/time-like "7.00". Colons and slashes are never
# separators, so clock/date forms cannot silently become identifiers.
_DIGIT_RUN = re.compile(
    r"\d+(?:(?:[\s,\-]+|[।॥]\s*|\.\s+)\d+)*"
)

# Tokens that carry no content in a dictated number ("uh, six zero... umm").
_DICTATION_FILLERS = frozenset({
    "uh", "um", "umm", "hmm", "hm", "ah", "aa", "haan", "han", "ji", "yes",
    "ok", "okay", "so", "it's", "its", "is", "the", "अच्छा", "हाँ", "हां",
    "जी", "तो",
    # Identifier lead-ins are scaffolding around the dictated value, not
    # evidence that the utterance is ordinary prose. Keeping them here lets
    # a partial "mera order ID hai seven" enter multi-turn accumulation.
    "my", "mera", "meri", "order", "id", "number", "num", "reference",
    "hai", "hain", "मेरा", "मेरी", "ऑर्डर", "आईडी", "आई", "डी", "नंबर",
    "रेफरेंस", "है", "हैं",
})


def fuse_digit_runs(text: str) -> str:
    """Join adjacent digit groups into contiguous runs, keep other words.

    "id is 6 0 1 0 1 1 ok" → "id is 601011 ok"; "60 10 11" → "601011".
    Run on ALREADY-verbalized text (see :func:`spoken_digit_text`).
    """
    normalized = normalize_script_digits(text or "")
    return _DIGIT_RUN.sub(
        lambda m: re.sub(r"[\s,.\-।॥]", "", m.group(0)), normalized
    )


def spoken_digit_text(text: str) -> str:
    """Full identifier normalization: words → digits, then runs fused.

    "Six zero one zero double one." → "601011."
    "छह शून्य एक शून्य एक एक"        → "601011"
    "6 0 1 0 1 1"                    → "601011"
    Non-number words pass through untouched, so an identifier regex can still
    anchor on surrounding text ("booking BK 601011").
    """
    return fuse_digit_runs(verbalized_digits(text or ""))


def digits_dominant(text: str) -> bool:
    """Whether the utterance is essentially a dictated number.

    True when it contains at least one number word/digit group and everything
    else is dictation filler ("yes", "umm", "ji") — at most two such tokens.
    This is the gate for multi-turn identifier accumulation: "six zero" and
    "one zero double one" qualify; "my room is on floor 2" does not.
    """
    number_tokens = 0
    other_tokens = 0
    for token in (text or "").split():
        word = _clean(token)
        if not word:
            continue
        if is_number_word(word):
            number_tokens += 1
        elif word in _DICTATION_FILLERS:
            continue
        else:
            other_tokens += 1
    return number_tokens >= 1 and other_tokens == 0 or (
        number_tokens >= 2 and other_tokens <= 1
    )


def spoken_digit_sequence(text: str) -> str:
    """The digits of a dictated number, as one string ("six zero" → "60").

    Longest fused digit run in the normalized utterance; empty string when
    the utterance carries no digits.
    """
    runs = _DIGIT_RUN.findall(fuse_digit_runs(verbalized_digits(text or "")))
    if not runs:
        return ""
    return max(runs, key=len)


def pure_digit_payload(text: str) -> str:
    """Digits of an utterance that consists ONLY of spoken-number tokens.

    Stricter than :func:`digits_dominant` — no filler tokens are tolerated at
    all, so this can safely admit a short segment past the transcript gate's
    unsupported-script rejection while an identifier is being collected
    (Gujarati "સાત" for a numeric ask → "7"). An utterance carrying ANY
    non-number word returns "" and stays subject to the normal gate rules,
    which is what keeps arbitrary unsupported-language sentences out.
    """
    saw_number = False
    for token in (text or "").split():
        word = _clean(token)
        if not word:
            continue
        if word in _REPEAT_WORDS:
            continue
        if not is_number_word(word):
            return ""
        saw_number = True
    if not saw_number:
        return ""
    return spoken_digit_sequence(text)
