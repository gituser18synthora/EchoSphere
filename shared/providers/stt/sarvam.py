"""Sarvam AI STT (Saaras) — lazy-imports the sarvamai SDK.

Migrated from the legacy voice engines sarvam_adapter.py. This adapter keeps no
locale table of its own: the platform→Sarvam spelling lives in
``shared.providers.languages`` (the one provider-mapping module), so the STT
and TTS sides cannot drift apart. Urdu is the one enabled platform language
Sarvam transcribes but cannot speak, which is why the STT locale set is defined
separately from the TTS one.
"""

import asyncio
import logging
import time

from shared.config import get_settings
from shared.providers.base import ProviderConfig, ProviderError, STTProvider, STTResult
from shared.providers.languages import (
    SARVAM_STT_SUPPORTED_LOCALES,
    sarvam_stt_language_code,
    to_platform_language,
)
from shared.audio.pcm import pcm_to_wav_bytes

logger = logging.getLogger("providers.stt.sarvam")


def _base_language(language: str) -> str:
    """Normalize a platform locale or short code to the internal base code.

    Callers hand this provider both spellings — the bot's configured locale
    ("hi-IN") and the short code ("hi"). The shared mapper accepts both; this
    helper only produces the short label a transcript is tagged with.
    """
    return (language or "").strip().split("-")[0].lower()


def sarvam_language_code(language: str | None) -> str:
    """The wire ``language_code`` for a platform language ("hi-IN"/"hi" →
    "hi-IN"); blank, "auto" and languages the recognizer cannot pin request
    auto-detect. Thin wrapper over the shared provider mapping."""
    return sarvam_stt_language_code(language)


def _sarvam_lang_to_internal(code: str | None, fallback: str) -> str:
    """Map a Sarvam language_code (e.g. "hi-IN") back to an internal short code."""
    if not code:
        return fallback
    platform = to_platform_language("sarvam", code)
    if platform not in SARVAM_STT_SUPPORTED_LOCALES:
        return fallback
    return _base_language(platform)


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
        requested = language or self._language or "en"
        lang = _base_language(requested)
        wire = sarvam_language_code(requested)
        if wire == "unknown" and lang not in ("", "auto", "unknown"):
            logger.warning(
                "sarvam-stt: language '%s' cannot be pinned — transcribing with "
                "auto-detect instead", requested,
            )
        try:
            response = await asyncio.wait_for(
                self._client.speech_to_text.transcribe(
                    file=("audio.wav", wav, "audio/wav"),
                    model=self._model,
                    language_code=wire,
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
