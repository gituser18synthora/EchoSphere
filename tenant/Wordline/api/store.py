"""Read-only merchant lookup table for the Wordline tenant API.

Loads ``data/merchants.json`` (produced by ``scripts/convert_csv.py``) and
indexes it by the keys a voice caller can actually give:

* the exact terminal id (``1231410A``),
* its *digit key* — the digits only (``1231410``).  The workflow's Terminal
  ID question captures digits (the STT rarely hears the trailing letter) and
  may keep the spaces or dashes a caller dictates with (``1 2 3 1 4 1 0``);
* the registered contact number, matched on its last ten digits;
* the merchant id (``ANCHORAPPMID``, ``0110656A0007856``) — exact, or its
  digit key when the caller's letter got lost, like the terminal id;
* the merchant (outlet) name — case-insensitive and partial, so it can
  match several merchants.  Several matches are reported as such
  (``NameMatches.records``); nothing here picks one of them.

Name matching runs in tiers and the first tier with any match decides:

1. *exact* — the whole name, ignoring case, spaces and punctuation
   ("westend entertainment"; "PVT LTD" = "Private Limited");
2. *partial* — every word of the query starts a word of the name
   ("bhanu medical", "la mode"), or the query without spaces is part of the
   name without spaces ("jaya lakshmi");
3. *phonetic* — the same word test on consonant keys, for speech-to-text
   spellings and Devanagari ("भानु मेडिकल", "Jayalaxmi").

Filler words a caller wraps a name in ("mera shop ka naam … hai", "my
shop is …") are dropped from the query first; a query with fewer than three
letters or digits left is rejected as too short.

4. *words* — only for a caller's free-text answer (``loose=True``; the bot's
   "MID and outlet name" question), after the strict tiers found nothing:
   the merchants sharing the most query words, provided at least one shared
   word is distinctive (not a generic business word such as MEDICAL, STORE,
   TRADERS).  "outlet Bhanu Medical, MID I don't know" still finds Bhanu;
   "xyz traders" finds nothing.  An explicit API name query never uses it.

The file is re-read when its modification time changes, so a regenerated
JSON is picked up without a restart.  Nothing here writes.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "merchants.json"

_PLACEHOLDER = re.compile(r"^\s*(\{\{.*\}\}|\{.*\}|\[.*\])\s*$")
_ALNUM = re.compile(r"[^A-Z0-9]")
_DIGITS = re.compile(r"\D")

MIN_TERMINAL_DIGITS = 4
MAX_TERMINAL_CHARS = 12
CONTACT_DIGITS = 10
MIN_MERCHANT_ID_DIGITS = 10
MAX_MERCHANT_ID_CHARS = 16
MIN_NAME_CHARS = 3
#: Most candidates a name search reports (the bot reads at most three).
MAX_NAME_CANDIDATES = 10


def is_placeholder(raw: object) -> bool:
    """An unresolved workflow template (``{{spoken_terminal_id_retry}}``) is
    not a value: the executor sends unknown placeholders literally."""
    return bool(_PLACEHOLDER.match(str(raw)))


def clean_value(raw: object) -> str:
    if raw is None or isinstance(raw, (dict, list)) or is_placeholder(raw):
        return ""
    return str(raw).strip()


def terminal_keys(raw: object) -> tuple[str, str]:
    """(exact alphanumeric key, digit key) for a dictated terminal id."""
    value = clean_value(raw).upper()
    exact = _ALNUM.sub("", value)
    digits = _DIGITS.sub("", value)
    if len(exact) > MAX_TERMINAL_CHARS:
        return "", ""
    return exact, digits


def contact_key(raw: object) -> str:
    """Last ten digits of a phone number, or "" when it is too short."""
    digits = _DIGITS.sub("", clean_value(raw))
    return digits[-CONTACT_DIGITS:] if len(digits) >= CONTACT_DIGITS else ""


def mask_contact(number: str, keep: int = 4) -> str:
    digits = _DIGITS.sub("", number or "")
    if not digits:
        return ""
    return "X" * max(0, len(digits) - keep) + digits[-keep:]


def merchant_id_keys(raw: object) -> tuple[str, str]:
    """(exact alphanumeric key, digit key) for a merchant id, or ("", "")
    when it cannot be one (fewer than ten digits, too long, two letters)."""
    value = clean_value(raw).upper()
    value = _SPELLED_LETTER.sub(r"\1", value)  # "A FOR APPLE" → "A"
    exact = _ALNUM.sub("", value)
    digits = _DIGITS.sub("", exact)
    if (len(digits) < MIN_MERCHANT_ID_DIGITS or len(exact) > MAX_MERCHANT_ID_CHARS
            or len(exact) - len(digits) > 1):
        return "", ""
    return exact, digits


# ── spoken identifiers inside free text ──────────────────────────────────────

_SPELLED_LETTER = re.compile(r"\b([A-Z])\s+FOR\s+[A-Z]+\b", re.I)
# A dictated id: digit runs and single letters separated by spaces/dashes.
_ID_RUN = re.compile(r"(?<![A-Za-z0-9])(?:\d+|[A-Za-z])(?:[\s,.-]*(?:\d+|[A-Za-z])(?![A-Za-z]))*")


def identifier_in_text(raw: object) -> tuple[str, str]:
    """The id a caller dictated inside free text, classified by its digits:
    ``("merchant_id", "0110656A0007856")`` for 10+ digits,
    ``("terminal_id", "1231410A")`` for 4-9 digits, else ``("", "")``.
    Used for the voice bot's free-text answers ("mera MID hai 0 1 1 …")."""
    text = _SPELLED_LETTER.sub(r"\1", clean_value(raw))
    best = ""
    for match in _ID_RUN.finditer(text):
        compact = re.sub(r"[^A-Za-z0-9]", "", match.group(0)).upper()
        if len(_DIGITS.sub("", compact)) > len(_DIGITS.sub("", best)):
            best = compact
    digits = len(_DIGITS.sub("", best))
    if digits >= MIN_MERCHANT_ID_DIGITS:
        return "merchant_id", best
    if digits >= MIN_TERMINAL_DIGITS:
        return "terminal_id", best
    return "", ""


