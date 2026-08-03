"""Final-transcript quality gate — drops noise and foreign-language hallucinations.

Realtime STT (especially Sarvam saaras in language auto-detect mode) reliably
hallucinates SOMETHING out of background noise, line hum and sub-word audio
fragments: short random phrases, often in an unrelated language the acoustic
model happens to consider likely. Left unchecked those phantom "utterances"
become user turns — they enter conversation history, run the router/workflow,
reach the LLM and produce a spoken reply to something the caller never said.

Every FINAL transcript segment passes through :func:`assess_transcript` before
the brain buffers it. The platform currently serves Hindi and English callers
(:data:`ALLOWED_STT_LANGUAGES` — Hinglish/code-switched speech is a first-class
citizen of both), so the gate anchors on that allowed set plus whatever quality
metadata the configured STT provider exposes (:func:`segment_quality`: detected
language, language probability, transcript confidence, no-speech probability,
audio duration — providers differ, every field is optional). Evidence order:

1. positive noise evidence rejects: high no-speech probability, very low
   provider confidence, an impossible speech rate, sub-word audio duration;
2. foreign-SCRIPT text rejects: hi/en/Hinglish speech can only ever be written
   in Devanagari and/or Latin, so a transcript dominated by Tamil/Telugu/
   Bengali/Arabic… glyphs is a misdetection by construction;
3. a foreign language LABEL rejects only when the text itself does not read as
   Hindi/English/Hinglish (or the provider is highly confident and the script
   is ambiguous romanized text) — a mislabel must never drop valid speech;
4. everything else is ACCEPTED — the gate fails open: a transcript with no
   metadata and a plausible script is treated as a real utterance.

Legitimate short replies ("haan", "nahi", "yes", "no", "ok", "नहीं") are
protected by the meaningful-short-reply lexicon (the router's signal patterns
plus hang-up detection), never by a minimum-length rule.
"""

import logging
import re
from dataclasses import dataclass

from shared.orchestration.router import classify_user_signal, detect_hangup

logger = logging.getLogger(__name__)

# The languages callers may speak to the platform, as base ISO 639-1 codes.
# For now this is Hindi + English only; per-bot overrides come from
# stt_settings.allowed_languages (see resolve_allowed_languages).
ALLOWED_STT_LANGUAGES: frozenset[str] = frozenset({"hi", "en"})

# ── gate thresholds ─────────────────────────────────────────────────────────
# A no-speech probability this high means the STT itself believes the segment
# was not speech (OpenAI Whisper reports this per segment).
NO_SPEECH_REJECT_PROBABILITY = 0.85
# Provider transcript confidence below this is garbage on every provider that
# reports one (Deepgram/whisper-derived; clear speech scores far higher).
MIN_TRANSCRIPT_CONFIDENCE = 0.35
# Reject when at least half of the lettered text is in scripts no allowed
# language uses (real hi/en/Hinglish essentially never contains them).
FOREIGN_SCRIPT_DOMINANCE = 0.5
# A foreign language label alone is weak (labels misfire on Hinglish); treat
# it as decisive only when the provider is at least this confident AND the
# text carries no script/lexicon evidence of an allowed language.
FOREIGN_LABEL_PROBABILITY = 0.8
# Audio shorter than this cannot contain a word; local VAD padding alone makes
# real speech segments longer (start_secs+stop_secs ≥ 0.3 s everywhere).
MIN_UTTERANCE_SECONDS = 0.25
# Sustained speech tops out well under this; noise hallucinations regularly
# "transcribe" whole sentences out of half a second of hiss.
MAX_WORDS_PER_SECOND = 9.0
_RATE_CHECK_MIN_WORDS = 6

# ── script heuristics (shared with the brain's language following) ──────────
_DEVANAGARI_CHARS = re.compile(r"[ऀ-ॿ]")
_LATIN_CHARS = re.compile(r"[A-Za-z]")
_BENGALI_CHARS = re.compile(r"[ঀ-৿]")
_GURMUKHI_CHARS = re.compile(r"[਀-੿]")
_GUJARATI_CHARS = re.compile(r"[઀-૿]")
_ORIYA_CHARS = re.compile(r"[଀-୿]")
_TAMIL_CHARS = re.compile(r"[஀-௿]")
_TELUGU_CHARS = re.compile(r"[ఀ-౿]")
_KANNADA_CHARS = re.compile(r"[ಀ-೿]")
_MALAYALAM_CHARS = re.compile(r"[ഀ-ൿ]")
_ARABIC_CHARS = re.compile(r"[؀-ۿݐ-ݿ]")
_SCRIPT_PATTERNS = {
    "hi": _DEVANAGARI_CHARS,
    "mr": _DEVANAGARI_CHARS,
    "ne": _DEVANAGARI_CHARS,
    "bn": _BENGALI_CHARS,
    "as": _BENGALI_CHARS,
    "pa": _GURMUKHI_CHARS,
    "gu": _GUJARATI_CHARS,
    "or": _ORIYA_CHARS,
    "ta": _TAMIL_CHARS,
    "te": _TELUGU_CHARS,
    "kn": _KANNADA_CHARS,
    "ml": _MALAYALAM_CHARS,
    "ur": _ARABIC_CHARS,
}
# Romanized-Hindi (Hinglish) marker words: when the STT reports Hindi but the
# text is fully Latin (translit/codemix STT modes), these confirm the STT's
# verdict so a Hinglish speaker still gets Hindi replies and a Hindi voice.
_HINGLISH_HINTS = re.compile(
    r"\b(haa?n|nahin?|nhi|abhi|aaj|paisa|paise|rupay[ae]?|bhai|"
    r"theek|thik|karo|karu(?:nga|ngi)?|kar (?:do|de|dunga|dungi)|hai|hain|"
    r"mera|mere|meri|aap|kyun?|kaise|kitna|batao|bolo|dijiye)\b",
    re.I,
)


