"""Google Cloud Text-to-Speech — lazy-imports google-cloud-texttospeech.

Migrated from the legacy voice engines google_adapter.py. Fixes the legacy bug
where the language_code was hardcoded to "en-US": it now derives from the
``language`` argument / config. Requests LINEAR16 at 16 kHz and strips the
WAV header Google prepends to LINEAR16 output (the legacy adapter passed the
header bytes through as if they were PCM).
"""

import asyncio
import time

from backend.config import get_settings
from backend.providers.base import (
    ProviderConfig,
    ProviderError,
    TTSProvider,
    TTSResult,
    run_in_sdk_pool,
)
from backend.voice_runtime.audio.pcm import resample_pcm, wav_to_pcm

_PCM_RATE = 16000

# Internal short codes → default BCP-47 regional codes.
_SHORT_TO_BCP47 = {
    "en": "en-US",
    "hi": "hi-IN",
    "bn": "bn-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "gu": "gu-IN",
    "pa": "pa-IN",
    "od": "or-IN",
    "or": "or-IN",
}


def _to_bcp47(code: str) -> str:
    if "-" in code:
        return code
    lowered = code.lower()
    if lowered in _SHORT_TO_BCP47:
        return _SHORT_TO_BCP47[lowered]
    if len(lowered) == 2:
        return f"{lowered}-{lowered.upper()}"
    return "en-US"


class GoogleTTS(TTSProvider):
    name = "google-tts"

    def __init__(self, config: ProviderConfig) -> None:
        try:
            from google.cloud import texttospeech
        except ImportError as exc:
            raise ProviderError(
                self.name, "invalid_input",
                "google-cloud-texttospeech SDK is not installed; run "
                "`pip install google-cloud-texttospeech` to use the google TTS provider",
            ) from exc
        self._tts = texttospeech
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.tts_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._client = texttospeech.TextToSpeechClient(client_options={"api_key": key})
        self._voice = config.voice or ""
        self._language = config.language or "en"
        self._timeout = config.timeout_seconds

    def _synthesize_sync(self, text: str, voice_name: str, language_code: str,
                         speed: float) -> bytes:
        tts = self._tts
        voice_params: dict = {"language_code": language_code}
        if voice_name:
            voice_params["name"] = voice_name
        response = self._client.synthesize_speech(
            input=tts.SynthesisInput(text=text),
            voice=tts.VoiceSelectionParams(**voice_params),
            audio_config=tts.AudioConfig(
                audio_encoding=tts.AudioEncoding.LINEAR16,
                sample_rate_hertz=_PCM_RATE,
                speaking_rate=max(0.25, min(4.0, speed)),
            ),
        )
        return bytes(response.audio_content)

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None,
        speed: float = 1.0,
    ) -> TTSResult:
        if not text.strip():
            return TTSResult(audio=b"", sample_rate=_PCM_RATE)
        voice_name = (voice or self._voice).strip()
        language_code = _to_bcp47(language or self._language or "en")
        started = time.perf_counter()
        try:
            raw = await asyncio.wait_for(
                run_in_sdk_pool(
                    self._synthesize_sync, text, voice_name, language_code, speed
                ),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            raise ProviderError(
                self.name, "timeout", f"Request timed out after {self._timeout}s"
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK error types are lazy-loaded
            raise _categorize(self.name, exc) from exc

        # LINEAR16 responses carry a WAV header — strip it and normalize rate.
        if raw.startswith(b"RIFF"):
            pcm, rate = wav_to_pcm(raw)
            if pcm and rate and rate != _PCM_RATE:
                pcm = resample_pcm(pcm, rate, _PCM_RATE)
        else:
            pcm = raw
        return TTSResult(
            audio=pcm,
            sample_rate=_PCM_RATE,
            duration_ms=(time.perf_counter() - started) * 1000,
        )


def _categorize(provider: str, exc: Exception) -> ProviderError:
    text = str(exc)
    lowered = text.lower()
    if "401" in text or "403" in text or "unauthenticated" in lowered or "permission" in lowered or "api key" in lowered:
        return ProviderError(provider, "auth", text[:200])
    if "429" in text or "resource_exhausted" in lowered or "rate" in lowered or "quota" in lowered:
        return ProviderError(provider, "rate_limit", text[:200])
    if "deadline" in lowered or "timeout" in lowered or "timed out" in lowered:
        return ProviderError(provider, "timeout", text[:200])
    return ProviderError(provider, "upstream", text[:200])