# ── merchant names ───────────────────────────────────────────────────────────

_NAME_EQUIVALENTS = {"PVT": "PRIVATE", "LTD": "LIMITED", "&": "AND", "CO-OP": "COOPERATIVE"}
# Words callers wrap a name in; dropped from the query (never from names).
_NAME_FILLERS = frozenset("""
    my our mine is its it's it the a an of called named name naam nam shop shops dukan dukaan
    outlet merchant registered business company firm store's mera meri mere hamara hamari
    hamare apna apni apne ka ki ke hai hain he h ji sir madam haan han ha yes yeah ok okay
    and aur wala wali vala vali se jo please bas no nahi nahin nhi
    mid id tid terminal number code merchant's i don't dont know hoon hu main mujhe
    yaad yad pata malum maloom bhool gaya gayi abhi paas pass tha thi hamko humko
    एमआईडी आईडी टीआईडी मर्चेंट नंबर कोड पता
    मेरा मेरी मेरे हमारा हमारी हमारे अपना अपनी दुकान शॉप आउटलेट मर्चेंट रजिस्टर्ड नाम का की के है हैं जी
    हाँ हां सर और वाला वाली से नहीं नही
""".split())
# Devanagari → Latin for names (``voice_runtime.spoken_text`` keeps the same
# flattened table for echo matching; copied so this service stays standalone).
_DEVANAGARI = {
    "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e",
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
    "ं": "n", "ँ": "n", "ः": "h",
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f", "य़": "y",
}
_DEVANAGARI_BLOCK = re.compile(r"[\u0900-\u097f]")


def _latin(text: str) -> str:
    out = []
    for ch in text:
        key = _DEVANAGARI.get(ch)
        if key is not None:
            out.append(key)
        elif not _DEVANAGARI_BLOCK.match(ch):
            out.append(ch)
        # virama, nukta, danda: dropped
    return "".join(out)


def name_words(raw: object, *, query: bool = False) -> list[str]:
    """Upper-case comparison words of a merchant name (or of a query, with
    the caller's filler words removed)."""
    words = []
    for word in clean_value(raw).replace("&", " & ").split():
        if query and word.lower().strip(".,!?।") in _NAME_FILLERS:
            continue
        word = _latin(word).upper()
        word = _NAME_EQUIVALENTS.get(word.strip("."), word)
        word = _ALNUM.sub("", word)
        if word and not (query and word.lower() in _NAME_FILLERS):
            words.append(word)
    return words


def phonetic_key(word: str) -> str:
    """Consonant skeleton: what survives English vs. Devanagari spellings
    ("MEDICAL" / "medikl" → "MDKL", "JAYALAKSHMI" / "jylkshmi" → "JLKSM").
    A comparison key only — used by the last, ``phonetic`` name tier."""
    w = word.upper()
    # English C/CH vs. Devanagari क/च/स cannot be told apart from spelling
    # alone ("CHRONICLE" = क, "CHENNAI" = च): the key merges them; CE/CI/CY
    # sound like S ("OFFICE" = ऑफिस).
    w = re.sub(r"C(?=[EIY])", "S", w)
    for a, b in (("CH", "K"), ("SH", "S"), ("PH", "F"), ("X", "KS"),
                 ("C", "K"), ("Q", "K"), ("W", "V"), ("Z", "J")):
        w = w.replace(a, b)
    w = re.sub(r"(?<=[BCDFGJKLMNPRSTV])H", "", w)   # aspiration: BH→B, TH→T
    w = re.sub(r"[AEIOUY]", "", w)   # Y: "SPYKAR" = स्पाइकर, "JAYA" = जय
    return re.sub(r"(.)\1+", r"\1", w)


