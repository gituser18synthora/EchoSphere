"""Speech-ready values for the merchant fields the voice bot reads out.

The field-team export is mostly ALL CAPS (``AGARWAL PUBLIC SCHOOL``,
``PAYTIVO-7210-2G+WIFI``, ``SHOP NO 17, SAIFEE PARK``).  The bot's TTS
(Sarvam ``bulbul:v4-flash``, voice ``shubh_hi_customer``) reads a capitalised
or hyphen-glued word as letters — measured 2026-11-10 by synthesising the
bot's own read-back sentences and transcribing them back verbatim:

* ``PAYTIVO-7210-2G+WIFI`` → "P A Y T I V O seven two one zero two G plus W Y";
* ``Hand held`` inside ``AL9220U-Hand held`` (Hindi reply) → "H A N D held";
* ``AGARWAL`` in a Hindi reply → "Egarwel"; ``Agarwal`` → "Agarwal";
* ``SHOP NO 17, SAIFEE PARK … MAZGAON`` (Hindi) → "S H O P N O … S A F E …
  M H gaon" (12.2 s); ``Shop Number 17, Saifee Park … Mazgaon`` → words (11.0 s).

So the API returns these four fields already shaped for speech (the stored
JSON keeps the CSV text unchanged, for traceability):

* ``spoken_name``    — MERCHANT (``outlet_name``) and Contact Person;
* ``spoken_model``   — ASSETMODEL (``asset_type``);
* ``spoken_address`` — Address.

Rules (deterministic, idempotent — a value already in natural case is left
alone):

* an ALL-CAPS word becomes Title case; small joining words (and, of, for, at,
  in, on, by, to) become lower case inside a value;
* kept as written: single letters / initials (``K C Tiwari``), known acronyms
  (``IDBI``, ``HQ``, ``LLP``, ``GPRS``, ``CHS`` …), any capitalised token with
  no vowel, Y counted as one (``PNB``, ``SBS``, ``NSK``, ``MMTC`` — they can
  only be spelt; ``DRY`` and ``BY`` are words), and
  every token that mixes letters and digits (model numbers ``AL9220U``,
  ``A9210``, ``2G``; house numbers ``A2``, ``48A``) — measured: ``AL9220U`` is
  read "A L nine two two zero U", which is how the model is said;
* ordinals lose their capital suffix (``19TH`` → ``19th``);
* spoken expansions of written abbreviations: ``PVT LTD`` → Private Limited,
  ``PROP`` → Proprietor, and in addresses ``NO 12`` → Number 12, ``H NO`` →
  House Number, ``G/F`` → Ground Floor, ``BLDG`` → Building, ``RD`` → Road,
  ``OPP`` → Opposite, ``NR`` → Near, ``FLR`` → Floor, ``SOC`` → Society,
  ``LM`` → landmark;
* models: hyphens become spaces, ``+`` becomes "plus", ``WIFI`` → WiFi,
  ``Hand held`` → Handheld;
* addresses: line breaks become ", " (or a space when the next line clearly
  continues the previous one: "…Sarojini Devi / Road Secunderabad"), the
  export's metadata lines (``New ME Contact No.…``, ``Terminal Line No.…``)
  and ``Merchant Pin Code`` / ``Merchant State`` labels are dropped — their
  values are kept once, as "…, State 400010" — and segments repeated by the
  export ("Indore MP / Indore - 452016, Madhya Pradesh", "Wardha 442001 /
  Wardha - 442001") are said once.  A trailing line that only repeats the
  contact person's name is dropped.  Pin codes stay plain six-digit numbers
  (the TTS reads them digit by digit: "four five two zero one six").

Nothing here invents data: words are re-cased, expanded or de-duplicated,
never added (except the label expansions above), and a truncated export
value ("GYNAECOLOG") is spoken as stored.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Capitalised tokens that are spelt out even though they contain a vowel.
ACRONYMS = frozenset({
    # banks / institutions seen in addresses and names
    "IDBI", "SBI", "BOI", "BOB", "UBI", "CBI", "CUB", "TMB", "IOB", "UCO", "ICICI",
    "IDFC", "HDFC", "AU", "RBI", "LIC", "ONGC", "IOC", "BPCL", "HPCL", "AIIMS", "IIT",
    "ITI", "ESI", "GPO", "MIDC", "CIDCO", "DLF", "NDMC", "BMC",
    # organisations in the Wordline data (AFFWA L HQ WAC U AF, WYT Hospitality)
    "AFFWA", "WAC", "HQ", "AF", "WYT",
    # business forms and technical terms
    "LLP", "LLC", "CHS", "GPRS", "GSM", "POS", "EDC", "ATM", "MID", "TID", "GST",
    "UPI", "NFC", "QR", "USB", "AC", "IT", "UK", "USA", "OK",
})
# Indian states / UTs: duplicate detection and "…, State 400010" ordering.
STATES = (
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa",
    "Gujarat", "Gujrat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram",
    "Nagaland", "Odisha", "Orissa", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Andaman and Nicobar Islands", "Chandigarh", "Delhi", "New Delhi",
    "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry", "Pondicherry",
    "Dadra and Nagar Haveli", "Daman and Diu",
)
# A state abbreviation is dropped when the full name is in the same address.
STATE_ABBREVIATIONS = {
    "MP": "Madhya Pradesh", "UP": "Uttar Pradesh", "MH": "Maharashtra",
    "TN": "Tamil Nadu", "AP": "Andhra Pradesh", "KA": "Karnataka", "GJ": "Gujarat",
    "RJ": "Rajasthan", "WB": "West Bengal", "HR": "Haryana", "PB": "Punjab",
    "KL": "Kerala", "TS": "Telangana", "UK": "Uttarakhand", "JK": "Jammu and Kashmir",
}
SMALL_WORDS = frozenset({"and", "of", "for", "at", "in", "on", "by", "to"})
# Whole-token expansions (matched case-insensitively on ALL-CAPS or Title tokens).
NAME_EXPANSIONS = {"PVT": "Private", "LTD": "Limited", "PROP": "Proprietor", "DR": "Dr"}
ADDRESS_EXPANSIONS = {
    **NAME_EXPANSIONS, "BLDG": "Building", "RD": "Road", "OPP": "Opposite", "NR": "Near",
    "FLR": "Floor", "SOC": "Society", "LM": "landmark",
}
# A next line starting with one of these continues the previous line's name.
_CONTINUATION_WORDS = frozenset({
    "road", "rd", "street", "st", "marg", "lane", "nagar", "colony", "complex", "hospital",
    "plaza", "park", "building", "bldg", "tower", "market", "bazar", "bazaar", "chowk",
    "society", "mall", "centre", "center", "hall", "no", "number", "cross", "main",
})

_VOWEL = re.compile(r"[AEIOUY]")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’]*")
_ORDINAL = re.compile(r"^(\d+)(ST|ND|RD|TH)$")
_PIN = re.compile(r"(?<!\d)([1-9]\d{5})(?!\d)")
_INITIAL_DOT = re.compile(r"\b([A-Za-z])\.(?=\s|$)")
_SPACES = re.compile(r"\s+")


def _is_caps(token: str) -> bool:
    letters = [c for c in token if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def _case_token(token: str, *, first: bool, expansions: dict[str, str]) -> str:
    upper = token.upper()
    if not _is_caps(token):
        # Already natural case ("Kunal", "shop", "Wi-Fi") — only expansions apply.
        expanded = expansions.get(upper) if token[:1].isupper() and upper in expansions else None
        return expanded or token
    if upper in expansions:
        word = expansions[upper]
        return word[:1].upper() + word[1:] if first else word
    if any(c.isdigit() for c in token):
        ordinal = _ORDINAL.match(token)
        return f"{ordinal.group(1)}{ordinal.group(2).lower()}" if ordinal else token
    if len(token) == 1 or upper in ACRONYMS or upper in STATE_ABBREVIATIONS:
        return token
    if not _VOWEL.search(upper):
        return token  # PNB, SBS, NSK, MMTC — only ever spelt
    if upper == "WIFI":
        return "WiFi"
    lower = token.lower()
    if lower in SMALL_WORDS and not first:
        return lower
    return lower[:1].upper() + lower[1:]


def _recase(text: str, expansions: dict[str, str]) -> str:
    position = 0

    def _sub(match: re.Match) -> str:
        nonlocal position
        out = _case_token(match.group(0), first=position == 0, expansions=expansions)
        position += 1
        return out

    return _TOKEN.sub(_sub, text)


def _squash(text: str) -> str:
    text = _SPACES.sub(" ", text).strip()
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r",(?=\S)", ", ", text)
    text = re.sub(r"(,\s*){2,}", ", ", text)
    return text.strip(" ,;")


# ── names: MERCHANT and Contact Person ─────────────────────────────────────


@lru_cache(maxsize=1024)
def spoken_name(raw: str | None) -> str:
    """``AGARWAL PUBLIC SCHOOL`` → ``Agarwal Public School``; ``K C TIWARI`` →
    ``K C Tiwari``; ``TRISTAR CARS PVT LTD`` → ``Tristar Cars Private Limited``."""
    if not raw:
        return ""
    text = _SPACES.sub(" ", str(raw)).strip()
    text = text.replace("&", " and ")
    text = _INITIAL_DOT.sub(r"\1", text)  # "Prasanthan K. C" → "Prasanthan K C"
    return _squash(_recase(text, NAME_EXPANSIONS))


# ── models: ASSETMODEL ───────────────────────────────────────────────────────


@lru_cache(maxsize=256)
def spoken_model(raw: str | None) -> str:
    """``PAYTIVO-7210-2G+WIFI`` → ``Paytivo 7210 2G plus WiFi``;
    ``ANTERA AL9220U-Hand held GPRS`` → ``Antera AL9220U Handheld GPRS``."""
    if not raw:
        return ""
    text = str(raw).replace("+", " plus ").replace("_", " ")
    text = re.sub(r"\s*-\s*", " ", text)
    text = re.sub(r"\bhand\s+held\b", "Handheld", text, flags=re.I)
    text = re.sub(r"\bwi\s*fi\b", "WiFi", text, flags=re.I)
    return _squash(_recase(text, NAME_EXPANSIONS))


# ── addresses ────────────────────────────────────────────────────────────────

_METADATA_LINE = re.compile(r"^\s*(?:new\s+me\s+contact\s+no|terminal\s+line\s+no)\b.*$", re.I | re.M)
_PIN_LABEL = re.compile(r"merchant\s+pin\s*code\s*[-:]?\s*([1-9]\d{5})?", re.I)
_STATE_LABEL = re.compile(r"merchant\s+state\s*[-:]?\s*([A-Za-z][A-Za-z .]*?)?\s*(?=\n|$|merchant\b)", re.I)
_HOUSE_NO = re.compile(r"\bH(?:OUSE)?\.?\s*NO\b\.?\s*(?=[A-Za-z]?\d)", re.I)
_NUMBER_NO = re.compile(r"\bNO\b\.?\s*(?=[A-Za-z]?\d)", re.I)
# "SHOP NO AVON PLAZA" (number missing in the export) is still "Shop Number".
_UNIT_NO = re.compile(r"\b(SHOP|FLAT|PLOT|DOOR|HOUSE|BUILDING|WARD|ROOM|UNIT|OFFICE|GALA|BLOCK)\s+NO\b\.?", re.I)
_GROUND_FLOOR = re.compile(r"\bG\s*/\s*F\b", re.I)
_CITY_PIN_HYPHEN = re.compile(r"\s*-\s*(?=[1-9]\d{5}(?!\d))")
_STATE_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in sorted(STATES, key=len, reverse=True)) + r")\b", re.I)


def _key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _join_lines(lines: list[str]) -> str:
    out = ""
    for line in lines:
        if not out:
            out = line
            continue
        first = (re.match(r"[A-Za-z]+", line) or [""])[0].lower()
        prev_tail = out.rstrip()
        # "…680 / Dr. Mascarenhas Road" and "…Sarojini Devi / Road Secunderabad"
        # are one phrase; a line ending in a pin code ends a phrase.
        continues = (
            first in _CONTINUATION_WORDS
            or prev_tail.endswith(",")
            or re.search(r"(?:(?<!\d)\d{1,4}|\b(?:no|near|opp|of|and|at|dr)\.?)$", prev_tail, re.I)
        )
        out = f"{prev_tail} {line}" if continues and not prev_tail.endswith(",") else f"{prev_tail.rstrip(',')}, {line}"
    return out


def _dedupe_segments(segments: list[str]) -> list[str]:
    kept: list[str] = []
    for seg in segments:
        key = _key(seg)
        if not key:
            continue
        if kept:
            prev = kept[-1]
            prev_key = _key(prev)
            core = _key(_PIN.sub("", seg))
            pin = _PIN.search(seg)
            if key == prev_key or prev_key.endswith(" " + key) or prev_key == key:
                continue                                  # "…Wardha 442001" then "Wardha 442001"
            if key.startswith(prev_key + " "):
                kept[-1] = seg                            # "Indore" then "Indore 452016"
                continue
            if pin and core and (prev_key == core or prev_key.endswith(" " + core)) \
                    and not _PIN.search(prev):
                kept[-1] = f"{prev} {pin.group(1)}"       # "…Majestic Bangalore" then "Majestic Bangalore 560009"
                continue
        if any(_key(k) == key for k in kept):
            continue                                      # "New Delhi, New Delhi"
        kept.append(seg)
    return kept


def _dedupe_words(text: str) -> str:
    # "Ahmedabad Ahmedabad 380052" → "Ahmedabad 380052" (alphabetic words only).
    return re.sub(r"\b([A-Za-z]{3,})(\s+\1\b)+", r"\1", text, flags=re.I)


@lru_cache(maxsize=1024)
def spoken_address(raw: str | None, contact_person: str | None = None) -> str:
    """Multi-line export address → one TTS-friendly line.

    ``SHOP NO 17, SAIFEE PARK, 680\\nDR. MASCARENHAS ROAD, MAZGAON , MUMBAI\\n
    Merchant Pin Code\\n400010\\nMerchant State\\nMAHARASHTRA`` →
    ``Shop Number 17, Saifee Park, 680 Dr. Mascarenhas Road, Mazgaon, Mumbai,
    Maharashtra 400010``.
    """
    if not raw:
        return ""
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    text = _METADATA_LINE.sub("", text)
    pins = [m.group(1) for m in _PIN_LABEL.finditer(text) if m.group(1)]
    states = [m.group(1).strip() for m in _STATE_LABEL.finditer(text) if m.group(1)]
    text = _STATE_LABEL.sub("\n", _PIN_LABEL.sub("\n", text))
    lines = [_SPACES.sub(" ", ln).strip(" ,") for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    person = _key(contact_person or "")
    if person and len(lines) > 1 and _key(lines[-1]) == person:
        lines.pop()                       # a trailing line repeating the contact person
    text = _join_lines(lines)
    text = _GROUND_FLOOR.sub("Ground Floor", text)
    text = _HOUSE_NO.sub("House Number ", text)
    text = _UNIT_NO.sub(lambda m: f"{m.group(1)} {'Number' if m.group(0)[-2:].isupper() else 'number'}", text)
    text = _NUMBER_NO.sub(lambda m: "Number " if m.group(0)[:1].isupper() else "number ", text)
    text = _CITY_PIN_HYPHEN.sub(" ", text)
    text = re.sub(r"(?<=[A-Za-z])\s*-\s*(?=[A-Za-z])", " ", text)
    text = _recase(text, ADDRESS_EXPANSIONS)
    segments = _dedupe_segments([s.strip() for s in text.split(",")])
    text = _dedupe_words(", ".join(segments))
    lowered = text.lower()
    for abbr, full in STATE_ABBREVIATIONS.items():
        if full.lower() in lowered or any(s.lower() == full.lower() for s in states):
            text = re.sub(rf"\b{abbr}\b", "", text)
    text = _squash(text)
    segments = _dedupe_segments([s.strip() for s in text.split(",")])
    text = ", ".join(segments)
    # Label values are spoken once, as "…, State 400010".
    state = next((s for s in states if s), "")
    if state and _key(state) not in _key(text):
        text = f"{text}, {spoken_name(state)}"
    pin = next((p for p in pins if p not in text), "")
    if pin:
        text = f"{text} {pin}"
    # "Madurai 625020 Tamil Nadu" → "Madurai, Tamil Nadu 625020" (pin last).
    text = re.sub(rf"(?<!\d)([1-9]\d{{5}}),?\s+{_STATE_RE.pattern}\s*$",
                  lambda m: f", {m.group(2)} {m.group(1)}", text, flags=re.I)
    return _squash(text)
