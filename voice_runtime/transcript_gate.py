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
4. CORROBORATED weak evidence rejects: signals that each fire on real speech
   too often to trust alone (speech barely above this line's noise floor, the
   bot speaking while the audio was captured, middling provider confidence,
   sub-word duration, a final that disagrees with its own partials, a context
   free lone token) are counted, and only a pair rejects
   (:func:`weak_noise_signals`);
5. everything else is ACCEPTED — the gate fails open: a transcript with no
   metadata and a plausible script is treated as a real utterance.

Legitimate short replies ("haan", "nahi", "yes", "no", "ok", "नहीं") are
protected by the meaningful-short-reply lexicon (the router's signal patterns
plus hang-up detection), never by a minimum-length rule.
"""

import logging
import re
from dataclasses import dataclass

from shared.orchestration.router import classify_user_signal, detect_hangup
from shared.orchestration.spoken_numbers import pure_digit_payload
from voice_runtime.endpointing import is_short_complete_reply

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

# ── weak-evidence combination ───────────────────────────────────────────────
# None of the signals below is trustworthy alone — each one also fires on real
# speech often enough that rejecting on it would drop genuine callers. They are
# counted instead, and only a CORROBORATED pair rejects. This is what keeps the
# gate from degenerating into "short transcripts are noise": duration is one
# vote among several, never the rule.
WEAK_EVIDENCE_REJECT_COUNT = 2
# Speech this close to the line's own noise floor is as likely to be the floor.
WEAK_SNR_DB = 6.0
# Above MIN_TRANSCRIPT_CONFIDENCE (which rejects outright) but well below what
# clean speech scores.
WEAK_CONFIDENCE = 0.6
# Below the duration of a spoken word plus VAD padding, but not impossibly so.
WEAK_DURATION_SECONDS = 0.45
# A final sharing almost nothing with the partials that preceded it is a
# revision, a translation, or a hallucination — only the last is noise, so this
# never votes alone. (Translate-mode STT legitimately rewrites the text, which
# is precisely why this is a weak signal.)
WEAK_INTERIM_AGREEMENT = 0.25
# A segment this far above the line's measured noise floor was heard clearly;
# speaker echo reaching the mic does not sit 12 dB over the floor, so a clear
# lone word captured during bot audio is the caller answering promptly, not
# bleed. Used to break the single_token+bot_audio_overlap pair that otherwise
# rejects exactly the data answers a collections flow asks for.
HEALTHY_SNR_DB = 12.0
# Bare numeric/reference answers ("5000", "12,500.50", "22/08") are
# self-contained data the flow explicitly asked for — a hallucination out of
# noise essentially never takes this shape, so they are exempt from the
# weak-evidence pair the same way lexicon short replies are.
_DATA_TOKEN_RE = re.compile(r"\d[\d,./-]*")

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
# ── short-utterance script rescue ───────────────────────────────────────────
# Sarvam in language auto-detect mode regularly mislabels SHORT Hindi
# interjections as another Indian language and then writes them in that
# language's script: a caller's "hmm" comes back as Punjabi "ਹਮ।", "okay ji"
# as "ਓਕੇ ਜੀ।". Rejecting those as unsupported_script silently drops a real
# caller turn — the single worst failure a collections call can have, because
# the workflow then re-reads its pitch as if the caller had said nothing.
#
# The northern Indic scripts are ISCII-aligned with Devanagari (same layout,
# fixed block offset), so a per-character offset transliteration recovers the
# Hindi the caller actually spoke well enough for the router/LLM. Applied
# ONLY to utterances of up to _TRANSLITERATE_MAX_WORDS words — long foreign-
# script text is far more likely to be a genuinely foreign speaker (which the
# language_unsupported flow must keep handling) or a hallucinated sentence.
_TRANSLITERATABLE_BLOCKS = (0x0980, 0x0A00, 0x0A80, 0x0B00)  # bn, pa, gu, or
_DEVANAGARI_BASE = 0x0900
_TRANSLITERATE_MAX_WORDS = 6

# Identifier-context digit rescue (see assess_transcript): a misdetected
# digit word is admitted only when the audio gate heard it clearly — a weak
# segment stays rejected, so line noise can never inject digits into an id.
_DIGIT_RESCUE_MIN_SNR_DB = 10.0


def transliterate_to_devanagari(text: str) -> str:
    """Offset-map Bengali/Gurmukhi/Gujarati/Oriya glyphs onto Devanagari."""
    out: list[str] = []
    for ch in text:
        point = ord(ch)
        for base in _TRANSLITERATABLE_BLOCKS:
            if base <= point < base + 0x80:
                out.append(chr(_DEVANAGARI_BASE + (point - base)))
                break
        else:
            out.append(ch)
    return "".join(out)


# Romanized-Hindi (Hinglish) marker words: when the STT reports Hindi but the
# text is fully Latin (translit/codemix STT modes), these confirm the STT's
# verdict so a Hinglish speaker still gets Hindi replies and a Hindi voice.
_HINGLISH_HINTS = re.compile(
    r"\b(haa?n|nahin?|nhi|abhi|aaj|paisa|paise|rupay[ae]?|bhai|"
    r"theek|thik|karo|karu(?:nga|ngi)?|kar (?:do|de|dunga|dungi)|hai|hain|"
    r"mera|mere|meri|aap|kyun?|kaise|kitna|batao|bolo|dijiye)\b",
    re.I,
)

# ── romanized-utterance language leaning ─────────────────────────────────────
# For all-Latin text the script carries no signal, so the DOMINANT lexicon of
# the utterance decides between Hindi/Hinglish and English. Both lexicons are
# deliberately function/grammar words: business loan-words ("payment", "UPI",
# "loan", "account", "overdue", "amount", "date") appear equally in both
# languages and must never vote — that is exactly the borrowed-word flip this
# guard exists to prevent.
_ROMAN_HINDI_TOKENS = frozenset({
    "haan", "han", "ha", "hanji", "ji", "nahi", "nahin", "nhi", "na", "mat",
    "abhi", "aaj", "kal", "parso", "bhai", "bhaiya", "yaar", "sahab",
    "theek", "thik", "accha", "acha", "achha", "sahi", "bilkul",
    "kar", "karo", "karu", "karunga", "karungi", "karenge", "kiya", "karta",
    "karti", "sakta", "sakti", "sakte", "hoga", "hogi", "honge", "hona",
    "hai", "hain", "tha", "thi", "the", "ho", "hu", "hun", "hoon",
    "mera", "mere", "meri", "mujhe", "mujhko", "main", "mai", "hum", "humko",
    "aap", "aapka", "aapki", "aapko", "tum", "tumhara", "tera", "teri",
    "kya", "kyun", "kyu", "kaise", "kaisa", "kitna", "kitni", "kab", "kahan",
    "batao", "bata", "bolo", "bol", "boliye", "dijiye", "dedo", "dena",
    "paisa", "paise", "rupaye", "rupay", "rupaiya",
    "se", "ka", "ki", "ke", "ko", "me", "mein", "par", "pe",
    "aur", "lekin", "magar", "phir", "fir", "toh", "to", "wala", "wali",
    "raha", "rahi", "rahe", "gaya", "gayi", "gaye", "diya", "liya", "lena",
    "milega", "milegi", "chahiye", "zarur", "jarur", "pakka", "baad",
})
_ROMAN_ENGLISH_TOKENS = frozenset({
    "i", "am", "is", "are", "was", "were", "be", "been", "being",
    "the", "a", "an", "this", "that", "these", "those", "it", "its",
    "you", "your", "yours", "we", "our", "they", "their", "he", "she",
    "my", "me", "mine", "us",
    "can", "cant", "cannot", "could", "will", "wont", "would", "shall",
    "should", "may", "might", "must", "do", "dont", "does", "doesnt",
    "did", "didnt", "have", "havent", "has", "hasnt", "had", "hadnt",
    "not", "no", "yes", "yeah", "okay", "ok",
    "and", "or", "but", "if", "then", "so", "because", "when", "while",
    "what", "why", "how", "where", "who", "which",
    "please", "tell", "speak", "speaking", "know", "want", "need",
    "now", "today", "tomorrow", "later", "soon", "next", "week", "month",
    "money", "pay", "paying", "call", "calling", "back", "again",
    "for", "from", "with", "about", "of", "in", "on", "at", "to", "by",
})


def romanized_language_leaning(text: str) -> str | None:
    """Dominant language of an all-Latin utterance: "hi", "en", or None.

    None means "no verdict" — the text carries native script (the script
    heuristics decide), or the lexical evidence is absent/tied. A None must
    always be treated as "trust the STT label", never as a vote.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    # Native script present → script evidence outranks lexicon counting.
    for pattern in _SCRIPT_PATTERNS.values():
        if pattern.search(stripped):
            return None
    tokens = re.findall(r"[a-z']+", stripped.lower())
    if not tokens:
        return None
    hindi = sum(1 for token in tokens if token.replace("'", "") in _ROMAN_HINDI_TOKENS)
    english = sum(
        1 for token in tokens if token.replace("'", "") in _ROMAN_ENGLISH_TOKENS
    )
    if hindi == english:
        return None
    return "hi" if hindi > english else "en"


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

    The last three fields are measured by EchoSphere itself rather than by a
    provider: the caller-audio gate supplies the segment's speech level and the
    noise floor it was measured against (so "loud" is relative to THIS line),
    whether the bot was speaking while the audio was captured (echo risk), and
    the brain supplies how well the final agrees with the partial hypotheses
    that preceded it.
    """

    provider: str = ""
    language: str | None = None  # raw provider code ("hi-IN", "ta", …)
    language_probability: float | None = None
    confidence: float | None = None
    no_speech_prob: float | None = None
    audio_seconds: float | None = None
    snr_db: float | None = None  # speech level above the measured noise floor
    during_bot_audio: bool = False  # captured while the bot was speaking
    interim_agreement: float | None = None  # 0..1 token overlap with partials


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
        if not wire_language:
            # Deepgram Flux TurnInfo reports per-turn detections as a list.
            languages = data.get("languages")
            if isinstance(languages, list) and languages:
                wire_language = languages[0]
        if wire_language:
            quality.language = str(wire_language)
        quality.language_probability = _as_float(data.get("language_probability"))
        quality.confidence = _as_float(data.get("confidence"))
        if quality.confidence is None:
            # Flux reports confidence per word; the mean is the segment's.
            words = data.get("words")
            if isinstance(words, list) and words:
                values = [
                    _as_float(word.get("confidence"))
                    for word in words
                    if isinstance(word, dict)
                ]
                values = [value for value in values if value is not None]
                if values:
                    quality.confidence = sum(values) / len(values)
        quality.no_speech_prob = _as_float(data.get("no_speech_prob"))
        metrics = data.get("metrics")
        quality.audio_seconds = _as_float(data.get("audio_seconds"))
        if quality.audio_seconds is None and isinstance(metrics, dict):
            quality.audio_seconds = _as_float(metrics.get("audio_duration"))
        if quality.audio_seconds is None:
            # Flux: the turn's audio window brackets the utterance.
            start = _as_float(data.get("audio_window_start"))
            end = _as_float(data.get("audio_window_end"))
            if start is not None and end is not None and end > start:
                quality.audio_seconds = end - start
    return quality


# ── the gate ────────────────────────────────────────────────────────────────


@dataclass
class GateVerdict:
    accepted: bool
    reason: str = "ok"
    language: str | None = None  # normalized base code of the detected language
    # Set when the gate rescued a short misdetected segment by transliterating
    # it into an allowed script — the brain must use THIS text for the turn.
    normalized_text: str | None = None


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
    *,
    numeric_context: bool = False,
) -> GateVerdict:
    """Decide whether one final STT segment is a real caller utterance.

    Rejections need positive evidence of noise or a foreign language; absent
    metadata the gate falls back to script analysis alone and otherwise
    accepts (fail open — dropping real speech is worse than answering noise).

    ``numeric_context`` is set by the brain ONLY while an active workflow is
    collecting a numeric identifier: a short segment whose every token is a
    known digit word may then be admitted as its ASCII digits even when
    auto-detection labelled it an unsupported language/script (Gujarati "સાત"
    → "7"). The digit lexicon is strict, the audio-quality checks above still
    apply, and anything carrying a non-number word stays subject to the
    normal rules — so arbitrary unsupported-language sentences remain
    rejected and the conversation language never follows a digit payload.
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

    def _digit_rescue() -> GateVerdict | None:
        """Identifier dictation rescue for segments the gate would REJECT.

        A clearly-heard segment made ONLY of digit-lexicon words is numeric
        payload regardless of what language auto-detection guessed — while
        (and only while) a workflow is collecting a numeric identifier.
        Normalized to ASCII digits so no downstream script/language analysis
        can misread it and it can never switch the conversation language.
        """
        if not numeric_context:
            return None
        if quality.snr_db is not None and quality.snr_db < _DIGIT_RESCUE_MIN_SNR_DB:
            return None
        digits = pure_digit_payload(stripped)
        if not digits:
            return None
        return GateVerdict(True, "digit_payload", language, normalized_text=digits)

    # 3. Script evidence: hi/en/Hinglish is Devanagari and/or Latin, so text
    # dominated by any other script is a hallucination or misdetection.
    foreign_chars, foreign_share = _foreign_script_share(stripped, allowed_languages)
    if foreign_chars and foreign_share >= FOREIGN_SCRIPT_DOMINANCE:
        rescued = _digit_rescue()
        if rescued is not None:
            return rescued
        # Short-utterance rescue: an auto-detect mislabel writes a real Hindi
        # interjection in the mislabeled language's script ("ਹਮ।", "ਓਕੇ ਜੀ।").
        # Recover it by offset transliteration instead of dropping the turn.
        if (
            "hi" in allowed_languages
            and len(stripped.split()) <= _TRANSLITERATE_MAX_WORDS
        ):
            recovered = transliterate_to_devanagari(stripped)
            if (
                recovered != stripped
                and script_supports_language(recovered, "hi")
                # Only rescue text that READS as a known meaningful reply
                # after transliteration ("हम।", "ओके जी।", "हाँ जी") — an
                # arbitrary short foreign sentence stays rejected, so real
                # unsupported-language callers still get the notice flow.
                and (
                    meaningful_short_reply(recovered)
                    or is_short_complete_reply(recovered)
                )
            ):
                return GateVerdict(
                    True, "transliterated_short_reply", language,
                    normalized_text=recovered,
                )
        return GateVerdict(False, "unsupported_script", language)

    # 4. Language label outside the allowed set: reject when the text does
    # not read as an allowed language; a confident label also outweighs bare
    # romanized text (translit/codemix modes write everything in Latin).
    if language is not None and language not in allowed_languages:
        rescued = _digit_rescue()
        if rescued is not None:
            return rescued
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

    # 5. Duration-based noise rules (never applied to known short replies or
    # to dictated numbers — "six zero" answering an ID prompt is real speech).
    if quality.audio_seconds is not None and quality.audio_seconds > 0:
        words = len(stripped.split())
        if (
            quality.audio_seconds < MIN_UTTERANCE_SECONDS
            and not short_reply
            and not _is_dictated_number(stripped)
        ):
            return GateVerdict(False, "noise_duration", language)
        if (
            words >= _RATE_CHECK_MIN_WORDS
            and words / quality.audio_seconds > MAX_WORDS_PER_SECOND
            # Digit tokens are not words: "6 0 1 0 1 1" counts six "words"
            # in barely a second of real dictation. The rate rule hunts
            # hallucinated text bursts, which digit sequences are not.
            and not _is_dictated_number(stripped)
        ):
            return GateVerdict(False, "impossible_rate", language)

    # 6. Corroborated weak evidence: no single signal below is decisive, so a
    # pair of independent ones is required.
    #
    # The exemption here is deliberately NARROWER than `short_reply` above:
    # that one matches the router's semantic signals anywhere in a sentence
    # (so "मैं कल पेमेंट कर दूंगा" counts as one), which would exempt almost
    # every meaningful-looking hallucination and leave this rule inert. What
    # must be protected is the genuinely self-contained reply — "haan", "nahi",
    # "ji", "yes", "no", "ok" — which trips several of these signals by nature.
    if (
        not is_short_complete_reply(stripped)
        and not _is_data_token(stripped)
        and not _is_dictated_number(stripped)
    ):
        weak = weak_noise_signals(stripped, quality)
        if (
            "single_token" in weak
            and "bot_audio_overlap" in weak
            and quality.snr_db is not None
            and quality.snr_db >= HEALTHY_SNR_DB
        ):
            # A clearly-heard lone word during (or just after) bot audio is a
            # prompt answer, not echo: names, amounts and references land
            # exactly here. The overlap signal keeps voting when the audio was
            # ALSO quiet — genuine bleed sits near the floor.
            weak.remove("bot_audio_overlap")
        if len(weak) >= WEAK_EVIDENCE_REJECT_COUNT:
            return GateVerdict(False, "weak_signal:" + "+".join(weak), language)

    return GateVerdict(True, "ok", language)


