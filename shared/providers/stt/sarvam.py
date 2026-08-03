"""Sarvam AI STT (Saaras) — lazy-imports the sarvamai SDK.

Migrated from the legacy voice engines sarvam_adapter.py. Fixes the legacy Odia
inconsistency: the BCP-47 code is "or-IN" everywhere (the legacy STT mapping
used the invalid "od-IN" while TTS used "or-IN").
"""

import asyncio
import time

from shared.config import get_settings
from shared.providers.base import ProviderConfig, ProviderError, STTProvider, STTResult
from shared.audio.pcm import pcm_to_wav_bytes

# Internal short codes → Sarvam BCP-47. Odia accepts both "od" and "or"
# internally but always maps to "or-IN" on the wire.
_LANG_TO_SARVAM = {
    "en": "en-IN",
    "hi": "hi-IN",
    "bn": "bn-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "od": "or-IN",
    "or": "or-IN",
    "pa": "pa-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "gu": "gu-IN",
}


def _sarvam_lang_to_internal(code: str | None, fallback: str) -> str:
    """Map a Sarvam language_code (e.g. "hi-IN") back to an internal short code."""
    if not code:
        return fallback
    base = code.split("-")[0].lower()
    return base if base in _LANG_TO_SARVAM else fallback


class SarvamSTT(STTProvider):
    """Sarvam speech-to-text. Language detection is automatic ("unknown" mode)
    when the requested language has no Sarvam mapping; the caller's language is
    used as a fallback label if Sarvam returns no detected language."""

    name = "sarvam-stt"

    def __init__(self, config: ProviderConfig) -> None:
        try:
            from sarvamai import AsyncSarvamAI
        except ImportError as exc:
            raise ProviderError(
                self.name, "invalid_input",
                "sarvamai SDK is not installed; run `pip install sarvamai` "
                "to use the sarvam STT provider",
            ) from exc
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.stt_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._client = AsyncSarvamAI(
            api_subscription_key=key, timeout=config.timeout_seconds
        )
        self._model = config.model or "saaras:v3"
        self._language = config.language or "en"
        self._timeout = config.timeout_seconds

    async def transcribe(
        self, audio: bytes, *, sample_rate: int = 16000, language: str | None = None
    ) -> STTResult:
        if not audio:
            return STTResult(text="")
        started = time.perf_counter()
        wav = pcm_to_wav_bytes(audio, sample_rate)
        lang = (language or self._language or "en").lower()
        try:
            response = await asyncio.wait_for(
                self._client.speech_to_text.transcribe(
                    file=("audio.wav", wav, "audio/wav"),
                    model=self._model,
                    language_code=_LANG_TO_SARVAM.get(lang, "unknown"),
                ),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            raise ProviderError(
                self.name, "timeout", f"Request timed out after {self._timeout}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — SDK error types are lazy-loaded
            raise _categorize(self.name, exc) from exc

        raw_detected = getattr(response, "language_code", None)
        # Present only in auto-detect mode ("unknown"); None when pinned.
        raw_probability = getattr(response, "language_probability", None)
        return STTResult(
            text=(getattr(response, "transcript", "") or "").strip(),
            language=_sarvam_lang_to_internal(raw_detected, lang),
            language_probability=(
                float(raw_probability) if raw_probability is not None else None
            ),
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
