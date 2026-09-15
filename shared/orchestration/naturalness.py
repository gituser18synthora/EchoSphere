"""Human speech naturalness planning (the "how to say it" layer).

The LLM / goal engine decides *what* the bot says; this module decides *how*
it is delivered: occasional thinking fillers and acknowledgements, contextual
tool-lookup prefaces, sparse mid-caller-speech backchannels, per-sentence
pause/rate variation, and (optional, off by default) rare self-corrections.

Design constraints, in priority order:

* **Deterministic and cheap** — pure config + RNG, no model calls, so it can
  run inside the first-audio critical path.  A planner call is microseconds.
* **Contextual and probabilistic** — nothing is injected on every turn, and
  serious caller signals (complaint/hardship/…) suppress playful hesitation.
* **Critical-content safe** — segments carrying amounts, dates, identifiers,
  OTPs or compliance wording never receive fillers, corrections or ambiguous
  pacing (`contains_critical_content`).
* **Language + gender aware** — variant pools exist per base language; Hindi
  entries are authored in masculine first-person form and re-agreed through
  ``adapt_authored_speaker_grammar`` using the *active* catalog voice, so a
  bot whose fallback voice differs in gender stays grammatical.  Languages
  without a pool simply get no fillers (never cross-language fillers).

Configuration resolves platform defaults -> tenant override -> bot override
(``resolve_human_speech``); the merged dict rides ResolvedBotConfig.
"""

from __future__ import annotations

import random
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field

from .voice_identity import VoiceIdentity, adapt_authored_speaker_grammar

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Pre-rendered filler sound kinds (voice_runtime.latency_filler). The
# pre-reply gap sound and the in-reply sentence breath draw from one clip
# library keyed by kind and by the active voice's catalog gender.
FILLER_SOUND_KINDS = ("breath", "inhale", "exhale", "inhale_exhale")
FILLER_SOUND_LABELS = {
    "breath": "Soft breath",
    "inhale": "Short inhale",
    "exhale": "Short exhale",
    "inhale_exhale": "Inhale-exhale",
}
FILLER_GENDERS = ("male", "female", "neutral")

HUMAN_SPEECH_DEFAULTS: dict = {
    # Feature switches
    "enabled": True,
    "thinking_fillers": True,
    "acknowledgements": True,
    "backchannels": True,
    "prosody_variation": True,
    "gender_agreement": True,
    "micro_pauses": True,
    "self_correction": False,
    # Latency fillers: a short, voice-gender-matched breath played from
    # pre-rendered audio when the reply has not started speaking
    # ``latency_filler_delay_ms`` after the caller stopped talking, cut the
    # instant real reply audio arrives (voice_runtime.latency_filler).
    "latency_fillers": True,
    # Sentence breaths: in pause mode, a rare soft breath before a long or
    # verification sentence INSIDE a reply — never after every sentence.
    "sentence_breaths": True,
    # Tunables (probabilities are per-opportunity, 0..1)
    "thinking_filler_probability": 0.25,
    # Acknowledgement ("जी…", "ठीक है…", "Hmm…") eligible at the latency
    # deadline; a hard no-consecutive-turns rule sits on top.
    "acknowledgement_probability": 0.5,
    "tool_ack_probability": 0.9,
    "backchannel_probability": 0.35,
    "micro_pause_probability": 0.45,
    "self_correction_probability": 0.01,
    # Per eligible sentence; with the ≤1-per-reply cap this lands a breath in
    # roughly every third reply that has a long sentence (live tuning
    # 2026-09-03: 0.2 gave one or two per call, too sparse to register).
    "sentence_breath_probability": 0.35,
    "min_long_turn_for_backchannel_ms": 4000,
    "min_gap_between_backchannels_ms": 8000,
    "max_backchannels_per_call": 4,
    "latency_filler_delay_ms": 1500,
    # Escalation ladder for LONG waits: when the breath has played and the
    # reply still has not started, a short voiced cue in the bot's own voice
    # ("Hmm…") follows at ``latency_filler_hmm_ms`` after the caller stopped,
    # and a spoken "एक सेकंड…" at ``latency_filler_spoken_ms``. Cues are
    # rendered once per voice and cached (voice_runtime.voiced_cues); the
    # spoken rung is withheld on critical/serious turns.
    "latency_filler_ladder": True,
    # Opt-in per bot: choose one initial sound at a contextual 1.5–2.5 s
    # deadline instead of requiring breath -> quiet gap -> voiced cue.
    "adaptive_latency_cues": False,
    "latency_filler_hmm_ms": 3500,
    "latency_filler_spoken_ms": 5000,
    # Which pre-rendered sound covers the pre-reply gap (the ladder's first
    # rung): one of FILLER_SOUND_KINDS. The in-reply sentence breath always
    # uses the short inhale.
    "latency_filler_kind": "breath",
    # Which clips of the library the runtime may play, per kind and per voice
    # gender: {kind: {gender: {"primary": clip_id, "alternates": [clip_id…]}}}.
    # The primary plays first in a call, then the alternates rotate with it.
    # Empty → every available clip of the voice's gender rotates (the
    # pre-selection behaviour).
    "filler_audio_selection": {},
    # Which voiced cue texts the ladder's "hmm" rung may use, per language:
    # {lang: {"primary": cue_id, "alternates": [cue_id…]}} (see
    # ladder_cue_options). Empty → the language's default cue and its first
    # alternate rotate.
    "latency_filler_cue_selection": {},
    # Chance that a LONG wait gets a voiced cue at all (per opportunity) once
    # the breath has played; below it the wait stays a breath — a person does
    # not put a word into every silence.
    "latency_cue_probability": 0.7,
}

_BOOL_KEYS = (
    "enabled", "thinking_fillers", "acknowledgements", "backchannels",
    "prosody_variation", "gender_agreement", "micro_pauses", "self_correction",
    "latency_fillers", "sentence_breaths", "latency_filler_ladder", "adaptive_latency_cues",
)
_PROBABILITY_KEYS = (
    "thinking_filler_probability", "acknowledgement_probability",
    "tool_ack_probability", "backchannel_probability",
    "micro_pause_probability", "self_correction_probability",
    "sentence_breath_probability", "latency_cue_probability",
)
_INT_KEYS = {
    "min_long_turn_for_backchannel_ms": (1000, 60_000),
    "min_gap_between_backchannels_ms": (2000, 120_000),
    "max_backchannels_per_call": (0, 20),
    # Quiet time after the caller stops before a filler may play. Below 500 ms
    # it would fire on ordinary fast turns; above 5 s it never masks anything.
    "latency_filler_delay_ms": (500, 5000),
    # Ladder rungs, measured from the caller's end of speech like the delay.
    "latency_filler_hmm_ms": (2000, 8000),
    "latency_filler_spoken_ms": (3000, 12000),
}
_CHOICE_KEYS = {"latency_filler_kind": FILLER_SOUND_KINDS}
# Nested selection maps: {name: … {name: {"primary", "alternates"}}} with the
# given number of string-key levels above the choice.
_SELECTION_KEYS = {"filler_audio_selection": 2, "latency_filler_cue_selection": 1}
_SELECTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:,()\-]{0,120}$")
_MAX_ALTERNATES = 8


def _normalize_choice(value: object) -> dict | None:
    """``{"primary": id, "alternates": [ids]}`` cleaned (primary never
    repeated among the alternates, duplicates dropped, capped); ``{}`` for an
    empty choice; None when malformed."""
    if not isinstance(value, dict):
        return None
    primary = value.get("primary")
    if primary == "":
        primary = None
    if primary is not None and (
        not isinstance(primary, str) or not _SELECTION_ID_RE.match(primary)
    ):
        return None
    alternates = value.get("alternates")
    if alternates is None:
        alternates = []
    if not isinstance(alternates, list) or any(
        not isinstance(item, str) or not _SELECTION_ID_RE.match(item) for item in alternates
    ):
        return None
    cleaned: list[str] = []
    for item in alternates:
        if item != primary and item not in cleaned:
            cleaned.append(item)
    if primary is None and not cleaned:
        return {}
    out: dict = {"alternates": cleaned[:_MAX_ALTERNATES]}
    if primary is not None:
        out["primary"] = primary
    return out


