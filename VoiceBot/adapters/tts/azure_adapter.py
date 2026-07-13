"""Azure TTS adapter. Output: PCM 8kHz 16-bit mono."""

import asyncio
import time
from typing import Any, AsyncIterator

import azure.cognitiveservices.speech as speechsdk

from adapters.audio_utils import resample_pcm_to_8k
from adapters.base import AdapterException, TTSAdapter, TTSResponse
from config.settings import Settings

# Azure often returns 16kHz or 24kHz; we resample to 8k
AZURE_DEFAULT_SAMPLE_RATE = 16000


class AzureTTSAdapter(TTSAdapter):
    """Azure Cognitive Services Speech. Output resampled to 8kHz 16-bit mono."""

    def __init__(self, **kwargs: Any) -> None:
        settings = Settings()
        self._speech_config = speechsdk.SpeechConfig(
            subscription=settings.azure_speech_key,
            region=settings.azure_speech_region,
        )
        self._speech_config.speech_synthesis_output_format = (
            speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
        )
        self._timeout = getattr(settings, "stt_tts_max_latency", 2.0)
        self._sample_rate = 16000

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> TTSResponse:
        self._speech_config.speech_synthesis_voice_name = voice_id
        pull_stream = speechsdk.audio.PullAudioOutputStream()
        audio_config = speechsdk.audio.AudioConfig(stream=pull_stream)
        synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=self._speech_config,
            audio_config=audio_config,
        )
        start = time.perf_counter()

        def _sync_speak() -> Any:
            return synthesizer.speak_text_async(text).get()

        try:
            result = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _sync_speak),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as e:
            raise AdapterException(
                f"Azure TTS request timed out after {self._timeout}s"
            ) from e
        except Exception as e:
            _raise_azure_error(e, self._timeout)

        if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
            raise AdapterException(
                f"Azure TTS failed: {result.reason} - {getattr(result, 'error_details', '')}"
            )
        chunks = []
        buf = bytearray(3200)
        while True:
            filled = pull_stream.read(buf)
            if filled <= 0:
                break
            chunks.append(bytes(buf[:filled]))
        raw = b"".join(chunks)
        pcm_8k = resample_pcm_to_8k(raw, self._sample_rate)
        latency_ms = (time.perf_counter() - start) * 1000
        duration_ms = len(pcm_8k) / (8000 * 2) * 1000
        return TTSResponse(
            audio_bytes=pcm_8k,
            sample_rate=8000,
            duration_ms=duration_ms,
        )

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> AsyncIterator[bytes]:
        self._speech_config.speech_synthesis_voice_name = voice_id
        pull_stream = speechsdk.audio.PullAudioOutputStream()
        audio_config = speechsdk.audio.AudioConfig(stream=pull_stream)
        synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=self._speech_config,
            audio_config=audio_config,
        )

        def _sync_speak() -> Any:
            return synthesizer.speak_text_async(text).get()

        try:
            result = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _sync_speak),
                timeout=self._timeout,
            )
        except Exception as e:
            _raise_azure_error(e, self._timeout)

        if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
            raise AdapterException(
                f"Azure TTS failed: {result.reason} - {getattr(result, 'error_details', '')}"
            )
        buf = bytearray(3200)
        while True:
            filled = pull_stream.read(buf)
            if filled <= 0:
                break
            chunk = bytes(buf[:filled])
            resampled = resample_pcm_to_8k(chunk, self._sample_rate)
            if resampled:
                yield resampled


def _raise_azure_error(e: Exception, timeout: float) -> None:
    err = str(e).lower()
    if "401" in str(e) or "auth" in err or "unauthorized" in err:
        raise AdapterException(f"Azure TTS authentication failed: {e}") from e
    if "429" in str(e) or "rate" in err:
        raise AdapterException(
            f"Azure TTS rate limit exceeded: {e}",
            retry_after=60.0,
        ) from e
    if "timeout" in err:
        raise AdapterException(f"Azure TTS request timed out: {e}") from e
    raise AdapterException(f"Azure TTS error: {e}") from e