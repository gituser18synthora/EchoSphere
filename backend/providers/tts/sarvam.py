"""Sarvam AI TTS (Bulbul) — lazy-imports the sarvamai SDK.

Migrated from VoiceBot/adapters/tts/sarvam_adapter.py. Keeps the
script-based Indic language auto-detection but honors an explicit
``language`` argument first. The WAV parsing and resampling now use the
shared numpy helpers in backend.voice_runtime.audio.pcm (replacing the
legacy per-sample pure-python loop), and output is 16 kHz 16-bit mono PCM.
"""

import asyncio
import base64
import re
import time

from backend.config import get_settings
from backend.providers.base import ProviderConfig, ProviderError, TTSProvider, TTSResult
from backend.voice_runtime.audio.pcm import resample_pcm, wav_to_pcm
from backend.voice_runtime.audio.text import sanitize_for_tts

_PCM_RATE = 16000

# Unicode ranges for Indic scripts → Sarvam language code.
_SCRIPT_LANG_MAP = [
    (r"[\u0900-\u097F]", "hi-IN"),  # Devanagari -> Hindi
    (r"[\u0980-\u09FF]", "bn-IN"),  # Bengali
    (r"[\u0A00-\u0A7F]", "pa-IN"),  # Gurmukhi -> Punjabi
    (r"[\u0A80-\u0AFF]", "gu-IN"),  # Gujarati
    (r"[\u0B00-\u0B7F]", "or-IN"),  # Odia
    (r"[\u0B80-\u0BFF]", "ta-IN"),  # Tamil
    (r"[\u0C00-\u0C7F]", "te-IN"),  # Telugu
    (r"[\u0C80-\u0CFF]", "kn-IN"),  # Kannada
    (r"[\u0D00-\u0D7F]", "ml-IN"),  # Malayalam
]
_COMPILED_SCRIPT_MAP = [(re.compile(pattern), lang) for pattern, lang in _SCRIPT_LANG_MAP]

_SUPPORTED_LANGS = {
    "hi-IN", "bn-IN", "kn-IN", "ml-IN", "mr-IN",
    "pa-IN", "raj-IN", "ta-IN", "te-IN",
    "en-IN", "gu-IN", "or-IN",
}

# Internal short codes → Sarvam BCP-47 (Odia is "or-IN" everywhere).
_SHORT_TO_SARVAM = {
    "en": "en-IN", "hi": "hi-IN", "bn": "bn-IN", "kn": "kn-IN", "ml": "ml-IN",
    "mr": "mr-IN", "od": "or-IN", "or": "or-IN", "pa": "pa-IN", "ta": "ta-IN",
    "te": "te-IN", "gu": "gu-IN",
}

_ALLOWED_SPEAKERS = {
    "anushka", "abhilash", "manisha", "vidya", "arya", "karun", "hitesh", "rohan",
}
_DEFAULT_SPEAKER = "rohan"


def _detect_language(text: str) -> str:
    """Detect a Sarvam language code from the script used in ``text``.

    Mixed text picks the script with the most characters; plain Latin text
    falls back to "en-IN".
    """
    counts: dict[str, int] = {}
    for pattern, lang in _COMPILED_SCRIPT_MAP:
        matches = pattern.findall(text)
        if matches:
            counts[lang] = len(matches)
    if not counts:
        return "en-IN"
    return max(counts, key=lambda key: counts[key])


def _resolve_language(explicit: str | None, text: str) -> str:
    if explicit:
        code = explicit if "-" in explicit else _SHORT_TO_SARVAM.get(explicit.lower(), "")
        if code in _SUPPORTED_LANGS:
            return code
    return _detect_language(text)


def _map_voice_to_speaker(voice: str | None) -> str:
    candidate = (voice or "").strip().lower()
    return candidate if candidate in _ALLOWED_SPEAKERS else _DEFAULT_SPEAKER


class SarvamTTS(TTSProvider):
    name = "sarvam-tts"

    def __init__(self, config: ProviderConfig) -> None:
        try:
            from sarvamai import AsyncSarvamAI
        except ImportError as exc:
            raise ProviderError(
                self.name, "invalid_input",
                "sarvamai SDK is not installed; run `pip install sarvamai` "
                "to use the sarvam TTS provider",
            ) from exc
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.tts_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._client = AsyncSarvamAI(
            api_subscription_key=key, timeout=config.timeout_seconds
        )
        self._model = config.model or "bulbul:v3"
        self._voice = config.voice or ""
        self._language = config.language or ""
        self._timeout = config.timeout_seconds

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None,
        speed: float = 1.0,
    ) -> TTSResult:
        text = sanitize_for_tts(text)
        if not text:
            return TTSResult(audio=b"", sample_rate=_PCM_RATE)
        started = time.perf_counter()
        language_code = _resolve_language(language or self._language or None, text)
        speaker = _map_voice_to_speaker(voice or self._voice)
        try:
            response = await asyncio.wait_for(
                self._client.text_to_speech.convert(
                    text=text,
                    model=self._model,
                    target_language_code=language_code,
                    speaker=speaker,
                    pace=max(0.5, min(2.0, speed)),
                    speech_sample_rate=_PCM_RATE,
                    output_audio_codec="wav",
                ),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            raise ProviderError(
                self.name, "timeout", f"Request timed out after {self._timeout}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — SDK error types are lazy-loaded
            raise _categorize(self.name, exc) from exc

        audios = getattr(response, "audios", None) or []
        pcm = b""
        if audios:
            wav_bytes = base64.b64decode(audios[0])
            pcm, rate = wav_to_pcm(wav_bytes)
            if pcm and rate and rate != _PCM_RATE:
                pcm = resample_pcm(pcm, rate, _PCM_RATE)
        return TTSResult(
            audio=pcm,
            sample_rate=_PCM_RATE,
            duration_ms=(time.perf_counter() - started) * 1000,
        )


def _categorize(provider: str, exc: Exception) -> ProviderError:
    text = str(exc)
    lowered = text.lower()
    if "401" in text or "403" in text or "unauthorized" in lowered or "auth" in lowered:
        return ProviderError(provider, "auth", text[:200])
    if "429" in text or "rate" in lowered:
        return ProviderError(provider, "rate_limit", text[:200])
    if "timeout" in lowered or "timed out" in lowered:
        return ProviderError(provider, "timeout", text[:200])
    return ProviderError(provider, "upstream", text[:200])