def _is_data_token(text: str) -> bool:
    """Whether the utterance is a bare numeric/reference token ("5000")."""
    return bool(_DATA_TOKEN_RE.fullmatch(text))


def _is_dictated_number(text: str) -> bool:
    """Whether the utterance is a spoken number/identifier being dictated.

    "six zero", "double one", "छह शून्य एक", "6 0 1 0 1 1" — a caller reading
    a booking ID/OTP/amount digit-by-digit in any supported language. Such
    answers are short and often overlap the tail of the bot's own question,
    so they need the same protection from the duration/weak-evidence noise
    rules as lexicon short replies and bare data tokens.
    """
    from shared.orchestration.spoken_numbers import digits_dominant

    return digits_dominant(text)


def weak_noise_signals(text: str, quality: SegmentQuality) -> list[str]:
    """Names of the weak noise indicators this segment trips.

    Exposed separately so diagnostics can record exactly which signals
    corroborated a rejection without storing audio.
    """
    signals: list[str] = []
    if quality.snr_db is not None and quality.snr_db < WEAK_SNR_DB:
        signals.append("low_snr")
    if quality.during_bot_audio:
        # The bot was speaking while this audio was captured, so speaker
        # bleed/echo is a live alternative explanation for the transcript.
        signals.append("bot_audio_overlap")
    if (
        quality.confidence is not None
        and quality.confidence < WEAK_CONFIDENCE
    ):
        signals.append("low_confidence")
    if (
        quality.audio_seconds is not None
        and 0 < quality.audio_seconds < WEAK_DURATION_SECONDS
    ):
        signals.append("short_audio")
    if (
        quality.interim_agreement is not None
        and quality.interim_agreement < WEAK_INTERIM_AGREEMENT
    ):
        signals.append("unstable_transcript")
    if len(text.split()) <= 1:
        # A lone token carries no linguistic context to sanity-check against;
        # meaningful one-word replies are already exempt by the caller.
        signals.append("single_token")
    return signals
