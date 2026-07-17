"""Azure Cognitive Services TTS — lazy-imports the azure speech SDK.

Migrated from the legacy voice engines azure_adapter.py. Fixes the legacy
concurrency bug: the old adapter mutated a shared SpeechConfig
(``speech_synthesis_voice_name``) per call; this port builds a fresh
SpeechConfig + synthesizer inside every synthesize() so concurrent calls
cannot race. Output is raw 16 kHz 16-bit mono PCM (no resample step).
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

_PCM_RATE = 16000


class AzureTTS(TTSProvider):
    name = "azure-tts"

    def __init__(self, config: ProviderConfig) -> None:
        try:
            import azure.cognitiveservices.speech as speechsdk
        except ImportError as exc:
            raise ProviderError(
                self.name, "invalid_input",
                "azure speech SDK is not installed; run "
                "`pip install azure-cognitiveservices-speech` to use the azure TTS provider",
            ) from exc
        self._speechsdk = speechsdk
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.tts_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        region = str((config.extra or {}).get("region") or "").strip()
        if not region:
            raise ProviderError(
                self.name, "invalid_input",
                "Missing Azure speech region (set config.extra['region'])",
            )
        self._key = key
        self._region = region
        self._voice = config.voice or ""
        self._timeout = config.timeout_seconds

    def _synthesize_sync(self, text: str, voice_name: str) -> bytes:
        """Blocking SDK call — runs in the shared provider thread pool.

        Builds a fresh SpeechConfig/SpeechSynthesizer per call (no shared
        mutable state). ``audio_config=None`` keeps the audio in memory.
        """
        speechsdk = self._speechsdk
        speech_config = speechsdk.SpeechConfig(subscription=self._key, region=self._region)
        speech_config.speech_synthesis_output_format = (
            speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
        )
        speech_config.speech_synthesis_voice_name = voice_name
        synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=speech_config, audio_config=None
        )
        result = synthesizer.speak_text_async(text).get()
        if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
            cancellation = getattr(result, "cancellation_details", None)
            detail = ""
            if cancellation is not None:
                detail = f" {cancellation.reason}: {getattr(cancellation, 'error_details', '')}"
            raise RuntimeError(f"Synthesis failed ({result.reason}){detail}")
        return bytes(result.audio_data)

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None,
        speed: float = 1.0,
    ) -> TTSResult:
        if not text.strip():
            return TTSResult(audio=b"", sample_rate=_PCM_RATE)
        voice_name = (voice or self._voice).strip()
        if not voice_name:
            raise ProviderError(
                self.name, "invalid_input",
                "Azure TTS requires a voice name (set config.voice or pass voice=)",
            )
        started = time.perf_counter()
        try:
            audio = await asyncio.wait_for(
                run_in_sdk_pool(self._synthesize_sync, text, voice_name),
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
        return TTSResult(
            audio=audio,
            sample_rate=_PCM_RATE,
            duration_ms=(time.perf_counter() - started) * 1000,
        )


def _categorize(provider: str, exc: Exception) -> ProviderError:
    text = str(exc)
    lowered = text.lower()
    if "401" in text or "403" in text or "unauthorized" in lowered or "auth" in lowered:
        return ProviderError(provider, "auth", text[:200])
    if "429" in text or "rate" in lowered or "throttl" in lowered:
        return ProviderError(provider, "rate_limit", text[:200])
    if "timeout" in lowered or "timed out" in lowered:
        return ProviderError(provider, "timeout", text[:200])
    return ProviderError(provider, "upstream", text[:200])