def normalize_selection(value: object, *, depth: int) -> dict | None:
    """Clean a nested selection map (``depth`` string-key levels above each
    choice). Empty choices are dropped so ``{}`` means "no selection".
    Returns None when the shape is not a selection at all."""
    if not isinstance(value, dict):
        return None
    out: dict = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 40:
            return None
        cleaned = (
            normalize_selection(item, depth=depth - 1) if depth > 1
            else _normalize_choice(item)
        )
        if cleaned is None:
            return None
        if cleaned:
            out[key] = cleaned
    return out


def selection_choice(selection: object, *path: str) -> dict | None:
    """The ``{"primary", "alternates"}`` choice at ``path`` inside a cleaned
    selection map, or None when nothing is selected there."""
    node: object = selection
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if isinstance(node, dict) and (node.get("primary") or node.get("alternates")):
        return node
    return None


def selection_ids(choice: dict | None) -> list[str]:
    """Primary first, then alternates — the runtime's rotation order."""
    if not choice:
        return []
    ids: list[str] = []
    primary = choice.get("primary")
    if primary:
        ids.append(str(primary))
    for item in choice.get("alternates") or []:
        if item and item not in ids:
            ids.append(str(item))
    return ids


# The first caller reply after the greeting carries the call's highest
# response latency (cold decision/LLM/knowledge paths, first prompt compile):
# a spoken "Ji…/Okay…" masks more dead air there than on any later turn, so
# its preface odds are raised by this factor (capped at certainty).
_FIRST_REPLY_PREFACE_BOOST = 1.5

# A reply sentence this long (or a critical one) may be preceded by one soft
# breath in pause mode — the beat a person takes before a longer explanation
# or a verification read-back.
_LONG_SENTENCE_WORDS = 10


def validate_human_speech(value: object) -> list[str]:
    """Strict validation for API-saved overrides (runtime merging is lenient).

    Returns a list of problems; empty means valid. Overrides are sparse —
    only overridden keys need to be present.
    """
    problems: list[str] = []
    if not isinstance(value, dict):
        return ["human_speech must be an object"]
    for key, item in value.items():
        if key in _BOOL_KEYS:
            if not isinstance(item, bool):
                problems.append(f"'{key}' must be a boolean")
        elif key in _PROBABILITY_KEYS:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                problems.append(f"'{key}' must be a number between 0 and 1")
            elif not 0.0 <= float(item) <= 1.0:
                problems.append(f"'{key}' must be between 0 and 1")
        elif key in _INT_KEYS:
            low, high = _INT_KEYS[key]
            if isinstance(item, bool) or not isinstance(item, int):
                problems.append(f"'{key}' must be an integer")
            elif not low <= item <= high:
                problems.append(f"'{key}' must be between {low} and {high}")
        elif key in _CHOICE_KEYS:
            if not isinstance(item, str) or item not in _CHOICE_KEYS[key]:
                problems.append(
                    f"'{key}' must be one of {', '.join(_CHOICE_KEYS[key])}"
                )
        elif key in _SELECTION_KEYS:
            cleaned = normalize_selection(item, depth=_SELECTION_KEYS[key])
            if cleaned is None:
                problems.append(
                    f"'{key}' must map names to {{primary, alternates}} choices"
                )
            elif key == "filler_audio_selection":
                for kind, genders in cleaned.items():
                    if kind not in FILLER_SOUND_KINDS:
                        problems.append(f"'{key}': unknown sound kind '{kind}'")
                        continue
                    for gender in genders:
                        if gender not in FILLER_GENDERS:
                            problems.append(f"'{key}': unknown gender '{gender}'")
        else:
            problems.append(f"unknown key '{key}'")
    return problems


def resolve_human_speech(*layers: dict | None) -> dict:
    """Merge human-speech config layers (platform -> tenant -> bot).

    Later layers win per key.  Unknown keys are dropped and every value is
    clamped/coerced so a junk override can never break a live call.
    """
    merged = dict(HUMAN_SPEECH_DEFAULTS)
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for key, value in layer.items():
            if key in _BOOL_KEYS:
                if isinstance(value, bool):
                    merged[key] = value
            elif key in _PROBABILITY_KEYS:
                if isinstance(value, bool):
                    continue
                try:
                    merged[key] = min(1.0, max(0.0, float(value)))
                except (TypeError, ValueError):
                    pass
            elif key in _INT_KEYS:
                if isinstance(value, bool):
                    continue
                low, high = _INT_KEYS[key]
                try:
                    merged[key] = min(high, max(low, int(value)))
                except (TypeError, ValueError):
                    pass
            elif key in _CHOICE_KEYS:
                if isinstance(value, str) and value in _CHOICE_KEYS[key]:
                    merged[key] = value
            elif key in _SELECTION_KEYS:
                cleaned = normalize_selection(value, depth=_SELECTION_KEYS[key])
                if cleaned is not None:
                    merged[key] = cleaned
    return merged


def resolve_human_speech_with_sources(
    tenant: dict | None = None,
    bot: dict | None = None,
) -> tuple[dict, dict[str, str]]:
    """Resolve platform -> tenant -> bot and report each effective source.

    The source map contains configuration provenance only; it never carries a
    tenant id, bot id, spoken text or any other customer data. Invalid stored
    values do not claim provenance because the runtime ignores them too.
    """
    effective = dict(HUMAN_SPEECH_DEFAULTS)
    sources = {key: "platform" for key in HUMAN_SPEECH_DEFAULTS}
    for level, layer in (("tenant", tenant), ("bot", bot)):
        if not isinstance(layer, dict):
            continue
        effective = resolve_human_speech(effective, layer)
        for key in HUMAN_SPEECH_DEFAULTS:
            if key in layer:
                # Validation on writes keeps these values well-typed. The
                # explicit type checks below also keep legacy junk from being
                # presented as an effective override source.
                value = layer[key]
                valid = (
                    (key in _BOOL_KEYS and isinstance(value, bool))
                    or (
                        key in _PROBABILITY_KEYS
                        and not isinstance(value, bool)
                        and isinstance(value, (int, float))
                    )
                    or (
                        key in _INT_KEYS
                        and not isinstance(value, bool)
                        and isinstance(value, int)
                    )
                    or (
                        key in _CHOICE_KEYS
                        and isinstance(value, str)
                        and value in _CHOICE_KEYS[key]
                    )
                    or (
                        key in _SELECTION_KEYS
                        and normalize_selection(value, depth=_SELECTION_KEYS[key]) is not None
                    )
                )
                if valid:
                    sources[key] = level
    return effective, sources


# --------------------------------------------------------------------------
# Critical-content detection
# --------------------------------------------------------------------------

