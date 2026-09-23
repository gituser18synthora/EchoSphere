"""TTS text preparation: sanitizing and sentence splitting.

Ported from the legacy VoiceBot ``audio/tts_text.py`` and
``audio/sentence_splitter.py``. Pure functions, stdlib only, Indic-script
aware (Devanagari danda/double-danda are treated as sentence terminators and
Devanagari text gets a danda when terminal punctuation must be added).
"""

from __future__ import annotations

import re

_INVISIBLE_RE = re.compile(
    r"[\u200b-\u200f\u2028-\u202f\u2060-\u2064\u2066-\u2069\ufeff\u00ad]+"
)

_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

_TERMINAL_PUNCT = frozenset(".?!…।॥")

# Fixed-width lookbehinds only — just digit and single capital letter.
# Abbreviations are handled by _is_abbreviation_boundary() below.
_SENTENCE_END_RE = re.compile(r"(?<!\d)(?<![A-Z])[.?!…।॥](?:\s|$)")

_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "sr", "jr", "vs",
    "etc", "e.g", "i.e", "prof", "rev", "gen",
    "fig", "approx", "dept", "est",
})

_LEAD_IN_WORDS = frozenset({
    "certainly", "sure", "absolutely", "ofcourse", "of course",
    "great", "okay", "ok", "alright", "noted", "understood",
    "yes", "no", "right", "exactly", "indeed", "perfect",
    "thanks", "thank you", "sorry", "apologies",
})

_LEAD_IN_ONLY_RE = re.compile(
    r"^(?:" + "|".join(re.escape(w) for w in _LEAD_IN_WORDS) + r")[.!?,\s]*$",
    re.IGNORECASE,
)

_LIST_ITEM_RE = re.compile(r"(?:^|\s)(?:\d+|[A-Za-z])[.)]\s+")

_ABBREV_ENDINGS = (
    "Mr.", "Mrs.", "Ms.", "Dr.", "Sr.", "Jr.", "vs.", "etc.", "e.g.", "i.e.",
)

_SIMPLE_SPLIT_RE = re.compile(r"(?<=[.?!])\s+")


# Any Unicode letter or digit, any script (\w minus underscore). A segment
# without one — an orphan ".", "…", "-", an emoji tail — has nothing a TTS
# engine can voice. Sarvam's streaming API rejects such payloads with a 422
# error event AND closes the socket, so they must never reach a provider.
_SPEAKABLE_RE = re.compile(r"[^\W_]")

# Identifiers are references, not quantities.  TTS providers commonly read a
# compact value such as ``601001`` as "six hundred one thousand and one", and
# ElevenLabs v3 garbled an order's last four digits ``9203`` outright
# (cv_3324204e8584, 2026-09-23), which makes the identifier impossible to
# verify over a call.  Keep the rewrite deliberately label-scoped so dates,
# amounts, room counts and other ordinary numbers retain their natural
# pronunciation: a digit run is spaced only when an identifier label sits
# right in front of it, and never when a currency/percent word follows it or
# it continues into a date/decimal.
_ID_NOUNS = (
    r"booking|reservation|बुकिंग|आरक्षण"
    r"|order|ऑर्डर|ऑर्डर|आर्डर|ओर्डर"
    r"|ticket|टिकट|टिकेट"
    r"|reference|ref|रेफरेंस|रेफ़रेंस|रिफरेंस"
    r"|transaction|txn|ट्रांज़ैक्शन|ट्रांजैक्शन|ट्रांजेक्शन"
    r"|complaint|शिकायत|case|केस|pnr|पीएनआर|utr|otp|ओटीपी"
    r"|tracking|ट्रैकिंग|awb|consignment|shipment|शिपमेंट|parcel|पार्सल"
)
# Labels that mark a reference on their own ("customer ID 700102",
# "phone number 9876543210", "OTP 4821", "pin code 400001").
_ID_WORDS = (
    r"id|आईडी|आई\.?\s?डी\.?|number|नंबर|नम्बर|संख्या|no\.?|num|code|कोड"
)
# "…ऑर्डर का आखिरी चार अंक 9203 हैं" / "last four digits are 9203" — the
# digit-count noun right before the run is the label.
_DIGITS_WORDS = r"digits?|अंक|अंकों|ank|ankon"
_ID_CONNECTORS = r"is|are|was|hai|hain|tha|है|हैं|था"
# A labelled run that is really a quantity ("order 1200 rupees ka tha").
_QUANTITY_SUFFIX = (
    r"रुपये|रुपए|रुपया|रु\.?|rupees?|rupaye|rupay|rs\.?|inr|₹"
    r"|%|percent|प्रतिशत|paise|पैसे"
)
_BOOKING_ID_RE = re.compile(
    r"(?P<prefix>(?<!\w)"
    r"(?:(?:" + _ID_NOUNS + r")(?:\s+(?:" + _ID_WORDS + r"))?"
    r"|(?:" + _ID_WORDS + r")"
    r"|(?:" + _DIGITS_WORDS + r"))"
    r"(?:\s+(?:" + _ID_CONNECTORS + r"))?\s*[:#-]?\s*)"
    r"(?P<identifier>\d{4,18})"
    r"(?!\d|[-/.:]\d|\s*(?:" + _QUANTITY_SUFFIX + r")(?!\w))",
    re.IGNORECASE,
)


