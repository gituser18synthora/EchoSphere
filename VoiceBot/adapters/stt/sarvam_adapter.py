"""Sarvam AI Speech-to-Text adapter. Input: raw PCM 8kHz 16-bit mono (WAV)."""

import asyncio
import logging
import os
import time
from typing import Any

from sarvamai import AsyncSarvamAI

from adapters.audio_utils import pcm_to_wav_bytes
from adapters.base import AdapterException, STTAdapter, STTResponse
from config.settings import Settings

logger = logging.getLogger(__name__)

# Map our language codes to Sarvam BCP-47
_LANG_TO_SARVAM = {
    "en": "en-IN",
    "hi": "hi-IN",
    "bn": "bn-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "od": "od-IN",
    "pa": "pa-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "gu": "gu-IN",
}

# All Sarvam-supported BCP-47 codes (for response validation)
_SARVAM_SUPPORTED = set(_LANG_TO_SARVAM.values())


def _sarvam_lang_to_internal(code: str | None) -> str:
    """Map Sarvam language_code (e.g. en-IN) to internal short code (e.g. en)."""
    if not code:
        return "en"
    base = code.split("-")[0].lower()
    return base if base in _LANG_TO_SARVAM else "en"


class SarvamSTTAdapter(STTAdapter):
    """
    Sarvam AI Speech-to-Text (Saarika model).

    Language detection is always automatic — Sarvam's 'unknown' mode
    detects the spoken language from audio and returns it in the response.
    The caller may still pass a `language` hint; it is used only as a
    fallback label if Sarvam returns no language in the response.
    """

    def __init__(self, **kwargs: Any) -> None:
        settings = Settings()
        key = (getattr(settings, "sarvam_api_key", None) or "").strip() or os.environ.get("SARVAM_API_KEY")
        self._client = AsyncSarvamAI(
            api_subscription_key=key,
            timeout=30.0,
        )
        self._timeout = getattr(settings, "stt_tts_max_latency", 2.0) * 5

    async def transcribe(
        self,
        audio_bytes: bytes,
        language: str = "en",       # used only as fallback if Sarvam returns no language
        auto_detect: bool = True,   # kept for API compatibility; detection is always on
    ) -> STTResponse:
        wav_bytes = pcm_to_wav_bytes(audio_bytes, sample_rate=8000)
        file = ("audio.wav", wav_bytes, "audio/wav")

        logger.debug(
            "[SarvamSTT] Sending audio for transcription (language=%s → %s)",
            language,
            _LANG_TO_SARVAM.get(language, "unknown"),
        )

        start = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self._client.speech_to_text.transcribe(
                    file=file,
                    model="saaras:v3",
                    language_code=_LANG_TO_SARVAM.get(language, "unknown"),
                    #high_vad_sensitivity=True,  # TODO: verify sarvamai SDK version supports high_vad_sensitivity
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as e:
            raise AdapterException(
                f"Sarvam STT request timed out after {self._timeout}s"
            ) from e
        except Exception as e:
            _raise_sarvam_error(e)

        latency_ms = (time.perf_counter() - start) * 1000

        text = getattr(response, "transcript", "") or ""

        # Prefer detected language from Sarvam; fall back to caller's hint
        raw_detected = getattr(response, "language_code", None)
        detected = _sarvam_lang_to_internal(raw_detected) if raw_detected else language

        logger.info(
            "[SarvamSTT] transcript=%r | detected=%s (raw=%s) | latency=%.0fms",
            text[:80], detected, raw_detected, latency_ms,
        )

        return STTResponse(
            text=text,
            detected_language=detected,
            confidence=1.0,
            is_final=True,
        )


def _raise_sarvam_error(e: Exception) -> None:
    err = str(e).lower()
    if "401" in str(e) or "auth" in err or "unauthorized" in err:
        raise AdapterException(f"Sarvam authentication failed: {e}") from e
    if "429" in str(e) or "rate" in err:
        raise AdapterException(
            f"Sarvam rate limit exceeded: {e}",
            retry_after=60.0,
        ) from e
    if "timeout" in err:
        raise AdapterException(f"Sarvam request timed out: {e}") from e
    raise AdapterException(f"Sarvam STT error: {e}") from e