# Naturalness must never blur amounts, dates, identifiers, verification codes
# or compliance wording.  False positives are safe (a segment just loses its
# decoration), so these patterns are deliberately broad.
_CRITICAL_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\d{3,}"),                                  # amounts / ids / phones
    re.compile(r"\d+[.,]\d+"),                              # decimals / groupings
    re.compile(r"[₹$€£]"),
    re.compile(r"(?<!\w)(?:rs\.?|rupees?|rupay?e|inr|usd|eur)(?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)(?:otp|pin|cvv)(?!\w)", re.IGNORECASE),
    re.compile(
        r"(?<!\w)(?:transaction|txn|utr|reference|ref\.?|account|a/c|khata|"
        r"verification|code|id)(?!\w)",
        re.IGNORECASE,
    ),
    # Dates: 12/08, 12-08-2026, "25 tareekh", month names (en + romanized hi)
    re.compile(r"\d{1,2}\s*[./-]\s*\d{1,2}"),
    re.compile(r"\d{4}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2}"),
    re.compile(r"\d{1,2}\s*(?:tareekh|taareekh|taarikh|तारीख)", re.IGNORECASE),
    re.compile(
        r"(?<!\w)(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?|janvari|farvari|अगस्त|जनवरी|फरवरी|मार्च|अप्रैल|मई|जून|"
        r"जुलाई|सितंबर|अक्टूबर|नवंबर|दिसंबर)(?!\w)",
        re.IGNORECASE,
    ),
    # Weekday commitments and relative commitment dates. These are critical
    # when spoken because a missed day can change a repayment promise.
    re.compile(
        r"(?<!\w)(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
        r"fri(?:day)?|sat(?:urday)?|sun(?:day)?|somvaar|mangalvaar|budhvaar|"
        r"guruvaar|shukravaar|shanivaar|ravivaar|सोमवार|मंगलवार|बुधवार|"
        r"गुरुवार|शुक्रवार|शनिवार|रविवार)(?!\w)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)(?:today|tomorrow|day after tomorrow|aaj|kal|parso|आज|कल|"
        r"परसों)(?!\w).{0,32}(?:pay|payment|repay|bhugtan|भुगतान|भर|करूँ|करू)",
        re.IGNORECASE,
    ),
    # Devanagari digits
    re.compile(r"[०-९]"),
    # Verbalized amounts: LLM replies speak numbers as words ("पच्चीस हज़ार
    # रुपये", "two thousand rupees") — digits alone would miss them.
    re.compile(
        r"(?<!\w)(?:हज़ार|हजार|लाख|करोड़|सौ|hazaa?r|laakh|lakh|crore|"
        r"thousand|hundred|million)(?!\w)",
        re.IGNORECASE,
    ),
    # Compliance / consent wording
    re.compile(
        r"(?<!\w)(?:consent|recorded|recording|legal|notice|compliance|waiver|"
        r"terms and conditions|privacy|authorization|authorisation|disclosure|"
        r"sahmati|kanooni|कानूनी|सहमति|रिकॉर्ड|अनुमति)(?!\w)",
        re.IGNORECASE,
    ),
    # Repayment promises and payment commitments, including common Hinglish.
    re.compile(
        r"(?<!\w)(?:promise to pay|payment commitment|repayment commitment|"
        r"repay(?:ment)?|will pay|can pay|pay by|payment by|payment on|"
        r"karunga|karungi|dunga|dungi|bhar dunga|bhar dungi|करूंगा|करूँगा|"
        r"करूंगी|करूँगी|दूंगा|दूँगा|दूंगी|दूँगी|भर दूंगा|भर दूंगी)(?!\w)",
        re.IGNORECASE,
    ),
    # Contact details and physical addresses. False positives deliberately
    # prefer clear delivery over decorating an address-like sentence.
    re.compile(
        r"(?<!\w)(?:phone|mobile|telephone|contact number|address|street|road|"
        r"lane|avenue|apartment|flat|house number|building|sector|pincode|"
        r"postal code|पता|मोबाइल|फ़ोन|फोन|सड़क|मकान|पिनकोड)(?!\w)",
        re.IGNORECASE,
    ),
    # Identity verification and consent gates are structured-critical at the
    # turn layer too; these words are the segment-level safety net.
    re.compile(
        r"(?<!\w)(?:identity verification|verify your identity|date of birth|"
        r"dob|customer verification|पहचान|जन्म तिथि|सत्यापन)(?!\w)",
        re.IGNORECASE,
    ),
)