# ── foreign-script guard ─────────────────────────────────────────────────────
# Smaller LLMs generating Indic text occasionally leak tokens from unrelated
# scripts ("രണ്ട് հազար രൂപ", "XX0923 бойынша", "具体മായ") — observed on a
# Malayalam collections bot with gpt-5-mini. Such characters are unspeakable
# for the target voice and can break the provider. Letters from scripts that
# cannot belong to the reply language are removed before synthesis; Latin
# (code-switched business terms, names) and Devanagari (Hindi lender/brand
# names used across the platform) are always kept, as are digits, symbols and
# punctuation. Unknown target languages keep the text untouched.
_SCRIPT_BY_BASE_LANGUAGE = {
    "hi": "DEVANAGARI", "mr": "DEVANAGARI", "ne": "DEVANAGARI",
    "ml": "MALAYALAM", "ta": "TAMIL", "te": "TELUGU", "kn": "KANNADA",
    "bn": "BENGALI", "gu": "GUJARATI", "pa": "GURMUKHI", "or": "ORIYA",
    "en": "LATIN",
}
_ALWAYS_ALLOWED_SCRIPTS = frozenset({"LATIN", "DEVANAGARI"})


def _letter_script(char: str) -> str | None:
    import unicodedata

    try:
        return unicodedata.name(char).split()[0]
    except ValueError:
        return None


def strip_foreign_scripts(text: str, language: str | None) -> tuple[str, dict[str, int]]:
    """Remove letters from scripts foreign to ``language``.

    Returns ``(cleaned_text, {script_name: removed_count})``. Nothing is
    removed for an unknown language, and the original text is returned when
    stripping would leave nothing speakable.
    """
    if not text:
        return text, {}
    base = (language or "").split("-")[0].lower()
    target = _SCRIPT_BY_BASE_LANGUAGE.get(base)
    if target is None:
        return text, {}
    allowed = _ALWAYS_ALLOWED_SCRIPTS | {target}
    removed: dict[str, int] = {}
    out: list[str] = []
    for char in text:
        if char.isalpha():
            script = _letter_script(char)
            if script is not None and script not in allowed and script != "DIGIT":
                removed[script] = removed.get(script, 0) + 1
                continue
        out.append(char)
    if not removed:
        return text, {}
    cleaned = re.sub(r" {2,}", " ", "".join(out)).strip()
    if not has_speakable_text(cleaned):
        return text, {}
    return cleaned, removed


def has_speakable_text(text: str) -> bool:
    """True when the text contains at least one letter or digit (any script)."""
    return bool(_SPEAKABLE_RE.search(text or ""))


def verbalize_booking_ids(text: str) -> str:
    """Space labelled identifiers so TTS reads them digit by digit.

    Covers booking/order/ticket/reference/transaction IDs, OTPs, "customer
    ID …", "phone number …" and "last four digits …" readouts in English,
    Hinglish and Devanagari.  Only a digit run directly after such a label is
    touched: amounts ("400 रुपये", "1200 rupees"), dates ("4 अगस्त",
    "2026-09-23") and unlabelled numbers keep their natural pronunciation.

    The visible assistant response and persisted transcript remain unchanged;
    this helper is part of TTS-only text preparation.  Applying it more than
    once is safe because an already spaced identifier no longer matches the
    compact-number pattern.
    """

    def _replace(match: re.Match[str]) -> str:
        identifier = match.group("identifier")
        return f"{match.group('prefix')}{' '.join(identifier)}"

    return _BOOKING_ID_RE.sub(_replace, text or "")