# Words many outlet names share: a free-text match on these alone is no match.
_GENERIC_NAME_WORDS = frozenset("""
    MEDICAL MEDICALS STORE STORES GENERAL HOTEL TRADERS PRIVATE LIMITED AND THE OF ENTERPRISES
    SERVICES SERVICE CENTRE CENTER SHOP SHOPS HOLDINGS INDIA LLP CO COOPERATIVE PHARMACY
""".split())


def _word_matches(q: str, qk: str, word: str, key: str) -> bool:
    """Free-text word test: the whole word, a 4+ letter start of it, or the
    same consonant key (no key prefixes — "KIRANA" must not hit "CORNER")."""
    return (word == q or (len(q) >= 4 and word.startswith(q))
            or bool(len(qk) >= 3 and key == qk))


def without_identifier(raw: object, ident: str) -> str:
    """``raw`` with the dictated id run removed ("0110656 A 0007856, Bhanu"
    → ", Bhanu"), for a name search over the rest of a free-text answer."""
    text = _SPELLED_LETTER.sub(r"\1", clean_value(raw))
    for match in _ID_RUN.finditer(text):
        if re.sub(r"[^A-Za-z0-9]", "", match.group(0)).upper() == ident:
            return (text[:match.start()] + " " + text[match.end():]).strip()
    return text