def contains_critical_content(text: str) -> bool:
    """True when ``text`` carries content whose clarity must not be reduced."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _CRITICAL_PATTERNS)


# --------------------------------------------------------------------------
# Variant pools
# --------------------------------------------------------------------------

# Hindi entries are authored in masculine first-person form; feminine forms
# are derived at selection time via adapt_authored_speaker_grammar so pools
# stay in lock-step with the catalog-driven identity logic.  Entries whose
# male/female adaptations differ are skipped for neutral-gender voices.
#
# Hinglish is Hindi here (same convention as transcript_gate / phrases.py).
_POOLS: dict[str, dict[str, tuple[str, ...]]] = {
    "hi": {
        # NOTE: "Ek minute..." deliberately absent here — announcing a wait
        # and then not looking anything up sounds broken; it lives in the
        # "checking" pool where a lookup actually follows.
        "thinking": ("Hmm...", "Achha...", "Dekhiye...", "Haan..."),
        "acknowledgement": ("Achha...", "Ji...", "Theek hai...", "Haan ji...",
                            "Ji, main samajh raha hoon...", "Samajh gaya...",
                            "Theek hai, samajh gaya.", "Koi baat nahi..."),
        # Dispatch-planned latency acknowledgements: one short token,
        # chosen by what the caller just did. "हाँ…" is deliberately absent —
        # after a statement it reads as agreement, not as listening.
        "ack_answer": ("जी…", "ठीक है…", "अच्छा…", "अच्छा, ठीक है…", "जी, ठीक है…"),
        # The thinking filler is "Hmm…" in Latin script for every language
        # (operator decision 2026-09-14): TTS renders it as the same neutral
        # hum, while "हम्म…" was read as a word.
        "ack_question": ("Hmm…", "जी…", "अच्छा…"),
        "ack_lookup": ("एक सेकंड…", "जी, एक सेकंड…", "Hmm… देख रहा हूँ…",
                       "एक सेकंड, देख रहा हूँ…"),
        "ack_neutral": ("जी…", "Hmm…"),
        "checking": (
            "Ek minute, main check karta hoon...",
            "Achha... ek minute, main check karta hoon.",
            "Ji, ek second... main dekh raha hoon.",
            "Ek minute, main abhi dekhta hoon...",
            "जी... एक मिनट दीजिए, मैं details देख रहा हूँ।",
        ),
        "critical_checking": (
            "Ek minute, main verify karta hoon.",
            "Ji, main abhi check karta hoon.",
        ),
        "empathy": (
            "Ji, main samajh sakta hoon.",
            "Hmm... ji, main samajh raha hoon.",
            "Ji...",
            "Main aapki baat samajh raha hoon.",
            "Ji, aapki situation samajh sakta hoon.",
        ),
        "backchannel": ("hmm...", "ji...", "achha...", "haan..."),
        "correction_token": ("sorry", "maaf kijiye"),
    },
    "en": {
        "thinking": ("Hmm...", "Okay...", "Right...", "Let me see..."),
        "acknowledgement": ("Okay...", "Right...", "I see...", "Got it..."),
        "ack_answer": ("Okay…", "Right…", "Got it…", "Alright…"),
        "ack_question": ("Hmm…", "Right…", "Let me see…"),
        "ack_lookup": ("One second…", "Let me check…", "Hmm… one moment…"),
        "ack_neutral": ("Okay…", "Hmm…"),
        "checking": (
            "One moment, let me check...",
            "Okay... give me a second, let me look that up.",
            "Right, let me check that for you...",
        ),
        "critical_checking": (
            "One moment, let me verify that.",
            "I will check that now.",
        ),
        "empathy": ("I understand.", "I hear you...", "Okay, I understand...",
                    "I do understand your situation."),
        "backchannel": ("hmm...", "right...", "okay..."),
        "correction_token": ("sorry",),
    },
    "gu": {
        "thinking": ("હમ્મ...", "બરાબર...", "જોઈએ..."),
        "acknowledgement": ("બરાબર...", "જી...", "સમજાયું..."),
        "checking": ("એક ક્ષણ, હું તપાસું છું...", "જી, હમણાં તપાસું છું..."),
        "critical_checking": ("એક ક્ષણ, હું ચકાસું છું.",),
        "empathy": ("જી, તમારી વાત સમજાઈ.", "તમારી પરિસ્થિતિ સમજાઈ."),
        "backchannel": ("હમ્મ...", "જી...", "બરાબર..."),
        "correction_token": ("માફ કરશો",),
    },
    "ml": {
        "thinking": ("ഹും...", "ശരി...", "നോക്കാം..."),
        "acknowledgement": ("ശരി...", "മനസ്സിലായി...", "അതെ..."),
        "checking": ("ഒരു നിമിഷം, ഞാൻ പരിശോധിക്കാം...", "ശരി, ഇപ്പോൾ നോക്കാം..."),
        "critical_checking": ("ഒരു നിമിഷം, ഞാൻ പരിശോധിക്കാം.",),
        "empathy": ("നിങ്ങളുടെ സാഹചര്യം മനസ്സിലായി.", "ശരി, മനസ്സിലായി."),
        "backchannel": ("ഹും...", "ശരി...", "അതെ..."),
        "correction_token": ("ക്ഷമിക്കണം",),
    },
    "mr": {
        "thinking": ("हं...", "ठीक आहे...", "पाहूया..."),
        "acknowledgement": ("ठीक आहे...", "समजलं...", "हो..."),
        "checking": ("एक क्षण, तपासून पाहूया...", "ठीक आहे, आत्ता पाहूया..."),
        "critical_checking": ("एक क्षण, तपासून पाहूया.",),
        "empathy": ("तुमचं म्हणणं समजलं.", "हो, समजलं."),
        "backchannel": ("हं...", "हो...", "ठीक आहे..."),
        "correction_token": ("माफ करा",),
    },
    "pa": {
        "thinking": ("ਹੂੰ...", "ਠੀਕ ਹੈ...", "ਦੇਖਦੇ ਹਾਂ..."),
        "acknowledgement": ("ਠੀਕ ਹੈ...", "ਸਮਝ ਆਇਆ...", "ਜੀ..."),
        "checking": ("ਇੱਕ ਪਲ, ਜਾਂਚ ਕਰਦੇ ਹਾਂ...", "ਠੀਕ ਹੈ, ਹੁਣੇ ਦੇਖਦੇ ਹਾਂ..."),
        "critical_checking": ("ਇੱਕ ਪਲ, ਜਾਂਚ ਕਰਦੇ ਹਾਂ.",),
        "empathy": ("ਜੀ, ਤੁਹਾਡੀ ਗੱਲ ਸਮਝ ਆਈ.", "ਤੁਹਾਡੀ ਸਥਿਤੀ ਸਮਝ ਆਈ."),
        "backchannel": ("ਹੂੰ...", "ਜੀ...", "ਠੀਕ ਹੈ..."),
        "correction_token": ("ਮਾਫ਼ ਕਰਨਾ",),
    },
    "ta": {
        "thinking": ("ம்...", "சரி...", "பார்க்கலாம்..."),
        "acknowledgement": ("சரி...", "புரிகிறது...", "ஆம்..."),
        "checking": ("ஒரு நிமிடம், சரிபார்க்கிறேன்...", "சரி, இப்போது பார்க்கிறேன்..."),
        "critical_checking": ("ஒரு நிமிடம், சரிபார்க்கிறேன்.",),
        "empathy": ("உங்கள் நிலை புரிகிறது.", "சரி, புரிகிறது."),
        "backchannel": ("ம்...", "சரி...", "ஆம்..."),
        "correction_token": ("மன்னிக்கவும்",),
    },
    "te": {
        "thinking": ("హ్మ్...", "సరే...", "చూద్దాం..."),
        "acknowledgement": ("సరే...", "అర్థమైంది...", "అవును..."),
        "checking": ("ఒక్క క్షణం, పరిశీలిస్తాను...", "సరే, ఇప్పుడు చూస్తాను..."),
        "critical_checking": ("ఒక్క క్షణం, పరిశీలిస్తాను.",),
        "empathy": ("మీ పరిస్థితి అర్థమైంది.", "సరే, అర్థమైంది."),
        "backchannel": ("హ్మ్...", "సరే...", "అవును..."),
        "correction_token": ("క్షమించండి",),
    },
    "ur": {
        "thinking": ("ہمم...", "اچھا...", "دیکھیے..."),
        "acknowledgement": ("ٹھیک ہے...", "جی...", "سمجھ آگئی..."),
        "checking": ("ایک لمحہ، ابھی چیک کرتے ہیں...", "جی، ابھی دیکھتے ہیں..."),
        "critical_checking": ("ایک لمحہ، ابھی تصدیق کرتے ہیں۔",),
        "empathy": ("جی، آپ کی بات سمجھ میں آ رہی ہے۔", "آپ کی پریشانی سمجھ میں آ رہی ہے۔"),
        "backchannel": ("ہمم...", "جی...", "اچھا..."),
        "correction_token": ("معاف کیجیے",),
    },
}

# Caller signals (shared/orchestration/intent_classifier PLATFORM_SIGNALS)
# that mark a serious / distressed context: no playful hesitation, only a
# brief empathetic acknowledgement is permitted.
_SERIOUS_SIGNALS = frozenset(
    {"complaint", "hardship", "refusal", "wrong_person", "agent_request",
     "distress", "frustration"}
)
# Signals where an acknowledgement feels natural before the answer.
_ACK_SIGNALS = frozenset({"affirm", "already_paid", "payment_intent", "callback"})

_QUESTION_END = re.compile(r"[?？]\s*$")
_WORD_RE = re.compile(r"\S+")

# Dispatch-time acknowledgement contexts → pools. Languages without the
# dedicated ``ack_*`` pools reuse their existing short pools.
EARLY_ACK_CONTEXTS = ("answer", "question", "lookup", "neutral")
_EARLY_ACK_POOLS = {
    "answer": "ack_answer", "question": "ack_question",
    "lookup": "ack_lookup", "neutral": "ack_neutral",
}
_EARLY_ACK_FALLBACK = {
    "ack_answer": "acknowledgement", "ack_question": "thinking",
    "ack_lookup": "thinking", "ack_neutral": "backchannel",
}
# A preface that OPENS with an acknowledgement word would stack onto a
# dispatch-time "जी…" ("जी… … Achha, ek minute…"). Token followed by
# whitespace/punctuation, so Devanagari matras never split a word.
_LEADING_ACK_RE = re.compile(
    r"^\W*(?:achha|acha|ji|haan|hmm+|theek hai|ok(?:ay)?|right|got it|"
    r"जी|अच्छा|हाँ|हम्म|ठीक है)(?=[\s,.…!;:]|$)",
    re.IGNORECASE,
)

# Latency-ladder cues (voice_runtime.latency_filler / voiced_cues): ONE fixed,
# gender-neutral text per language and rung, rendered once per voice and
# cached, so no per-turn TTS round-trip. ``hmm`` is a beat of thought; ``wait``
# announces a moment more — only ever after the breath and the hmm, when the
# reply is provably slow, and never on critical/serious turns.
LADDER_CUE_KINDS = ("hmm", "wait")
# Per language and rung: (cue_id, text) options. The FIRST entry is the
# language's neutral default. The operator chooses which cues a bot MAY use
# (``latency_filler_cue_selection``: primary = neutral default, alternates =
# the rest of the allowed set) and previews each rendered in the bot's own
# voice; which one a long wait actually gets is decided per turn from the
# caller's words (``plan_latency_cue``): never a fixed sequence, never the
# previous cue again, never a word at all on most waits. Every option is
# gender-neutral and semantically light — a cue acknowledges that the caller
# was heard, never that the bot agrees; the role table below keeps
# confirmation-flavoured cues ("ठीक है…", "उँ-हूँ…") away from complaints.
_LADDER_CUE_POOLS: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
    "hi": {
        "hmm": (
            # "Hmm…" (Latin) for Hindi too — never "हम्म…" (operator decision).
            ("hmm", "Hmm…"), ("hoon", "हूँ…"), ("achha", "अच्छा…"), ("ji", "जी…"),
            ("theek_hai", "ठीक है…"), ("un_hoon", "उँ-हूँ…"), ("oh", "ओह…"),
        ),
        "wait": (("ek_second", "एक सेकंड…"),),
    },
    "en": {
        "hmm": (
            ("hmm", "Hmm…"), ("mm_hmm", "Mm-hmm…"), ("okay", "Okay…"),
            ("right", "Right…"), ("i_see", "I see…"), ("oh", "Oh…"),
        ),
        "wait": (("one_second", "One second…"),),
    },
    "gu": {"hmm": (("hmm", "હમ્મ…"),), "wait": (("wait", "એક ક્ષણ…"),)},
    "ml": {"hmm": (("hmm", "ഹും…"),), "wait": (("wait", "ഒരു നിമിഷം…"),)},
    "mr": {"hmm": (("hmm", "हं…"),), "wait": (("wait", "एक क्षण…"),)},
    "pa": {"hmm": (("hmm", "ਹੂੰ…"),), "wait": (("wait", "ਇੱਕ ਪਲ…"),)},
    "ta": {"hmm": (("hmm", "ம்…"),), "wait": (("wait", "ஒரு நிமிடம்…"),)},
    "te": {"hmm": (("hmm", "హ్మ్…"),), "wait": (("wait", "ఒక్క క్షణం…"),)},
    "ur": {"hmm": (("hmm", "ہمم…"),), "wait": (("wait", "ایک لمحہ…"),)},
}
# Default text per language and rung (the first pool entry).
_LADDER_CUES: dict[str, dict[str, str]] = {
    lang: {kind: options[0][1] for kind, options in pools.items()}
    for lang, pools in _LADDER_CUE_POOLS.items()
}


def ladder_cue(language: str | None, kind: str) -> str:
    """The default cue text for ``kind`` in ``language`` ("" when the language
    has no pool — never a cross-language cue)."""
    return _LADDER_CUES.get(base_language(language), {}).get(kind, "")


def ladder_cue_options(language: str | None, kind: str) -> list[dict]:
    """Every cue option for ``kind`` in ``language``: ``[{"id", "text"}, …]``,
    default first."""
    return [
        {"id": cue_id, "text": text}
        for cue_id, text in _LADDER_CUE_POOLS.get(base_language(language), {}).get(kind, ())
    ]


def ladder_cue_text(language: str | None, kind: str, cue_id: str | None) -> str:
    """The text of one cue option ("" when the id is unknown for that
    language/rung)."""
    for option in _LADDER_CUE_POOLS.get(base_language(language), {}).get(kind, ()):
        if option[0] == cue_id:
            return option[1]
    return ""


def default_cue_selection(language: str | None) -> dict | None:
    """The cues a bot MAY use when it selects nothing: the whole pool of the
    language (neutral default first). Which one plays is a per-turn,
    context-driven decision (``plan_latency_cue``). None without a pool."""
    options = ladder_cue_options(language, "hmm")
    if not options:
        return None
    return {"primary": options[0]["id"], "alternates": [o["id"] for o in options[1:]]}


# What each voiced cue conveys, per language. The planner ranks the allowed
# cues for the turn's context by these roles — a table of MEANINGS, not a
# fixed mapping from context to one cue.
#   thinking      a beat of thought while working something out
#   positive_ack  warm agreement-flavoured acknowledgement ("उँ-हूँ…")
#   information   "I've taken that in" after the caller gave details ("अच्छा…")
#   polite        a courteous acknowledgement ("जी…")
#   confirm       "understood / noted" after an answer to a question ("ठीक है…")
#   concern       a soft reaction to unexpected or troubling news ("ओह…")
_CUE_ROLES: dict[str, dict[str, str]] = {
    "hi": {"hmm": "thinking", "hoon": "thinking", "achha": "information", "ji": "polite",
           "theek_hai": "confirm", "un_hoon": "positive_ack", "oh": "concern"},
    "en": {"hmm": "thinking", "mm_hmm": "positive_ack", "okay": "confirm", "right": "polite",
           "i_see": "information", "oh": "concern"},
}
LATENCY_CUE_CONTEXTS = (
    "lookup", "thinking", "information", "confirm", "affirm", "polite", "concern", "neutral",
)
# Role preference per turn context (best first). Roles absent for a context
# are never used there: after a complaint nothing that could read as
# agreement or acceptance; after a plain statement nothing that sounds like
# surprise.
_CUE_CONTEXT_ROLES: dict[str, tuple[str, ...]] = {
    "lookup": ("thinking", "polite"),
    "thinking": ("thinking", "polite"),
    "information": ("information", "confirm", "polite", "thinking"),
    "confirm": ("confirm", "information", "polite", "thinking"),
    "affirm": ("positive_ack", "polite", "confirm", "thinking"),
    "polite": ("polite", "positive_ack", "thinking"),
    "concern": ("concern", "thinking", "polite"),
    "neutral": ("thinking", "polite"),
}
# In a serious caller state (complaint, refusal, hardship…) only these roles
# are ever voiced, whatever the context looked like.
_SERIOUS_CUE_ROLES = ("concern", "thinking", "polite")
# "ओह…" is a reaction, not a filler: at most once per call, and even then
# only on a minority of concern turns.
_CONCERN_CUE_PROBABILITY = 0.4


@dataclass
class LatencyCuePlan:
    """The planner's decision for one long wait's voiced cue.

    ``verbal`` — may a voiced cue play at all this turn (else the wait stays
    a breath); ``cue_ids`` — the bot's allowed cues in preference order for
    this turn (best first; the processor plays the first one already
    rendered); ``reason`` — why ``verbal`` is False (telemetry).
    """

    verbal: bool
    cue_ids: list[str] = field(default_factory=list)
    context: str = "neutral"
    reason: str = ""
    role: str = ""
    delay_ms: int | None = None

    def as_selection(self) -> dict | None:
        if not self.cue_ids:
            return None
        return {"primary": self.cue_ids[0], "alternates": list(self.cue_ids[1:])}



def normalize_spoken_variant(text: str) -> str:
    """Comparison form for cross-pool no-repeat tracking.

    NFKC folds compatibility forms; case, whitespace, punctuation and either
    ASCII or Unicode ellipses are ignored. Script letters remain intact, so
    visually similar phrases in different languages are never conflated.
    """
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    normalized = normalized.replace("…", "...")
    return " ".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def is_serious_caller_state(signal: str | None) -> bool:
    """Whether a trusted caller-state signal suppresses casual decoration."""
    return str(signal or "").strip().lower() in _SERIOUS_SIGNALS


def base_language(locale: str | None) -> str:
    """Platform locale -> pool key ('hi-IN' -> 'hi'). Hinglish rides 'hi'."""
    code = (locale or "").strip().lower()
    if not code:
        return ""
    base = code.split("-", 1)[0].split("_", 1)[0]
    return "hi" if base in ("hi", "hinglish") else base


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------


@dataclass
class TurnSpeechPlan:
    """How the upcoming assistant turn should be delivered."""

    preface: str = ""
    preface_kind: str = ""          # thinking | acknowledgement | checking | empathy
    allow_self_correction: bool = False
    critical: bool = False
    critical_reason: str = ""
    telemetry: dict = field(default_factory=dict)

    @property
    def has_preface(self) -> bool:
        return bool(self.preface)


@dataclass
class SegmentDelivery:
    """Delivery metadata for one already-aggregated TTS sentence."""

    pause_after_ms: int | None = None   # None -> router default
    speed_scale: float | None = None    # multiplier on the bot's base speed
    critical: bool = False
    critical_reason: str = ""
    # One soft breath before this sentence (pause mode; rare; ≤1 per turn).
    breath_before: bool = False
    emphasis: str = "none"             # none | moderate
    pitch_scale: float | None = None
    energy_scale: float | None = None
    question_style: bool = False
    speech_style: str = "neutral"      # neutral | supportive | serious
    phrase_boundaries: tuple[int, ...] = ()


# --------------------------------------------------------------------------
# Planner
# --------------------------------------------------------------------------


class SpeechNaturalnessPlanner:
    """Config-driven, per-call naturalness planner.

    One instance is created per call and shared by the conversation brain
    (turn prefaces, backchannels, self-correction) and the TTS router
    (per-sentence pause/rate variation).  All methods are synchronous and
    allocation-light; everything is safe to call on the audio hot path.
    """

    def __init__(self, config: dict | None = None, *,
                 rng: random.Random | None = None,
                 config_sources: dict[str, str] | None = None) -> None:
        self._config = resolve_human_speech(config)
        self._rng = rng or random.Random()
        # Per-pool recent picks: never repeat the last three variants.
        self._recent: dict[str, deque[str]] = {}
        # One shared delivery-state window across thinking, acknowledgement,
        # checking and backchannel pools. Values are normalized so "Achha..."
        # and " achha … " are the same spoken variant. Three deep: repeated
        # calls in one campaign must not sound like the same recorded script.
        self._recent_spoken: deque[str] = deque(maxlen=3)
        self._last_preface_turn: int | None = None
        self._last_early_ack_turn: int | None = None
        self._last_early_ack_reason = ""
        # "ओह…" is a one-per-call reaction (plan_latency_cue).
        self._oh_used = False
        self._last_backchannel_monotonic: float | None = None
        self._backchannels_played = 0
        self._last_backchannel_suppression_reason = ""
        self._turn_critical_reason = ""
        self._config_sources = {
            key: value
            for key, value in (config_sources or {}).items()
            if key in HUMAN_SPEECH_DEFAULTS and value in ("platform", "tenant", "bot")
        }

    # -- config ----------------------------------------------------------

    @property
    def config(self) -> dict:
        return dict(self._config)

    @property
    def enabled(self) -> bool:
        return bool(self._config["enabled"])

    @property
    def backchannels_enabled(self) -> bool:
        return self.enabled and bool(self._config["backchannels"])

    @property
    def min_long_turn_for_backchannel_ms(self) -> int:
        return int(self._config["min_long_turn_for_backchannel_ms"])

    @property
    def min_gap_between_backchannels_ms(self) -> int:
        return int(self._config["min_gap_between_backchannels_ms"])

    @property
    def latency_fillers_enabled(self) -> bool:
        """Pre-rendered gap fillers ride the master switch like every other
        naturalness dimension."""
        return self.enabled and bool(self._config["latency_fillers"])

    @property
    def latency_filler_delay_ms(self) -> int:
        return int(self._config["latency_filler_delay_ms"])

    @property
    def latency_filler_ladder_enabled(self) -> bool:
        return self.latency_fillers_enabled and bool(self._config["latency_filler_ladder"])

    @property
    def latency_filler_hmm_ms(self) -> int:
        return int(self._config["latency_filler_hmm_ms"])

    @property
    def latency_filler_spoken_ms(self) -> int:
        return int(self._config["latency_filler_spoken_ms"])

    @property
    def latency_filler_kind(self) -> str:
        """Which pre-rendered sound covers the pre-reply gap."""
        kind = str(self._config.get("latency_filler_kind") or "breath")
        return kind if kind in FILLER_SOUND_KINDS else "breath"

    def filler_selection_for(self, kind: str, gender: str) -> dict | None:
        """The bot's clip choice for ``kind``/``gender`` (primary + alternates),
        or None when every clip of that gender may rotate."""
        return selection_choice(self._config.get("filler_audio_selection"), kind, gender)

    def cue_selection_for(self, language: str | None) -> dict | None:
        """The cues the bot MAY voice on a long wait in ``language`` (primary
        = neutral default, alternates = the rest of the allowed set), else the
        language's whole pool. None when the language has no cue pool. Which
        of them a turn gets is ``plan_latency_cue``'s decision."""
        chosen = selection_choice(
            self._config.get("latency_filler_cue_selection"), base_language(language)
        )
        return chosen if chosen else default_cue_selection(language)

    @property
    def latency_cue_probability(self) -> float:
        return float(self._config.get("latency_cue_probability", 0.7))

    def plan_latency_cue(self, *, language: str, context: str = "neutral",
                         serious: bool = False, critical: bool = False,
                         turn_index: int = 0, early_ack_spoken: bool = False,
                         expected_fast: bool = False,
                         last_cue: str | None = None) -> LatencyCuePlan:
        """Decide, for the turn just dispatched, whether a LONG wait may get a
        voiced cue and which of the bot's allowed cues fit it best.

        Decision order:

        1. **Is a word needed at all?** No voiced cue when the language has
           no pool, when the turn already got a spoken acknowledgement (one
           voice, not two), when the caller dictated critical content
           (amounts, identifiers, OTPs — the reply must be the next thing
           they hear), when the reply is expected quickly, and on a share of
           turns by ``latency_cue_probability`` — most long waits stay a
           breath.
        2. **Which word?** The bot's allowed cues are ranked by the roles that
           fit ``context`` (what the caller just did: ``lookup``/``thinking``,
           ``information``, ``confirm``, ``affirm``, ``polite``, ``concern``,
           ``neutral``); in a serious caller state only thinking / polite /
           concern roles survive, so nothing sounds like agreement with a
           complaint. "ओह…" (concern) is used sparingly: at most once per call,
           on a minority of concern turns. The previously voiced cue is never
           first again while an alternative exists.

        The processor plays the first cue in ``cue_ids`` that is already
        rendered, so the order is a preference, not a sequence.
        """
        lang = base_language(language)
        options = ladder_cue_options(language, "hmm")
        if context not in LATENCY_CUE_CONTEXTS:
            context = "neutral"
        if last_cue == "oh":
            self._oh_used = True
        plan = LatencyCuePlan(verbal=False, context=context)
        adaptive = bool(self._config.get("adaptive_latency_cues"))
        if adaptive:
            # Lookup/question turns benefit from an earlier listening cue;
            # short confirmations leave more room for a direct reply. This
            # is a playback opportunity, never a delay added to the answer.
            plan.delay_ms = {
                "lookup": 1500, "thinking": 1800, "information": 2000,
                "concern": 2000, "neutral": 2200, "confirm": 2500,
                "affirm": 2500, "polite": 2500,
            }[context]
            if expected_fast:
                # A completed decision is not playable audio. Give its TTS
                # time, but don't suppress the cue forever if synthesis stalls.
                plan.delay_ms = 2500
        if not options:
            plan.reason = f"no_pool_language:{lang or '?'}"
            return plan
        selection = self.cue_selection_for(language) or {}
        pool_ids = [o["id"] for o in options]
        allowed = [cue for cue in selection_ids(selection) if cue in pool_ids] or pool_ids
        roles = _CUE_ROLES.get(lang, {cue: "thinking" for cue in pool_ids})
        preferred_roles = _CUE_CONTEXT_ROLES[context]
        if serious:
            preferred_roles = tuple(r for r in _SERIOUS_CUE_ROLES if r in preferred_roles) or ("thinking", "polite")
        # Rank: role preference first, pool order within a role; roles the
        # context does not list are dropped (an emotionally wrong cue is
        # worse than a breath).
        ranked: list[str] = []
        for role in preferred_roles:
            if role == "concern":
                if self._oh_used or self._rng.random() >= _CONCERN_CUE_PROBABILITY:
                    continue
            ranked.extend(cue for cue in allowed if roles.get(cue) == role and cue not in ranked)
        if last_cue in ranked and len(ranked) > 1:
            ranked.remove(last_cue)
            ranked.append(last_cue)
        plan.cue_ids = ranked
        plan.role = roles.get(ranked[0], "") if ranked else ""
        if not ranked:
            plan.reason = "no_fitting_cue"
            return plan
        if not self.latency_fillers_enabled or not self._config.get("latency_filler_ladder", True):
            plan.reason = "disabled"
            return plan
        if early_ack_spoken:
            plan.reason = "ack_already_spoken"
            return plan
        if critical:
            plan.reason = "critical_content"
            return plan
        if expected_fast and not adaptive:
            plan.reason = "reply_expected_fast"
            return plan
        probability = self.latency_cue_probability
        if serious:
            probability *= 0.5
        if turn_index == 1:
            probability = min(1.0, probability * _FIRST_REPLY_PREFACE_BOOST)
        if self._rng.random() >= probability:
            plan.reason = "roll"
            return plan
        plan.verbal = True
        return plan

    def note_latency_cue_played(self, cue_id: str | None) -> None:
        """Telemetry from the processor: which voiced cue a wait got."""
        if cue_id == "oh":
            self._oh_used = True

    @property
    def configuration_level(self) -> str:
        """Highest override level represented in this resolved config."""
        levels = set(self._config_sources.values())
        if "bot" in levels:
            return "bot"
        if "tenant" in levels:
            return "tenant"
        return "platform"

    def set_turn_criticality(self, critical: bool, reason: str = "") -> None:
        """Set structured criticality before any speech for the current turn."""
        self._turn_critical_reason = (reason or "structured") if critical else ""

    # -- variant selection -----------------------------------------------

    def _adapted(self, text: str, identity: VoiceIdentity | None) -> str:
        if not self._config["gender_agreement"]:
            return text
        if identity is None or identity.gender not in ("male", "female"):
            return text
        return adapt_authored_speaker_grammar(text, identity)

    @staticmethod
    def _is_gendered(text: str) -> bool:
        male = adapt_authored_speaker_grammar(text, VoiceIdentity(gender="male"))
        female = adapt_authored_speaker_grammar(text, VoiceIdentity(gender="female"))
        return male != female

    def _pick(self, language: str, pool_key: str,
              identity: VoiceIdentity | None, *,
              exclude_leading_ack: bool = False) -> str:
        pools = _POOLS.get(language, {})
        pool = pools.get(pool_key) or pools.get(_EARLY_ACK_FALLBACK.get(pool_key, "")) or ()
        if not pool:
            return ""
        gender = identity.gender if identity else "neutral"
        candidates = [
            entry for entry in pool
            if gender in ("male", "female") or not self._is_gendered(entry)
        ]
        if exclude_leading_ack:
            # Never stack: a dispatch-time "जी…" already opened the turn.
            # A pool made only of ack-led variants keeps them rather than
            # losing a tool preface that masks real dead air.
            unstacked = [e for e in candidates if not _LEADING_ACK_RE.match(e)]
            candidates = unstacked or candidates
        if not candidates:
            return ""
        recent = self._recent.setdefault(f"{language}:{pool_key}", deque(maxlen=3))
        adapted = [(entry, self._adapted(entry, identity)) for entry in candidates]
        # Prefer avoiding both pool-local and global recent spoken variants.
        fresh = [
            pair for pair in adapted
            if normalize_spoken_variant(pair[0]) not in recent
            and normalize_spoken_variant(pair[1]) not in self._recent_spoken
        ]
        # A language with one safe variant must continue to work: if no fresh
        # option exists, choose from the valid pool instead of failing.
        entry, spoken = self._rng.choice(fresh or adapted)
        normalized_entry = normalize_spoken_variant(entry)
        normalized_spoken = normalize_spoken_variant(spoken)
        recent.append(normalized_entry)
        self._recent_spoken.append(normalized_spoken)
        return spoken

    # -- turn-level planning ----------------------------------------------

    def plan_turn(self, *, language: str, identity: VoiceIdentity | None = None,
                  signal: str = "", route_kind: str = "llm",
                  turn_index: int = 0, critical: bool = False,
                  critical_reason: str = "",
                  allow_safe_tool_preface: bool = False,
                  early_ack_spoken: bool = False) -> TurnSpeechPlan:
        """Plan delivery for the upcoming assistant turn.

        ``route_kind``: "tool" (a backend lookup runs before the reply),
        "kb" (knowledge retrieval), "llm" (generated reply), or "direct"
        (deterministic/scripted reply text).
        ``signal`` is the platform caller signal from the decision layer.

        Only TOOL routes receive a preface here ("ek minute, check karta
        hoon…", spoken before the lookup runs — it masks real dead air and
        names what is happening). Acknowledgements and thinking beats are
        planned at dispatch (:meth:`plan_early_ack`) and played from cached
        audio only if the latency deadline is reached before reply audio.
        ``early_ack_spoken`` keeps a tool preface from stacking onto one.
        """
        plan = TurnSpeechPlan()
        cfg = self._config
        lang = base_language(language)
        plan.telemetry = {
            "filler_used": False,
            "filler_type": "",
            "acknowledgement_used": False,
            "language": lang,
            "gender_mode": identity.gender if identity else "neutral",
            "signal": signal,
            "route_kind": route_kind,
            "critical_content": bool(critical),
            "configuration_level": self.configuration_level,
            "streaming_self_correction": "disabled_safe_boundary_unavailable",
            "early_ack": bool(early_ack_spoken),
        }
        plan.critical = bool(critical)
        plan.critical_reason = critical_reason if critical else ""
        self.set_turn_criticality(plan.critical, plan.critical_reason)
        if not self.enabled:
            plan.telemetry["suppression_reason"] = "disabled"
            return plan
        if lang not in _POOLS:
            # Unsupported pool language: never inject cross-language fillers.
            plan.telemetry["suppression_reason"] = f"no_pool_language:{lang or '?'}"
            return plan
        if turn_index <= 0:
            plan.telemetry["suppression_reason"] = "greeting_turn"
            return plan  # never decorate the greeting

        if critical:
            # A generic tool lookup may use one unambiguous checking phrase;
            # high-risk routes (payment reference, identity, compliance,
            # commitment and deterministic finance) suppress even that.
            if (
                route_kind == "tool"
                and allow_safe_tool_preface
                and self._rng.random() < cfg["tool_ack_probability"]
            ):
                preface = self._pick(
                    lang, "critical_checking", identity,
                    exclude_leading_ack=early_ack_spoken,
                )
                if preface:
                    plan.preface = preface
                    plan.preface_kind = "checking"
                    self._last_preface_turn = turn_index
                    plan.telemetry["filler_used"] = True
                    plan.telemetry["filler_type"] = "checking"
                    plan.telemetry["acknowledgement_used"] = True
                    plan.telemetry["suppression_reason"] = ""
                    return plan
            plan.telemetry["suppression_reason"] = (
                f"critical:{critical_reason or 'structured'}"
            )
            return plan

        serious = signal in _SERIOUS_SIGNALS
        suppression = ""

        pool_key = ""
        if route_kind != "tool":
            # The acknowledgement for this turn was decided at dispatch
            # (plan_early_ack); the latency processor decides whether it is
            # needed. Nothing is glued to the front of the reply.
            suppression = "dispatch_ack_path"
        elif self._rng.random() < cfg["tool_ack_probability"]:
            # A lookup is about to run: a spoken "let me check" both sounds
            # human and masks tool latency. Serious contexts get the calmer
            # empathetic form first.
            pool_key = "empathy" if serious else "checking"
        else:
            suppression = "roll"

        if pool_key:
            preface = self._pick(
                lang, pool_key, identity, exclude_leading_ack=early_ack_spoken,
            )
            if preface:
                plan.preface = preface
                plan.preface_kind = pool_key
                self._last_preface_turn = turn_index
                suppression = ""
            else:
                suppression = "no_pool_variant"
        plan.telemetry["suppression_reason"] = suppression

        plan.allow_self_correction = (
            bool(cfg["self_correction"]) and route_kind in ("llm", "direct")
            and not serious
        )
        plan.telemetry["filler_used"] = plan.has_preface
        plan.telemetry["filler_type"] = plan.preface_kind
        plan.telemetry["acknowledgement_used"] = plan.preface_kind in (
            "acknowledgement", "empathy",
        )
        return plan

    # -- dispatch-time acknowledgement -------------------------------------

    def plan_early_ack(self, *, language: str, identity: VoiceIdentity | None = None,
                       context: str = "answer", turn_index: int = 0,
                       serious: bool = False, critical: bool = False,
                       commit: bool = True) -> str:
        """One short spoken acknowledgement for the turn just dispatched.

        Planned when the caller's turn closes, then eligible only after the
        latency filler's configured delay while reply audio is unavailable.
        With ``commit=False``, the runtime calls ``note_early_ack_played`` only
        when cached audio actually plays; skipped fast turns do not consume
        the no-consecutive-turns allowance. ``context`` is
        what the caller just did, derived deterministically from their words:

        * ``answer``   — a statement or an answer: "जी…", "ठीक है…", "अच्छा…"
        * ``question`` — a question: a beat of thought ("Hmm…"), never
          "ठीक है" (which would sound like an answer)
        * ``lookup``   — a knowledge question a retrieval will answer:
          "एक सेकंड…", "देख रहा हूँ…"
        * ``neutral``  — anything sensitive: a serious caller state
          (complaint, refusal, hardship…) or dictated amounts/identifiers.
          Only listening tokens ("जी…", "Hmm…") at half probability;
          "ठीक है"/"अच्छा" after a refusal would read as acceptance.

        Control: never on two consecutive turns (a hard rule — no call
        opens every reply with "जी"), per-turn probability from
        ``acknowledgement_probability`` (boosted on the first reply, the
        slowest of the call), pool rotation with no-repeat, one token only
        (never stacked), and nothing for greetings or unsupported languages.
        Returns "" when no acknowledgement should be spoken;
        :attr:`last_early_ack_reason` says why.
        """
        cfg = self._config
        self._last_early_ack_reason = ""

        def _withhold(reason: str) -> str:
            self._last_early_ack_reason = reason
            return ""

        if not (self.enabled and cfg["acknowledgements"]):
            return _withhold("disabled")
        lang = base_language(language)
        if lang not in _POOLS:
            return _withhold(f"no_pool_language:{lang or '?'}")
        if turn_index <= 0:
            return _withhold("greeting_turn")
        if self._last_early_ack_turn == turn_index - 1:
            return _withhold("anti_repetition")
        if context not in _EARLY_ACK_POOLS:
            context = "answer"
        if serious or critical:
            context = "neutral"
        if context == "question" and not cfg["thinking_fillers"]:
            return _withhold("thinking_disabled")
        probability = cfg["acknowledgement_probability"]
        if context == "neutral":
            probability *= 0.5
        if turn_index == 1:
            # First reply after the greeting: the slowest turn of the call
            # gets the best odds of a spoken beat while the system thinks.
            probability = min(1.0, probability * _FIRST_REPLY_PREFACE_BOOST)
        if self._rng.random() >= probability:
            return _withhold("roll")
        token = self._pick(lang, _EARLY_ACK_POOLS[context], identity)
        if not token:
            return _withhold("no_pool_variant")
        if commit:
            self.note_early_ack_played(turn_index)
        self._last_early_ack_reason = ""
        return token

    def note_early_ack_played(self, turn_index: int) -> None:
        self._last_early_ack_turn = turn_index

    @property
    def last_early_ack_reason(self) -> str:
        return self._last_early_ack_reason

    # -- segment-level planning (TTS router) -------------------------------

    def plan_segment(self, text: str, *, base_pause_ms: int,
                     language: str = "", first_in_turn: bool = False,
                     breaths_so_far: int = 0) -> SegmentDelivery:
        """Per-sentence delivery: pause variation + subtle rate variation.

        Called by the TTS router for each aggregated sentence (pause mode).
        Critical segments get clear pacing and a slightly longer separating
        pause; questions slow down a touch; short acknowledgements ride a
        touch quicker; everything else may receive a small deterministic
        jitter so pacing never sounds metronomic. ``first_in_turn`` /
        ``breaths_so_far`` gate the rare in-reply breath (never before the
        first sentence — the reply gap has its own filler — and at most one
        per turn, only before a long or critical sentence).
        """
        delivery = SegmentDelivery()
        cfg = self._config
        if not self.enabled:
            return delivery

        segment_critical = contains_critical_content(text)
        delivery.critical = bool(self._turn_critical_reason) or segment_critical
        delivery.critical_reason = (
            self._turn_critical_reason
            or ("segment_pattern" if segment_critical else "")
        )
        is_question = bool(_QUESTION_END.search(text or ""))
        words = len(_WORD_RE.findall(text or ""))
        delivery.question_style = is_question
        delivery.phrase_boundaries = tuple(
            match.end() for match in re.finditer(r"[,;:—–]", text or "")
        )
        if delivery.critical:
            delivery.speech_style = "serious"
            delivery.emphasis = "moderate"
            delivery.energy_scale = 0.95
            delivery.pitch_scale = 1.0
        elif is_question:
            delivery.speech_style = "supportive"
            delivery.emphasis = "moderate"

        if cfg["prosody_variation"]:
            if delivery.critical:
                # Clear pacing for amounts/dates/ids: slightly slower, never
                # faster, no jitter.
                delivery.speed_scale = 0.96
            elif is_question:
                delivery.speed_scale = round(0.95 + self._rng.random() * 0.03, 3)
            elif words <= 3:
                # "जी।" / "ठीक है।": acknowledgements sit a touch quicker
                # than questions, the way a person tosses one off.
                delivery.speed_scale = round(1.02 + self._rng.random() * 0.03, 3)
            else:
                delivery.speed_scale = round(0.97 + self._rng.random() * 0.06, 3)

        if cfg["micro_pauses"] and base_pause_ms > 0:
            if delivery.critical:
                # A clean boundary around critical content aids comprehension.
                delivery.pause_after_ms = min(700, base_pause_ms + 120)
            elif words <= 3:
                # Short ack fragments ("Achha...") read best with a beat after.
                delivery.pause_after_ms = min(700, max(180, base_pause_ms + 70))
            elif self._rng.random() < cfg["micro_pause_probability"]:
                jitter = self._rng.randint(-60, 140)
                delivery.pause_after_ms = min(700, max(80, base_pause_ms + jitter))

        if (
            cfg["sentence_breaths"]
            and base_pause_ms > 0
            and not first_in_turn
            and breaths_so_far < 1
            and (words >= _LONG_SENTENCE_WORDS or delivery.critical)
            and self._rng.random() < cfg["sentence_breath_probability"]
        ):
            # The beat a person takes before a longer explanation or a
            # verification read-back. Subtle, rare, never after every line.
            delivery.breath_before = True
        return delivery

    # -- self-correction ----------------------------------------------------

    def maybe_self_correct(self, text: str, *, language: str,
                           identity: VoiceIdentity | None = None) -> str:
        """Very rare, controlled restart ("Aapka payment... sorry, ...").

        Never applied to critical content; disabled by default.  Returns the
        text unchanged when no correction applies.
        """
        cfg = self._config
        if not (self.enabled and cfg["self_correction"]):
            return text
        if contains_critical_content(text):
            return text
        lang = base_language(language)
        tokens = _POOLS.get(lang, {}).get("correction_token") or ()
        words = text.split()
        if not tokens or len(words) < 6:
            return text
        if self._rng.random() >= cfg["self_correction_probability"]:
            return text
        lead = " ".join(words[:2])
        restart = " ".join(words[1:])
        token = self._rng.choice(tokens)
        return f"{lead}... {token}, {restart}"

    # -- backchannels --------------------------------------------------------

    def plan_backchannel(self, *, language: str,
                         identity: VoiceIdentity | None = None,
                         caller_state: str = "",
                         now: float | None = None) -> str:
        """Return a backchannel token to play, or "" when none should play.

        The caller (conversation brain) is responsible for the *turn-state*
        gates: caller has been speaking continuously long enough, bot silent,
        no generation in flight.  This method owns probability, spacing and
        variant choice.  A non-empty return value starts the cooldown clock.
        """
        cfg = self._config
        self._last_backchannel_suppression_reason = ""
        if not self.backchannels_enabled:
            self._last_backchannel_suppression_reason = "disabled"
            return ""
        if self._backchannels_played >= cfg["max_backchannels_per_call"]:
            self._last_backchannel_suppression_reason = "max_count"
            return ""
        lang = base_language(language)
        if lang not in _POOLS:
            self._last_backchannel_suppression_reason = "unsupported_language"
            return ""
        moment = time.monotonic() if now is None else now
        if self._last_backchannel_monotonic is not None:
            gap_s = cfg["min_gap_between_backchannels_ms"] / 1000.0
            if moment - self._last_backchannel_monotonic < gap_s:
                self._last_backchannel_suppression_reason = "cooldown"
                return ""
        if caller_state in _SERIOUS_SIGNALS:
            # No casual agreement over a complaint, refusal, hardship or
            # distress. Silence is safer than a phrase that could sound like
            # semantic acceptance while the caller still owns the floor.
            self._last_backchannel_monotonic = moment
            self._last_backchannel_suppression_reason = (
                f"serious_context:{caller_state}"
            )
            return ""
        if self._rng.random() >= cfg["backchannel_probability"]:
            # A failed roll still consumes the opportunity window so the
            # monitor does not immediately re-roll every tick.
            self._last_backchannel_monotonic = moment
            self._last_backchannel_suppression_reason = "probability"
            return ""
        token = self._pick(lang, "backchannel", identity)
        if token:
            self._last_backchannel_monotonic = moment
            self._backchannels_played += 1
        return token

    @property
    def backchannels_played(self) -> int:
        return self._backchannels_played

    @property
    def last_backchannel_suppression_reason(self) -> str:
        return self._last_backchannel_suppression_reason