def sanitize_for_tts(text: str, *, ensure_terminal_punct: bool = False) -> str:
    """Normalize LLM output into provider-safe, unambiguous spoken text."""
    if not text:
        return ""
    cleaned = text.replace("\u00a0", " ")
    cleaned = _INVISIBLE_RE.sub("", cleaned)
    # Markdown markup is never spoken.
    cleaned = re.sub(r"[*_`#]+", "", cleaned)
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    cleaned = re.sub(r" +", " ", cleaned).strip()
    cleaned = verbalize_booking_ids(cleaned)
    if ensure_terminal_punct and cleaned and not _has_terminal_punct(cleaned):
        cleaned = cleaned + _default_terminal_punct(cleaned)
    return cleaned


def _has_terminal_punct(text: str) -> bool:
    stripped = text.rstrip()
    return bool(stripped) and stripped[-1] in _TERMINAL_PUNCT


def _default_terminal_punct(text: str) -> str:
    if _DEVANAGARI_RE.search(text):
        return "।"
    return "."


def _is_lead_in_only(sentence: str) -> bool:
    return bool(_LEAD_IN_ONLY_RE.match(sentence.strip()))


def _contains_list_item(text: str) -> bool:
    return bool(_LIST_ITEM_RE.search(text))


def _is_abbreviation_boundary(text: str, match_start: int) -> bool:
    """True when the punctuation at ``match_start`` belongs to an abbreviation."""
    before = text[:match_start].rstrip()
    if not before:
        return False
    last_word_match = re.search(r"(\w[\w.]*)$", before)
    if not last_word_match:
        return False
    return last_word_match.group(1).lower() in _ABBREVIATIONS


def _split_sentences(text: str) -> list[str]:
    """Split on sentence boundaries, skipping abbreviation periods.

    Terminal punctuation stays attached to its sentence.
    """
    results: list[str] = []
    last = 0
    for match in _SENTENCE_END_RE.finditer(text):
        if _is_abbreviation_boundary(text, match.start()):
            continue
        end = match.start() + 1
        chunk = text[last:end].strip()
        if chunk:
            results.append(chunk)
        last = end
    remainder = text[last:].strip()
    if remainder:
        results.append(remainder)
    return results


def split_for_tts(text: str) -> list[str]:
    """Split LLM output into TTS-ready chunks.

    Handles numbered/lettered lists (kept as one chunk), lead-in affirmations
    (joined with the next sentence), abbreviations (never split), and normal
    sentence boundaries on ``. ? ! … । ॥``.
    """
    if not text:
        return []
    text = sanitize_for_tts(text)
    if not text:
        return []

    if _contains_list_item(text):
        return [text]

    raw_sentences = _split_sentences(text)

    merged: list[str] = []
    i = 0
    while i < len(raw_sentences):
        sentence = raw_sentences[i].strip()
        if not sentence:
            i += 1
            continue
        if _is_lead_in_only(sentence) and i + 1 < len(raw_sentences):
            next_sentence = raw_sentences[i + 1].strip()
            merged.append(f"{sentence} {next_sentence}".strip())
            i += 2
        else:
            merged.append(sentence)
            i += 1

    return [s for s in merged if s]


def truncate_at_sentence_boundary(text: str, max_words: int = 100) -> str:
    """Truncate to at most ``max_words`` words, cutting at a sentence boundary."""
    words = text.split()
    if len(words) <= max_words:
        return text
    chunk = " ".join(words[:max_words])
    matches = [
        m for m in _SENTENCE_END_RE.finditer(chunk)
        if not _is_abbreviation_boundary(chunk, m.start())
    ]
    if matches:
        end = matches[-1].start() + 1
        return chunk[:end].strip()
    return sanitize_for_tts(chunk, ensure_terminal_punct=True)


def _ends_with_continuator(segment: str) -> bool:
    stripped = segment.rstrip()
    for token in _ABBREV_ENDINGS:
        if stripped.endswith(token):
            return True
    return bool(re.search(r"\d\.\d$", stripped))


def split_into_sentences(text: str) -> list[str]:
    """Lightweight sentence splitter on ``. ? !`` with abbreviation/decimal merging."""
    stripped = (text or "").strip()
    if not stripped:
        return []

    parts = _SIMPLE_SPLIT_RE.split(stripped)
    merged: list[str] = []
    current = ""
    for part in parts:
        current = part if not current else f"{current} {part}"
        if not _ends_with_continuator(current):
            piece = current.strip()
            if piece:
                merged.append(piece)
            current = ""

    tail = current.strip()
    if tail:
        merged.append(tail)

    if not merged:
        return [stripped]
    return merged