def script_supports_language(text: str, locale: str) -> bool:
    """Whether an utterance's dominant script is consistent with a locale.

    Hindi speech is transcribed in Devanagari (borrowed English words stay
    Latin, so code-mixed text still counts as Hindi when Devanagari holds a
    meaningful share). Fully-Latin text still counts as Hindi when it reads
    as romanized Hinglish — the STT's language verdict plus marker words.
    English must be clearly Latin-dominant. Other supported Indian languages
    must be dominated by their own Unicode script. Unknown scripts fail
    closed: one noisy STT language label must not change the conversation.
    """
    counts = {
        base: len(pattern.findall(text))
        for base, pattern in _SCRIPT_PATTERNS.items()
    }
    # hi/mr/ne and bn/as share a script; only count each script once.
    script_total = sum({
        id(pattern): len(pattern.findall(text))
        for pattern in _SCRIPT_PATTERNS.values()
    }.values())
    lat = len(_LATIN_CHARS.findall(text))
    total = script_total + lat
    if total == 0:
        return False
    base = locale.split("-")[0].lower()
    if base == "hi":
        dev = counts["hi"]
        if dev / total >= 0.4:
            return True
        return dev == 0 and bool(_HINGLISH_HINTS.search(text))
    if base == "en":
        return lat / total >= 0.7
    pattern = _SCRIPT_PATTERNS.get(base)
    if pattern is None:
        return False
    return counts[base] / total >= 0.6


def _foreign_script_share(text: str, allowed: frozenset[str]) -> tuple[int, float]:
    """(foreign_chars, foreign_share) of the lettered text.

    Latin is always an allowed script: romanized/code-mixed speech exists for
    every allowed language and borrowed English words appear everywhere.
    """
    allowed_patterns = {
        id(pattern)
        for base, pattern in _SCRIPT_PATTERNS.items()
        if base in allowed
    }
    foreign = allowed_count = 0
    for pattern in {id(p): p for p in _SCRIPT_PATTERNS.values()}.values():
        count = len(pattern.findall(text))
        if id(pattern) in allowed_patterns:
            allowed_count += count
        else:
            foreign += count
    allowed_count += len(_LATIN_CHARS.findall(text))
    letters = allowed_count + foreign
    return foreign, (foreign / letters) if letters else 0.0


# ── provider quality metadata ───────────────────────────────────────────────


@dataclass
class SegmentQuality:
    """Quality metadata for one final STT segment; every field optional.

    Providers differ: Sarvam WebSocket finals carry language_code /
    language_probability / metrics.audio_duration; the segmented REST path
    (EchoSTTService) attaches provider confidence, no-speech probability and
    the exact PCM duration; the mock provider only reports confidence.
    """

    provider: str = ""
    language: str | None = None  # raw provider code ("hi-IN", "ta", …)
    language_probability: float | None = None
    confidence: float | None = None
    no_speech_prob: float | None = None
    audio_seconds: float | None = None


def _as_float(value) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def base_language(code: str | None) -> str | None:
    """Normalize a provider language code to its base ISO form ("hi-IN" → "hi")."""
    if not code:
        return None
    base = str(code).split("-")[0].strip().lower()
    if not base or base == "unknown":
        return None
    return base


def segment_quality(frame, *, provider: str = "") -> SegmentQuality:
    """Extract quality metadata from a pipecat TranscriptionFrame.

    ``frame.result`` is provider-shaped: Sarvam's WebSocket service attaches
    the full streaming message ({"type": "data", "data": {...}}), the REST
    EchoSTTService a flat dict. The raw wire language in ``result`` wins over
    ``frame.language`` — pipecat's Sarvam service maps unknown wire codes to
    Language.HI_IN, which would mask exactly the detections we need to see.
    """
    quality = SegmentQuality(provider=provider)

    raw_language = getattr(frame, "language", None)
    if raw_language is not None:
        quality.language = getattr(raw_language, "value", None) or str(raw_language)

    result = getattr(frame, "result", None)
    if isinstance(result, dict):
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        quality.provider = str(result.get("provider") or provider)
        wire_language = data.get("language_code") or data.get("language")
        if wire_language:
            quality.language = str(wire_language)
        quality.language_probability = _as_float(data.get("language_probability"))
        quality.confidence = _as_float(data.get("confidence"))
        quality.no_speech_prob = _as_float(data.get("no_speech_prob"))
        metrics = data.get("metrics")
        quality.audio_seconds = _as_float(data.get("audio_seconds"))
        if quality.audio_seconds is None and isinstance(metrics, dict):
            quality.audio_seconds = _as_float(metrics.get("audio_duration"))
    return quality


