"""Provider model/voice catalog seed (initial data only — editable via master data).

Populates:
- provider_models: models, supported languages, codecs, sample rates and the
  parameter schema that drives provider-specific configuration UI + validation.
- voice_profiles: ElevenLabs voices and Sarvam bulbul:v3 speakers.

Language codes inside PROVIDER_MODELS use the provider's wire form (Sarvam:
``od-IN``; ElevenLabs v2.5 family: bare ISO 639-1). ``shared.providers.
languages`` maps them to platform locale codes. Voice rows use platform
locale codes.

Exception: ``eleven_v3`` stores PLATFORM locale codes (en-US, en-IN, hi-IN …)
derived from the ``supported_languages`` catalog — the languages table is the
source of truth for what tenants may pick, so the model row only ever lists
locales that exist as catalog records AND are officially supported by the
model. Enable/disable state is applied at read time (model_platform_languages
serves enabled rows only). Locale codes are never sent to ElevenLabs for v3 —
the model takes no language_code parameter.

No API keys anywhere — providers reference credentials via ``env:`` secret
references on provider_defs.
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.ids import new_id
from shared.models import (
    AiConfigProfile,
    ApprovedModel,
    ProviderDef,
    ProviderModel,
    SupportedLanguage,
    VoiceProfile,
)

# ── Parameter schemas ────────────────────────────────────────────────────────
# {"field": {"type": number|integer|boolean|enum|string|int_list|string_list,
#            "min","max","step","default","values","label","help","advanced"}}

# Deepgram Flux (conversational STT, /v2/listen): only the parameters the
# voice-agent runtime actually consumes are exposed — encoding/sample-rate are
# decided by the call transport, and the raw API surface stays out of the UI.
_DEEPGRAM_FLUX_SCHEMA = {
    "eot_threshold": {
        "type": "number", "min": 0.5, "max": 0.9, "default": 0.7, "step": 0.05,
        "label": "End-of-turn threshold",
        "help": "Confidence Flux needs before ending the caller's turn. "
                "Lower answers sooner but may cut into natural pauses.",
    },
    "eager_eot_threshold": {
        "type": "number", "min": 0.3, "max": 0.9, "default": 0.6, "step": 0.05,
        "label": "Eager end-of-turn threshold", "advanced": True,
        "help": "Enables EagerEndOfTurn: orchestration starts speculatively "
                "before the turn is confirmed (lower = faster responses, more "
                "speculative decision calls).",
    },
    "eot_timeout_ms": {
        "type": "integer", "min": 500, "max": 60000, "default": 3000,
        "label": "End-of-turn timeout (ms)", "advanced": True,
        "help": "Silence after speech that force-ends the turn regardless of "
                "end-of-turn confidence.",
    },
    "language_hints": {
        # Languages flux-general-multi supports (pipecat wire codes).
        "type": "string_list", "max_items": 8, "max_length": 8,
        "values": ["de", "en", "es", "fr", "hi", "it", "ja", "nl", "pt", "ru"],
        "label": "Language hints", "advanced": True,
        "help": "Bias multilingual detection toward these languages. Defaults "
                "to the bot's configured languages (e.g. hi, en).",
    },
}

_SARVAM_STT_COMMON = {
    "vad_signals": {
        "type": "boolean", "default": True, "label": "VAD signals",
        "help": "Emit start/end-of-speech events from Sarvam's server-side VAD.",
    },
    "high_vad_sensitivity": {
        "type": "boolean", "default": False, "label": "High VAD sensitivity",
        "help": "Finalize segments after ~0.5s of silence instead of ~1s (faster endpointing).",
    },
    "input_encoding": {
        # "wav" is the only encoding the pinned sarvamai SDK (0.1.28) accepts
        # on the streaming socket — "pcm_s16le" fails per-chunk validation.
        "type": "enum", "values": ["pcm_s16le", "wav"], "default": "wav",
        "label": "Input encoding", "advanced": True,
        "help": "Wire encoding for microphone/telephony audio sent to Sarvam.",
    },
    "timeout_seconds": {
        "type": "number", "min": 5, "max": 120, "default": 30, "step": 1,
        "label": "Timeout (s)", "advanced": True,
        "help": "Connection/response timeout before the turn is failed.",
    },
}

_SAARAS_V3_SCHEMA = {
    "mode": {
        "type": "enum", "values": ["transcribe", "verbatim", "translit", "codemix", "translate"],
        "default": "transcribe", "label": "Mode",
        "help": "saaras:v3 output mode. 'transcribe' is standard; 'translate' returns English.",
    },
    **_SARVAM_STT_COMMON,
    "positive_speech_threshold": {
        "type": "number", "min": 0.0, "max": 1.0, "default": 0.7, "step": 0.05,
        "label": "Speech threshold", "advanced": True,
        "help": "VAD probability above which a frame counts as speech.",
    },
    "negative_speech_threshold": {
        "type": "number", "min": 0.0, "max": 1.0, "default": 0.45, "step": 0.05,
        "label": "Silence threshold", "advanced": True,
        "help": "VAD probability below which a frame counts as silence.",
    },
    "min_speech_frames": {
        "type": "integer", "min": 1, "max": 50, "default": 2,
        "label": "Min speech frames", "advanced": True,
        "help": "Consecutive speech frames required to open a segment.",
    },
    "interrupt_min_speech_frames": {
        "type": "integer", "min": 1, "max": 50, "default": 2,
        "label": "Barge-in min frames", "advanced": True,
        "help": "Speech frames required to register a barge-in.",
    },
}

_SARVAM_TTS_V3_SCHEMA = {
    "pace": {
        "type": "number", "min": 0.5, "max": 2.0, "default": 1.0, "step": 0.05,
        "label": "Pace", "help": "Speech speed multiplier (bulbul:v3 range 0.5–2.0).",
    },
    "temperature": {
        "type": "number", "min": 0.01, "max": 1.0, "default": 0.6, "step": 0.01,
        "label": "Temperature",
        "help": "Synthesis randomness — lower is more deterministic (bulbul:v3 only).",
    },
    "min_buffer_size": {
        "type": "integer", "min": 10, "max": 500, "default": 40,
        "label": "Min buffer size",
        "help": "Characters buffered before audio generation starts. 30–40 recommended for realtime.",
    },
    "max_chunk_length": {
        "type": "integer", "min": 50, "max": 500, "default": 150,
        "label": "Max chunk length",
        "help": "Maximum characters per synthesis chunk. 120–150 recommended for realtime.",
    },
    "dict_id": {
        "type": "string", "default": None, "optional": True, "max_length": 100,
        "label": "Pronunciation dictionary ID", "advanced": True,
        "help": "Optional Sarvam pronunciation dictionary applied during synthesis.",
    },
    "send_completion_event": {
        "type": "boolean", "default": True, "label": "Completion event", "advanced": True,
        "help": "Ask Sarvam to signal when synthesis of flushed text has finished.",
    },
    # bulbul:v3 always enables preprocessing server-side; exposed read-only so
    # the UI can say so without offering a dead toggle.
    "enable_preprocessing": {
        "type": "boolean", "default": True, "fixed": True, "label": "Preprocessing",
        "help": "Text normalization before synthesis. Always enabled for bulbul:v3.",
    },
}

_SARVAM_TTS_V2_SCHEMA = {
    "pace": {
        "type": "number", "min": 0.3, "max": 3.0, "default": 1.0, "step": 0.05,
        "label": "Pace", "help": "Speech speed multiplier (bulbul:v2 range 0.3–3.0).",
    },
    "pitch": {
        "type": "number", "min": -0.75, "max": 0.75, "default": 0.0, "step": 0.05,
        "label": "Pitch", "help": "Voice pitch adjustment (bulbul:v2 only).",
    },
    "loudness": {
        "type": "number", "min": 0.3, "max": 3.0, "default": 1.0, "step": 0.1,
        "label": "Loudness", "help": "Volume multiplier (bulbul:v2 only).",
    },
    "enable_preprocessing": {
        "type": "boolean", "default": False, "label": "Preprocessing",
        "help": "Enable text normalization before synthesis.",
    },
    "min_buffer_size": _SARVAM_TTS_V3_SCHEMA["min_buffer_size"],
    "max_chunk_length": _SARVAM_TTS_V3_SCHEMA["max_chunk_length"],
    "send_completion_event": _SARVAM_TTS_V3_SCHEMA["send_completion_event"],
}

# Eleven v3 (alpha) exposes only the documented v3 voice settings: discrete
# stability presets plus similarity/style. speed, use_speaker_boost and the
# realtime WebSocket knobs (auto_mode, chunk_length_schedule, …) are not
# supported by the model and are never offered nor sent.
_ELEVENLABS_V3_TTS_SCHEMA = {
    "stability": {
        "type": "enum", "values": [0.0, 0.5, 1.0], "default": 0.5,
        # Keys use the JS String() form of the parsed numbers (0.0 → "0").
        "labels": {"0": "Creative", "0.5": "Natural", "1": "Robust"},
        "label": "Stability",
        "help": "Eleven v3 accepts three presets: Creative (expressive, may "
                "hallucinate), Natural (balanced) or Robust (very stable).",
    },
    "similarity_boost": {
        "type": "number", "min": 0.0, "max": 1.0, "default": 1.0, "step": 0.05,
        "label": "Similarity boost",
        "help": "How closely synthesis adheres to the original voice timbre.",
    },
    "style": {
        "type": "number", "min": 0.0, "max": 1.0, "default": 0.0, "step": 0.05,
        "label": "Style", "advanced": True,
        "help": "Style exaggeration. 0 is fastest and most stable.",
    },
}

_ELEVENLABS_TTS_SCHEMA = {
    "stability": {
        "type": "number", "min": 0.0, "max": 1.0, "default": 0.0, "step": 0.05,
        "label": "Stability",
        "help": "Lower values give more expressive, varied delivery; higher values are steadier.",
    },
    "similarity_boost": {
        "type": "number", "min": 0.0, "max": 1.0, "default": 1.0, "step": 0.05,
        "label": "Similarity boost",
        "help": "How closely synthesis adheres to the original voice timbre.",
    },
    "style": {
        "type": "number", "min": 0.0, "max": 1.0, "default": 0.0, "step": 0.05,
        "label": "Style",
        "help": "Style exaggeration. 0 is fastest and most stable.",
    },
    "use_speaker_boost": {
        "type": "boolean", "default": True, "label": "Speaker boost",
        "help": "Boosts similarity to the original speaker at a small latency cost.",
    },
    "speed": {
        "type": "number", "min": 0.7, "max": 1.2, "default": 1.0, "step": 0.05,
        "label": "Speed", "help": "Playback speed multiplier.",
    },
    "chunk_length_schedule": {
        "type": "int_list", "default": [120, 160, 250, 290], "min": 50, "max": 500,
        "max_items": 8, "label": "Chunk schedule", "advanced": True,
        "help": "Character thresholds that trigger generation when auto mode is off.",
    },
    "auto_mode": {
        "type": "boolean", "default": True, "label": "Auto mode",
        "help": "Generate immediately per sentence instead of server-side buffering. "
                "Recommended when sending complete sentences (this platform does).",
    },
    "inactivity_timeout": {
        "type": "integer", "min": 20, "max": 180, "default": 60,
        "label": "Inactivity timeout (s)", "advanced": True,
        "help": "Server-side idle timeout for the streaming connection.",
    },
    "sync_alignment": {
        "type": "boolean", "default": False, "label": "Sync alignment", "advanced": True,
        "help": "Deliver character timing data in sync with audio chunks.",
    },
    "apply_text_normalization": {
        "type": "enum", "values": ["auto", "on", "off"], "default": "auto",
        "label": "Text normalization", "advanced": True,
        "help": "Normalize numbers/abbreviations before synthesis.",
    },
}

_OPENAI_LLM_SCHEMA = {
    "temperature": {
        "type": "number", "min": 0.0, "max": 2.0, "default": 0.3, "step": 0.05,
        "label": "Temperature", "help": "Response randomness. Keep low for voice bots.",
    },
    "max_tokens": {
        "type": "integer", "min": 16, "max": 4096, "default": 256,
        "label": "Max output tokens",
        "help": "Upper bound per reply. Voice replies should stay short.",
    },
    "timeout_seconds": {
        "type": "number", "min": 5, "max": 120, "default": 30, "step": 1,
        "label": "Timeout (s)", "advanced": True,
        "help": "Per-request timeout before retry/fallback handling.",
    },
    "streaming": {
        "type": "boolean", "default": True, "fixed": True, "label": "Streaming",
        "help": "Token streaming into the sentence buffer. Always on for realtime voice.",
    },
    "max_retries": {
        "type": "integer", "min": 0, "max": 5, "default": 1,
        "label": "Max retries", "advanced": True,
        "help": "Automatic retries on transient failures.",
    },
}

# Sarvam wire language codes (bulbul:v3 supported set — note od-IN for Odia).
_SARVAM_TTS_LANGS = [
    "hi-IN", "en-IN", "bn-IN", "mr-IN", "gu-IN", "ta-IN",
    "te-IN", "kn-IN", "ml-IN", "pa-IN", "od-IN",
]
_SAARIKA_LANGS = ["unknown"] + _SARVAM_TTS_LANGS
_SAARAS_V3_LANGS = _SAARIKA_LANGS + [
    "as-IN", "ur-IN", "ne-IN", "kok-IN", "ks-IN", "sd-IN",
    "sa-IN", "sat-IN", "mni-IN", "brx-IN", "mai-IN", "doi-IN",
]

# ElevenLabs Flash/Turbo v2.5 languages (bare ISO 639-1, 32 languages).
_ELEVEN_V2_5_LANGS = [
    "en", "hi", "ta", "ja", "zh", "de", "fr", "ko", "pt", "it", "es", "id",
    "nl", "tr", "fil", "pl", "sv", "bg", "ro", "ar", "cs", "el", "fi", "hr",
    "ms", "sk", "da", "uk", "ru", "hu", "no", "vi",
]

# Official Eleven v3 supported languages as base ISO codes (639-1 where one
# exists; "fil"/"ceb" have no two-letter form) — verified against
# elevenlabs.io/docs models page 2026-07-30. Note: no Odia. This is the
# PROVIDER capability matrix only; what tenants can actually pick is derived
# below by intersecting it with the platform language catalog
# (supported_languages), which stays the source of truth.
ELEVEN_V3_ISO_CODES = frozenset({
    "af", "ar", "hy", "as", "az", "be", "bn", "bs", "bg", "ca", "ceb", "ny",
    "hr", "cs", "da", "nl", "en", "et", "fil", "fi", "fr", "gl", "ka", "de",
    "el", "gu", "ha", "he", "hi", "hu", "is", "id", "ga", "it", "ja", "jv",
    "kn", "kk", "ky", "ko", "lv", "ln", "lt", "lb", "mk", "ms", "ml", "zh",
    "cmn", "mr", "ne", "no", "ps", "fa", "pl", "pt", "pa", "ro", "ru", "sr",
    "sd", "sk", "sl", "so", "es", "sw", "sv", "ta", "te", "th", "tr", "uk",
    "ur", "vi", "cy",
})


def eleven_v3_platform_locales(languages: Iterable[tuple[str, str | None]]) -> list[str]:
    """Platform locale codes Eleven v3 may offer, from (code, iso_code) pairs.

    A locale qualifies when its canonical base code (``iso_code``, falling
    back to the code's own prefix) is officially supported by the model.
    Locales without a catalog record never qualify — the languages table is
    the source of truth. Order-preserving, duplicate-free.
    """
    locales: list[str] = []
    for code, iso in languages:
        code = (code or "").strip()
        if not code or code in locales:
            continue
        base = (iso or code.split("-")[0]).strip().lower()
        if base in ELEVEN_V3_ISO_CODES:
            locales.append(code)
    return locales


def _eleven_v3_locales_from_db(db: Session) -> list[str]:
    """Derive the offered v3 locales from the live supported_languages table.

    Includes disabled rows on purpose: enable/disable is an availability
    toggle applied at read time (model_platform_languages), not a removal —
    re-enabling a language must not require touching the model row.
    """
    rows = db.execute(
        select(SupportedLanguage.code, SupportedLanguage.iso_code)
        .order_by(SupportedLanguage.sort_order, SupportedLanguage.code)
    ).all()
    return eleven_v3_platform_locales((code, iso) for code, iso in rows)


def _base_seed_language_pairs() -> list[tuple[str, str | None]]:
    # Lazy import: base_seed imports this module inside run_base_seed, so a
    # module-level import back would be circular during that call path.
    from backend.seeds.base_seed import LANGUAGES

    return [(code, iso) for code, _, _, iso, _, _, _ in LANGUAGES]


# Legacy shape of the eleven_v3 row before it became catalog-derived (bare ISO
# codes). Rows still exactly matching it are provably unedited and safe to
# convert; anything else is operator-managed and left alone.
_LEGACY_ELEVEN_V3_BARE_CODES = [
    "af", "ar", "hy", "as", "az", "be", "bn", "bs", "bg", "ca", "ceb", "ny",
    "hr", "cs", "da", "nl", "en", "et", "fil", "fi", "fr", "gl", "ka", "de",
    "el", "gu", "ha", "he", "hi", "hu", "is", "id", "ga", "it", "ja", "jv",
    "kn", "kk", "ky", "ko", "lv", "ln", "lt", "lb", "mk", "ms", "ml", "zh",
    "mr", "ne", "no", "ps", "fa", "pl", "pt", "pa", "ro", "ru", "sr", "sd",
    "sk", "sl", "so", "es", "sw", "sv", "ta", "te", "th", "tr", "uk", "ur",
    "vi", "cy",
]

# Catalog-derived platform locales for fresh inserts (seed data mirrors the
# freshly seeded table; live databases are re-derived from the table itself
# in seed_provider_catalog). Includes en-US and en-IN as separate records.
_ELEVEN_V3_LANGS = eleven_v3_platform_locales(_base_seed_language_pairs())

# Why the whole GPT-5 generation is catalogued inactive: the OpenAI LLM
# provider calls chat.completions with `max_tokens` and temperature 0.3, both
# of which that generation rejects (it takes `max_completion_tokens` and the
# default temperature only). The rows exist so usage of these models can be
# priced and validated; activating one before the provider sends the right
# parameters would hand operators a model that fails on every turn.
_GPT5_NOTE = (
    "{summary}. Catalogued for pricing but inactive: the OpenAI LLM provider "
    "still sends `max_tokens`/temperature, which the GPT-5 family rejects. "
    "Activate only after the provider is updated."
)

# Concise operator-facing model summaries. Applied on insert and backfilled
# onto existing rows only while their description is empty (operator text is
# never overwritten).
MODEL_DESCRIPTIONS: dict[tuple[str, str, str], str] = {
    ("elevenlabs", "tts", "eleven_v3"): (
        "Most expressive ElevenLabs model (alpha): emotional range, audio tags, "
        "70+ languages. High latency, 5,000-character limit, no realtime "
        "streaming — synthesized per reply over REST; previews and non-realtime "
        "use recommended."
    ),
    ("elevenlabs", "tts", "eleven_flash_v2_5"): (
        "Ultra-low-latency model (~75 ms) for realtime conversation over the "
        "streaming WebSocket. 32 languages, 40,000-character limit. "
        "Recommended for live calls."
    ),
    ("elevenlabs", "tts", "eleven_turbo_v2_5"): (
        "Deprecated quality/latency-balance model (32 languages). Superseded "
        "by Eleven Flash v2.5."
    ),
    ("openai", "llm", "gpt-4.1"): (
        "Full GPT-4.1: strongest of the 4.1 family for instruction following "
        "and long context. Higher cost/latency than 4.1 mini — prefer it for "
        "complex reasoning turns rather than every realtime reply."
    ),
    ("openai", "llm", "gpt-4.1-nano"): (
        "Cheapest GPT-4.1 variant and the lowest-latency OpenAI chat model. "
        "Suited to classification, routing and short scripted replies."
    ),
    ("openai", "llm", "gpt-5.6-sol"): _GPT5_NOTE.format(
        summary="Flagship GPT-5.6 model: highest quality of the generation, "
                "priced accordingly"),
    ("openai", "llm", "gpt-5.6-terra"): _GPT5_NOTE.format(
        summary="Mid-tier GPT-5.6 model balancing quality and cost"),
    ("openai", "llm", "gpt-5.6-luna"): _GPT5_NOTE.format(
        summary="Smallest, cheapest GPT-5.6 model for high-volume turns"),
    ("openai", "llm", "gpt-5.1"): _GPT5_NOTE.format(
        summary="GPT-5.1 general-purpose model"),
    ("openai", "llm", "gpt-5"): _GPT5_NOTE.format(
        summary="GPT-5 general-purpose model"),
    ("openai", "llm", "gpt-5-mini"): _GPT5_NOTE.format(
        summary="Cost-reduced GPT-5 variant"),
    ("openai", "llm", "gpt-5-nano"): _GPT5_NOTE.format(
        summary="Smallest and cheapest GPT-5 variant"),
    ("openai", "stt", "gpt-transcribe"): (
        "Current OpenAI batch transcription model and the cheapest of the "
        "family. Inactive under platform governance: STT is Sarvam-only."
    ),
    ("openai", "stt", "gpt-4o-transcribe"): (
        "GPT-4o transcription (batch/REST). Inactive under platform "
        "governance: STT is Sarvam-only."
    ),
    ("openai", "stt", "gpt-4o-mini-transcribe"): (
        "Cheaper GPT-4o mini transcription (batch/REST). Inactive under "
        "platform governance: STT is Sarvam-only."
    ),
    ("deepgram", "stt", "flux-general-multi"): (
        "Deepgram Flux multilingual conversational STT (/v2/listen): "
        "model-integrated turn detection (EndOfTurn / EagerEndOfTurn / "
        "TurnResumed), per-turn language detection. Recommended for "
        "Hindi/Hinglish/English voice agents."
    ),
    ("deepgram", "stt", "flux-general-en"): (
        "Deepgram Flux English-only conversational STT (/v2/listen) with "
        "model-integrated turn detection. Use flux-general-multi for "
        "Hindi/Hinglish callers."
    ),
    ("deepgram", "stt", "nova-3"): (
        "Legacy Deepgram streaming transcription (v1). Superseded for voice "
        "agents by Flux; inactive."
    ),
}

# (provider, capability, code, display, languages, codecs, rates, streaming,
#  schema, is_default, status, sort)
PROVIDER_MODELS = [
    # ── STT ──────────────────────────────────────────────────────────────
    ("sarvam", "stt", "saarika:v2.5", "Saarika v2.5 (streaming)",
     _SAARIKA_LANGS, ["linear16"], [8000, 16000], True,
     _SARVAM_STT_COMMON, False, "active", 1),
    ("sarvam", "stt", "saaras:v3", "Saaras v3 (streaming)",
     _SAARAS_V3_LANGS, ["linear16"], [8000, 16000], True,
     _SAARAS_V3_SCHEMA, True, "active", 0),
    ("mock", "stt", "mock", "Mock STT", [], ["linear16"], [8000, 16000, 24000], True,
     {}, True, "active", 99),
    # ── TTS ──────────────────────────────────────────────────────────────
    ("sarvam", "tts", "bulbul:v3", "Bulbul v3 (streaming)",
     _SARVAM_TTS_LANGS, ["linear16", "mulaw", "alaw"], [8000, 16000, 22050, 24000], True,
     _SARVAM_TTS_V3_SCHEMA, True, "active", 0),
    ("sarvam", "tts", "bulbul:v2", "Bulbul v2 (legacy)",
     _SARVAM_TTS_LANGS, ["linear16", "mulaw", "alaw"], [8000, 16000, 22050, 24000], True,
     _SARVAM_TTS_V2_SCHEMA, False, "inactive", 1),
    ("elevenlabs", "tts", "eleven_flash_v2_5", "Eleven Flash v2.5",
     _ELEVEN_V2_5_LANGS, ["pcm", "ulaw", "alaw"], [8000, 16000, 22050, 24000], True,
     _ELEVENLABS_TTS_SCHEMA, True, "active", 0),
    # Eleven v3 is not supported on the ElevenLabs realtime WebSocket
    # (streaming=False) — live calls synthesize it segment-by-segment over
    # REST; fallback/per-language engines require a streaming model instead.
    ("elevenlabs", "tts", "eleven_v3", "Eleven v3 (expressive)",
     _ELEVEN_V3_LANGS, ["pcm", "ulaw", "alaw"], [8000, 16000, 22050, 24000], False,
     _ELEVENLABS_V3_TTS_SCHEMA, False, "active", 1),
    ("elevenlabs", "tts", "eleven_turbo_v2_5", "Eleven Turbo v2.5 (deprecated)",
     _ELEVEN_V2_5_LANGS, ["pcm", "ulaw", "alaw"], [8000, 16000, 22050, 24000], True,
     _ELEVENLABS_TTS_SCHEMA, False, "inactive", 2),
    ("mock", "tts", "mock", "Mock TTS", [], ["linear16"], [8000, 16000, 24000], True,
     {}, True, "active", 99),
    # OpenAI Whisper — REST/segmented (non-streaming), language auto-detect.
    # Inactive under platform governance: STT is Sarvam-only.
    ("openai", "stt", "whisper-1", "Whisper (whisper-1)",
     [], ["linear16"], [8000, 16000, 24000], False, {}, True, "inactive", 0),
    # Current OpenAI transcription models — same governance as whisper-1
    # (STT is Sarvam-only); catalogued so their usage can be priced.
    ("openai", "stt", "gpt-transcribe", "GPT Transcribe",
     [], ["linear16"], [8000, 16000, 24000], False, {}, False, "inactive", 1),
    ("openai", "stt", "gpt-4o-transcribe", "GPT-4o Transcribe",
     [], ["linear16"], [8000, 16000, 24000], False, {}, False, "inactive", 2),
    ("openai", "stt", "gpt-4o-mini-transcribe", "GPT-4o mini Transcribe",
     [], ["linear16"], [8000, 16000, 24000], False, {}, False, "inactive", 3),
    # Deepgram Flux — conversational realtime STT (/v2/listen) with
    # model-integrated turn detection; the platform's second active STT
    # vendor. flux-general-multi is the default for Hindi/Hinglish/English
    # calling; flux-general-en is English-only.
    ("deepgram", "stt", "flux-general-multi", "Flux (multilingual)",
     [], ["linear16"], [8000, 16000, 24000, 44100, 48000], True,
     _DEEPGRAM_FLUX_SCHEMA, True, "active", 0),
    ("deepgram", "stt", "flux-general-en", "Flux (English)",
     ["en"], ["linear16"], [8000, 16000, 24000, 44100, 48000], True,
     _DEEPGRAM_FLUX_SCHEMA, False, "active", 1),
    # Legacy Deepgram batch/streaming models — inactive; catalogued so their
    # pricing can be configured/validated.
    ("deepgram", "stt", "nova-3", "Nova-3 (streaming)",
     [], ["linear16"], [8000, 16000, 24000], True, {}, False, "inactive", 2),
    ("deepgram", "stt", "nova-2", "Nova-2 (legacy)",
     [], ["linear16"], [8000, 16000, 24000], True, {}, False, "inactive", 3),
    # OpenAI TTS — inactive under platform governance: TTS is Sarvam/ElevenLabs.
    ("openai", "tts", "tts-1", "TTS-1", [], ["linear16"], [24000], False,
     {}, True, "inactive", 0),
    ("openai", "tts", "tts-1-hd", "TTS-1 HD", [], ["linear16"], [24000], False,
     {}, False, "inactive", 1),
    # ── LLM ──────────────────────────────────────────────────────────────
    ("openai", "llm", "gpt-4o-mini", "GPT-4o mini", [], None, None, True,
     _OPENAI_LLM_SCHEMA, True, "active", 0),
    ("openai", "llm", "gpt-4o", "GPT-4o", [], None, None, True,
     _OPENAI_LLM_SCHEMA, False, "active", 1),
    ("openai", "llm", "gpt-4.1-mini", "GPT-4.1 mini", [], None, None, True,
     _OPENAI_LLM_SCHEMA, False, "active", 2),
    ("openai", "llm", "gpt-4.1", "GPT-4.1", [], None, None, True,
     _OPENAI_LLM_SCHEMA, False, "active", 3),
    ("openai", "llm", "gpt-4.1-nano", "GPT-4.1 nano", [], None, None, True,
     _OPENAI_LLM_SCHEMA, False, "active", 4),
    # GPT-5 generation — inactive by design, see _GPT5_NOTE above.
    ("openai", "llm", "gpt-5.6-sol", "GPT-5.6 Sol", [], None, None, True,
     _OPENAI_LLM_SCHEMA, False, "inactive", 5),
    ("openai", "llm", "gpt-5.6-terra", "GPT-5.6 Terra", [], None, None, True,
     _OPENAI_LLM_SCHEMA, False, "inactive", 6),
    ("openai", "llm", "gpt-5.6-luna", "GPT-5.6 Luna", [], None, None, True,
     _OPENAI_LLM_SCHEMA, False, "inactive", 7),
    ("openai", "llm", "gpt-5.1", "GPT-5.1", [], None, None, True,
     _OPENAI_LLM_SCHEMA, False, "inactive", 8),
    ("openai", "llm", "gpt-5", "GPT-5", [], None, None, True,
     _OPENAI_LLM_SCHEMA, False, "inactive", 9),
    ("openai", "llm", "gpt-5-mini", "GPT-5 mini", [], None, None, True,
     _OPENAI_LLM_SCHEMA, False, "inactive", 10),
    ("openai", "llm", "gpt-5-nano", "GPT-5 nano", [], None, None, True,
     _OPENAI_LLM_SCHEMA, False, "inactive", 11),
    ("mock", "llm", "mock", "Mock LLM", [], None, None, True, {}, True, "active", 99),
    # ── Embedding ────────────────────────────────────────────────────────
    ("openai", "embedding", "text-embedding-3-small", "text-embedding-3-small (1536d)",
     [], None, None, False, {}, True, "active", 0),
    ("openai", "embedding", "text-embedding-3-large", "text-embedding-3-large (3072d)",
     [], None, None, False, {}, False, "active", 1),
    ("mock", "embedding", "mock-embedding", "Mock Embeddings (dev)",
     [], None, None, False, {}, True, "active", 99),
]

# Platform locale codes for voice rows (or-IN is the platform form of od-IN).
_SARVAM_VOICE_LOCALES = [
    "hi-IN", "en-IN", "bn-IN", "mr-IN", "gu-IN", "ta-IN",
    "te-IN", "kn-IN", "ml-IN", "pa-IN", "or-IN",
]

_ELEVEN_DEFAULT_VOICE_SETTINGS = {
    "stability": 0.0, "similarity_boost": 1.0, "style": 0.0,
    "use_speaker_boost": True, "speed": 1.0,
}

# (id, name, gender, provider_voice_id)
ELEVENLABS_VOICES = [
    ("vp-el-monika", "Monika", "female", "f1abxvIEijusskcPWE5x"),
    ("vp-el-raju", "Raju", "male", "WQAp2s6GVJHv6IkTFqO0"),
    ("vp-el-niraj", "Niraj", "male", "yD3f554gXhA5NxImkyqU"),
    ("vp-el-leo", "Leo", "male", "TLC61WvtioR7PrhxZ1RH"),
    ("vp-el-viraj", "Viraj", "male", "3AMU7jXQuQa3oRvRqUmb"),
    ("vp-el-shardul", "Shardul", "male", "6EphsklDopDQ6eRkwNHT"),
    ("vp-el-anvi", "Anvi", "female", "VG7gYikNQ71LJ52W9fAD"),
    ("vp-el-shivank", "Shivank", "female", "Vf2PzaME4dMzjUBlO0w0"),
]

# Sarvam bulbul:v3 speakers — wire codes are lowercase; display names Title case.
# Gender labels are catalog data (editable in master data), best-effort here.
# All 37 verified against the live Sarvam API (2026-07-23): every speaker
# returned valid audio for en-IN and hi-IN with bulbul:v3. "niharika" appears
# in Sarvam's own compatibility error message but is rejected with
# "Speaker 'niharika' is not recognized" — do not add it without re-testing.
_SARVAM_FEMALE = {
    "ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita", "shreya",
    "roopa", "tanya", "shruti", "suhani", "kavitha", "rupali",
}
SARVAM_SPEAKERS = [
    "shubh", "aditya", "ritu", "priya", "neha", "rahul", "pooja", "rohan",
    "simran", "kavya", "amit", "dev", "ishita", "shreya", "ratan", "varun",
    "manan", "sumit", "roopa", "kabir", "aayan", "ashutosh", "advait", "anand",
    "tanya", "tarun", "sunny", "mani", "gokul", "vijay", "shruti", "suhani",
    "mohit", "kavitha", "rehan", "soham", "rupali",
]

_SAMPLE_TEXT = "Hello! I can help you book your next appointment in just a minute."


def seed_provider_catalog(db: Session) -> dict:
    """Idempotent catalog seed; never overwrites operator edits."""
    created = {"provider_models": 0, "provider_voices": 0}

    for (provider, capability, code, display, langs, codecs, rates, streaming,
         schema, is_default, status, sort) in PROVIDER_MODELS:
        description = MODEL_DESCRIPTIONS.get((provider, capability, code))
        is_eleven_v3 = (provider, capability, code) == ("elevenlabs", "tts", "eleven_v3")
        if is_eleven_v3:
            # The languages table is the source of truth: derive the offered
            # locales from it (fresh installs fall back to the seed-derived
            # constant while the table is still empty).
            langs = _eleven_v3_locales_from_db(db) or list(_ELEVEN_V3_LANGS)
        exists = db.scalar(
            select(ProviderModel).where(
                ProviderModel.provider_code == provider,
                ProviderModel.capability == capability,
                ProviderModel.code == code,
            )
        )
        if exists is None:
            db.add(ProviderModel(
                id=new_id("pm"), provider_code=provider, capability=capability,
                code=code, display_name=display, description=description,
                languages=langs, codecs=codecs,
                sample_rates=rates, streaming=streaming, params_schema=schema,
                is_default=is_default, status=status, sort_order=sort,
            ))
            created["provider_models"] += 1
        else:
            if description and not exists.description:
                # Safe-metadata backfill only: fills an empty description,
                # never replaces operator-entered text or any other field.
                exists.description = description
            if is_eleven_v3 and exists.languages == _LEGACY_ELEVEN_V3_BARE_CODES and langs:
                # One-time conversion of the pre-catalog bare-ISO list to the
                # table-derived locale list. Only a row still exactly equal to
                # the legacy constant is converted — anything else has been
                # operator-managed (master data) and is left untouched.
                exists.languages = langs

    for vid, name, gender, provider_voice_id in ELEVENLABS_VOICES:
        if db.get(VoiceProfile, vid) is None:
            db.add(VoiceProfile(
                id=vid, name=name, gender=gender,
                # Empty languages list = usable for any language the selected
                # ElevenLabs model supports (the model does the language work).
                languages=[],
                accent="Indian", styles=["Natural"], latency_ms=180, premium=True,
                sample_text=_SAMPLE_TEXT, provider="elevenlabs",
                provider_voice_id=provider_voice_id,
                model_codes=["eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_v3"],
                provider_settings=dict(_ELEVEN_DEFAULT_VOICE_SETTINGS),
            ))
            created["provider_voices"] += 1

    for i, speaker in enumerate(SARVAM_SPEAKERS):
        vid = f"vp-sv-{speaker}"
        if db.get(VoiceProfile, vid) is None:
            db.add(VoiceProfile(
                id=vid, name=speaker.title(),
                gender="female" if speaker in _SARVAM_FEMALE else "male",
                languages=list(_SARVAM_VOICE_LOCALES),
                accent="Indian", styles=["Natural"], latency_ms=170, premium=False,
                sample_text=_SAMPLE_TEXT, provider="sarvam",
                # Wire code sent to the Sarvam API (lowercase, exact form).
                provider_voice_id=speaker,
                model_codes=["bulbul:v3"],
                provider_settings={"pace": 1.0},
                is_default=(speaker == "shubh"),
                sort_order=i,
            ))
            created["provider_voices"] += 1

    return created


# ── AI Governance: required active provider matrix ───────────────────────────
# The database stays the source of truth for availability; this reconciliation
# converges any database (fresh or long-lived) to the governed matrix without
# deleting rows, changing IDs or touching operator-maintained metadata.
#
# "mock" is a dev/test pseudo-provider: it keeps its status here and is
# excluded from production by the catalog layer (backend/core/provider_catalog).

ALLOWED_ACTIVE_PROVIDERS: dict[str, set[str]] = {
    "llm": {"openai"},
    "embedding": {"openai"},
    "stt": {"sarvam", "deepgram"},
    "tts": {"sarvam", "elevenlabs"},
    # Voice catalogs follow their TTS vendor's governance.
    "voice": {"platform", "elevenlabs"},
}

_DEV_PSEUDO_PROVIDERS = {"mock"}

# Approved-model registry vendors allowed on the AI Governance page.
_ALLOWED_REGISTRY_VENDORS = {"openai", "sarvam", "elevenlabs", "deepgram"}

# Platform-seeded AI profiles are re-pointed inside the matrix when they
# reference a now-inactive provider. Operator-created profiles are left
# untouched — the UI/API surface those as "inactive selection" instead.
_SEEDED_PROFILE_CODES = {
    "low_cost", "balanced", "high_accuracy", "low_latency", "enterprise", "custom",
}
_PROFILE_REMAP: dict[str, tuple[str, str]] = {
    "stt": ("sarvam", "saaras:v3"),
    "tts": ("sarvam", "bulbul:v3"),
    "llm": ("openai", "gpt-4o-mini"),
    "embedding": ("openai", "text-embedding-3-small"),
}
_REMAPPED_DEFAULT_VOICE = "vp-sv-shubh"


def reconcile_provider_governance(db: Session) -> dict:
    """Idempotent convergence of provider/model activation to the matrix.

    Audits every change with the System actor. Bot/tenant configurations are
    never rewritten here — save-path validation and runtime enforcement handle
    references to providers this pass deactivates.
    """
    from backend.core.audit import record_audit

    changed = {
        "providers_activated": 0, "providers_deactivated": 0,
        "provider_models_deactivated": 0, "ai_profiles_reconciled": 0,
        "approved_models_deprecated": 0,
    }

    def _set_status(row, new_status: str, *, entity_type: str, label: str, action: str):
        before = row.status
        row.status = new_status
        record_audit(
            db, user=None, action=action, entity_type=entity_type,
            entity_id=str(row.id), target_label=label,
            previous_value={"status": before}, new_value={"status": new_status},
        )

    from shared.config import get_settings

    # The mock pseudo-provider backs the credential-free dev/test harness: it
    # converges to active outside production and inactive in production. The
    # catalog and runtime layers additionally refuse mock in production
    # regardless of status (defense in depth).
    mock_target = "inactive" if get_settings().app_env == "production" else "active"

    providers = db.scalars(
        select(ProviderDef).where(ProviderDef.is_deleted.is_(False))
    ).all()
    for provider in providers:
        allowed = ALLOWED_ACTIVE_PROVIDERS.get(provider.kind)
        if allowed is None:
            continue
        label = f"{provider.name} ({provider.kind})"
        if provider.code in _DEV_PSEUDO_PROVIDERS:
            target = mock_target
        else:
            target = "active" if provider.code in allowed else "inactive"
        if target == "active" and provider.status != "active":
            _set_status(provider, "active", entity_type="master:providers",
                        label=label, action="Activated provider (governance)")
            changed["providers_activated"] += 1
        elif target == "inactive" and provider.status == "active":
            _set_status(provider, "inactive", entity_type="master:providers",
                        label=label, action="Deactivated provider (governance)")
            changed["providers_deactivated"] += 1

    models = db.scalars(
        select(ProviderModel).where(ProviderModel.is_deleted.is_(False))
    ).all()
    for model in models:
        allowed = ALLOWED_ACTIVE_PROVIDERS.get(model.capability)
        if allowed is None or model.provider_code in _DEV_PSEUDO_PROVIDERS:
            continue
        # Models of allowed providers keep their operator-managed status;
        # models of disallowed providers must never stay active.
        if model.provider_code not in allowed and model.status == "active":
            _set_status(
                model, "inactive", entity_type="master:provider-models",
                label=f"{model.provider_code}/{model.code} ({model.capability})",
                action="Deactivated provider model (governance)",
            )
            changed["provider_models_deactivated"] += 1

    profiles = db.scalars(
        select(AiConfigProfile).where(
            AiConfigProfile.is_deleted.is_(False),
            AiConfigProfile.code.in_(sorted(_SEEDED_PROFILE_CODES)),
        )
    ).all()
    for profile in profiles:
        before: dict = {}
        after: dict = {}
        for capability, (target_provider, target_model) in _PROFILE_REMAP.items():
            provider_field = f"{capability}_provider"
            model_field = f"{capability}_model"
            current = getattr(profile, provider_field)
            if not current or current in ALLOWED_ACTIVE_PROVIDERS[capability]:
                continue
            before[provider_field] = current
            before[model_field] = getattr(profile, model_field)
            setattr(profile, provider_field, target_provider)
            setattr(profile, model_field, target_model)
            after[provider_field] = target_provider
            after[model_field] = target_model
            if capability == "tts":
                before["default_voice"] = profile.default_voice
                profile.default_voice = _REMAPPED_DEFAULT_VOICE
                after["default_voice"] = _REMAPPED_DEFAULT_VOICE
            if capability == "embedding":
                profile.embedding_dimension = 1536
        fallbacks = profile.fallback_providers or []
        kept = [
            entry for entry in fallbacks
            if isinstance(entry, dict) and all(
                entry.get(f"{cap}_provider") in (None, *ALLOWED_ACTIVE_PROVIDERS[cap])
                for cap in _PROFILE_REMAP
            )
        ]
        if kept != fallbacks:
            before["fallback_providers"] = fallbacks
            after["fallback_providers"] = kept
            profile.fallback_providers = kept
        if after:
            record_audit(
                db, user=None, action="Reconciled AI configuration profile (governance)",
                entity_type="master:ai-profiles", entity_id=profile.id,
                target_label=profile.name, previous_value=before, new_value=after,
            )
            changed["ai_profiles_reconciled"] += 1

    registry_rows = db.scalars(
        select(ApprovedModel).where(ApprovedModel.is_deleted.is_(False))
    ).all()
    for row in registry_rows:
        vendor = (row.provider or "").strip().lower()
        if vendor not in _ALLOWED_REGISTRY_VENDORS and row.status != "deprecated":
            previous = row.status
            row.status = "deprecated"
            record_audit(
                db, user=None, action="Deprecated model (governance)",
                entity_type="approved_model", entity_id=row.id,
                target_label=f"{row.name} · {row.purpose}",
                previous_value={"status": previous}, new_value={"status": "deprecated"},
            )
            changed["approved_models_deprecated"] += 1

    if any(changed.values()):
        # Deactivations must reach live call resolution promptly — drop every
        # cached bot config snapshot (best-effort; TTL caps staleness anyway).
        from shared.bot_config import invalidate_all_bot_configs_sync

        invalidate_all_bot_configs_sync()
    return changed