def _near(a: str, b: str) -> bool:
    """Edit distance <= 1 ("AIDA" ~ "IDA"; "YAAD" !~ "IDA")."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _phonetic_words_match(query: list[str], name: list[str]) -> bool:
    """Every query word matches a name word by consonant key — the key, or a
    2+ consonant start of it.  A one-consonant key ("YAAD" → "D", "AIDA" →
    "D") matches far too much, so such a word must also be spelled within
    one letter of the name word."""
    for q in query:
        qk = phonetic_key(q)
        if not qk:
            continue
        if not any((len(qk) >= 2 and phonetic_key(n).startswith(qk))
                   or (qk == phonetic_key(n) and (len(qk) >= 2 or _near(q, n)))
                   for n in name):
            return False
    return True


def _words_match(query: list[str], name: list[str], *, prefix_min: int = 1) -> bool:
    return all(any(n == q or (len(q) >= prefix_min and n.startswith(q)) for n in name)
               for q in query)


@dataclass(frozen=True)
class NameMatches:
    """Outcome of a merchant-name search.  ``records`` holds EVERY match of
    the deciding tier (sorted by name) — one record is a resolved merchant,
    several are a choice for the caller, none is a miss."""
    query_words: tuple[str, ...]
    tier: str                      # exact | partial | phonetic | ""
    records: tuple[dict, ...]
    too_short: bool = False


@dataclass(frozen=True)
class Match:
    record: dict
    matched_by: str  # terminal_id | terminal_digits | merchant_id | merchant_id_digits
                     # | merchant_name | contact_number


class MerchantStore:
    """Thread-safe, mtime-refreshing view over the merchants JSON file."""

    def __init__(self, path: Path = DATA_FILE):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._records: list[dict] = []
        self._by_terminal: dict[str, dict] = {}
        self._by_digits: dict[str, dict] = {}
        self._by_contact: dict[str, dict] = {}
        self._by_mid: dict[str, dict] = {}
        self._by_mid_digits: dict[str, dict] = {}
        self._names: list[tuple[dict, list[str], str, list[str]]] = []
        self.loaded_at: str | None = None
        self.meta: dict = {}

    # ── loading ───────────────────────────────────────────────────────────

    def _load_locked(self) -> None:
        document = json.loads(self.path.read_text(encoding="utf-8"))
        records = [r for r in document.get("records", []) if isinstance(r, dict)]
        by_terminal: dict[str, dict] = {}
        by_digits: dict[str, dict] = {}
        by_contact: dict[str, dict] = {}
        by_mid: dict[str, dict] = {}
        by_mid_digits: dict[str, dict] = {}
        names: list[tuple[dict, list[str], str, list[str]]] = []
        for record in records:
            mid, mid_digits = merchant_id_keys(record.get("merchant_id"))
            if mid:
                by_mid.setdefault(mid, record)
                by_mid_digits.setdefault(mid_digits, record)
            words = name_words(record.get("outlet_name"))
            if words:
                names.append((record, words, "".join(words),
                              [k for k in map(phonetic_key, words) if k]))
            exact, digits = terminal_keys(record.get("terminal_id"))
            if exact:
                by_terminal.setdefault(exact, record)
            if len(digits) >= MIN_TERMINAL_DIGITS:
                by_digits.setdefault(digits, record)
            contact = contact_key(record.get("contact_number"))
            if contact:
                by_contact.setdefault(contact, record)
        self._records = records
        self._by_terminal, self._by_digits, self._by_contact = by_terminal, by_digits, by_contact
        self._by_mid, self._by_mid_digits = by_mid, by_mid_digits
        self._names = sorted(names, key=lambda item: item[2])
        self.meta = document.get("_meta") or {}
        self.loaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def refresh(self, force: bool = False) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = None
        with self._lock:
            if force or mtime != self._mtime:
                self._load_locked()
                self._mtime = mtime

    # ── queries ───────────────────────────────────────────────────────────

    def records(self) -> list[dict]:
        self.refresh()
        return list(self._records)

    def __len__(self) -> int:
        self.refresh()
        return len(self._records)

    def find_terminal(self, raw: object) -> Match | None:
        self.refresh()
        exact, digits = terminal_keys(raw)
        if exact and exact in self._by_terminal:
            return Match(self._by_terminal[exact], "terminal_id")
        if len(digits) >= MIN_TERMINAL_DIGITS and digits in self._by_digits:
            return Match(self._by_digits[digits], "terminal_digits")
        return None

    def find_contact(self, raw: object) -> Match | None:
        self.refresh()
        key = contact_key(raw)
        record = self._by_contact.get(key) if key else None
        return Match(record, "contact_number") if record else None

    def find_merchant_id(self, raw: object) -> Match | None:
        """Exact merchant id (case-insensitive, separators ignored), else its
        digits when the caller gave no letter at all (``01106560007856`` for
        ``0110656A0007856``).  Never a prefix, never a different letter."""
        self.refresh()
        exact, digits = merchant_id_keys(raw)
        if exact and exact in self._by_mid:
            return Match(self._by_mid[exact], "merchant_id")
        # Digits only when the caller gave NO letter (the STT dropped it); a
        # different letter is a different id, not a lost one.
        if digits and exact == digits and digits in self._by_mid_digits:
            return Match(self._by_mid_digits[digits], "merchant_id_digits")
        return None

    def find_name(self, raw: object, *, loose: bool = False) -> NameMatches:
        """Every merchant whose name matches ``raw`` in the first matching
        tier (see the module docstring).  Never chooses between them.
        ``loose`` adds the *words* tier for a caller's free-text answer."""
        self.refresh()
        query = name_words(raw, query=True)
        compact = "".join(query)
        if len(compact) < MIN_NAME_CHARS:
            return NameMatches(tuple(query), "", (), too_short=True)
        exact = [r for r, _w, c, _k in self._names if c == compact]
        if exact:
            return NameMatches(tuple(query), "exact", tuple(exact))
        partial = [r for r, words, c, _k in self._names
                   if _words_match(query, words) or (len(compact) >= 4 and compact in c)]
        if partial:
            return NameMatches(tuple(query), "partial", tuple(partial))
        latin = [_latin(q.lower()).upper() for q in query]
        if len("".join(phonetic_key(q) for q in latin)) >= MIN_NAME_CHARS:
            phonetic = [r for r, words, _c, _k in self._names
                        if _phonetic_words_match(latin, words)]
            if phonetic:
                return NameMatches(tuple(query), "phonetic", tuple(phonetic))
        if loose:
            return NameMatches(tuple(query), *self._shared_words(query))
        return NameMatches(tuple(query), "", ())

    def _shared_words(self, query: list[str]) -> tuple[str, tuple[dict, ...]]:
        qkeys = [phonetic_key(q) for q in query]
        best, winners = 0, []
        for record, words, _c, _k in self._names:
            keys = [phonetic_key(w) for w in words]
            shared = [q for q, qk in zip(query, qkeys)
                      if len(q) >= 3 and any(_word_matches(q, qk, w, k) for w, k in zip(words, keys))]
            if not shared or all(q in _GENERIC_NAME_WORDS for q in shared):
                continue
            if len(shared) > best:
                best, winners = len(shared), [record]
            elif len(shared) == best:
                winners.append(record)
        return ("words", tuple(winners)) if winners else ("", ())

    def search(self, *, issue_code: str | None = None, bank: str | None = None) -> list[dict]:
        self.refresh()
        wanted_bank = (bank or "").strip().lower()
        return [
            r for r in self._records
            if (not issue_code or r.get("issue_code") == issue_code)
            and (not wanted_bank or wanted_bank in str(r.get("bank_name", "")).lower())
        ]