# ── the gate ────────────────────────────────────────────────────────────────


@dataclass
class GateVerdict:
    accepted: bool
    reason: str = "ok"
    language: str | None = None  # normalized base code of the detected language


def meaningful_short_reply(text: str) -> bool:
    """Whether a short utterance is a known meaningful reply.

    "haan", "nahi", "yes", "no", "ok", "theek hai", question words, hang-up
    phrases… — anything the router's signal lexicon or hang-up detection
    recognizes. These must survive every length/duration-based noise rule.
    """
    return bool(text) and (
        classify_user_signal(text) is not None or detect_hangup(text)
    )


def resolve_allowed_languages(stt_settings: dict | None) -> frozenset[str]:
    """Allowed STT languages for one bot: platform default, per-bot override.

    ``stt_settings.allowed_languages`` (list of locale or base codes) narrows
    or widens the platform set; junk entries are ignored so a typo can never
    disable the gate or reject every caller.
    """
    raw = (stt_settings or {}).get("allowed_languages")
    if isinstance(raw, (list, tuple, set)):
        cleaned = {code for code in (base_language(item) for item in raw) if code}
        if cleaned:
            return frozenset(cleaned)
    return ALLOWED_STT_LANGUAGES


def assess_transcript(
    text: str,
    quality: SegmentQuality | None = None,
    allowed_languages: frozenset[str] = ALLOWED_STT_LANGUAGES,
) -> GateVerdict:
    """Decide whether one final STT segment is a real caller utterance.

    Rejections need positive evidence of noise or a foreign language; absent
    metadata the gate falls back to script analysis alone and otherwise
    accepts (fail open — dropping real speech is worse than answering noise).
    """
    quality = quality or SegmentQuality()
    stripped = (text or "").strip()
    if not stripped:
        return GateVerdict(False, "empty")

    language = base_language(quality.language)
    short_reply = meaningful_short_reply(stripped)

    # 1. The STT itself says this was not speech (Whisper no_speech_prob).
    if (
        quality.no_speech_prob is not None
        and quality.no_speech_prob >= NO_SPEECH_REJECT_PROBABILITY
    ):
        return GateVerdict(False, "no_speech", language)

    # 2. Transcript confidence so low the text is noise on any provider.
    if (
        quality.confidence is not None
        and quality.confidence < MIN_TRANSCRIPT_CONFIDENCE
    ):
        return GateVerdict(False, "low_confidence", language)

    # 3. Script evidence: hi/en/Hinglish is Devanagari and/or Latin, so text
    # dominated by any other script is a hallucination or misdetection.
    foreign_chars, foreign_share = _foreign_script_share(stripped, allowed_languages)
    if foreign_chars and foreign_share >= FOREIGN_SCRIPT_DOMINANCE:
        return GateVerdict(False, "unsupported_script", language)

    # 4. Language label outside the allowed set: reject when the text does
    # not read as an allowed language; a confident label also outweighs bare
    # romanized text (translit/codemix modes write everything in Latin).
    if language is not None and language not in allowed_languages:
        reads_allowed = any(
            script_supports_language(stripped, base) for base in allowed_languages
        )
        if not reads_allowed:
            return GateVerdict(False, "unsupported_language", language)
        has_allowed_native_script = any(
            len(pattern.findall(stripped)) > 0
            for base, pattern in _SCRIPT_PATTERNS.items()
            if base in allowed_languages
        )
        if (
            not has_allowed_native_script
            and quality.language_probability is not None
            and quality.language_probability >= FOREIGN_LABEL_PROBABILITY
            and not short_reply
            and not _HINGLISH_HINTS.search(stripped)
        ):
            return GateVerdict(False, "unsupported_language", language)

    # 5. Duration-based noise rules (never applied to known short replies).
    if quality.audio_seconds is not None and quality.audio_seconds > 0:
        words = len(stripped.split())
        if quality.audio_seconds < MIN_UTTERANCE_SECONDS and not short_reply:
            return GateVerdict(False, "noise_duration", language)
        if (
            words >= _RATE_CHECK_MIN_WORDS
            and words / quality.audio_seconds > MAX_WORDS_PER_SECOND
        ):
            return GateVerdict(False, "impossible_rate", language)

    return GateVerdict(True, "ok", language)
