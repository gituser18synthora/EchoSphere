"""Google Cloud Text-to-Speech adapter. Output: PCM 8kHz 16-bit mono."""

import asyncio
import time
from typing import Any, AsyncIterator

from google.cloud import texttospeech

from adapters.audio_utils import resample_pcm_to_8k
from adapters.base import AdapterException, TTSAdapter, TTSResponse
from config.settings import Settings

# Request 8kHz so no resampling needed when supported; else 16k and resample
GOOGLE_TTS_SAMPLE_RATE = 8000


class GoogleTTSAdapter(TTSAdapter):
    """Google Cloud TTS. Request LINEAR16 at 8kHz when possible."""

    def __init__(self, **kwargs: Any) -> None:
        settings = Settings()
        self._client = texttospeech.TextToSpeechClient()
        # Use credentials from env (GOOGLE_APPLICATION_CREDENTIALS or default)
        self._timeout = getattr(settings, "stt_tts_max_latency", 2.0)

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> TTSResponse:
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(
            name=voice_id,
            language_code="en-US",
        )
        # Request 8kHz 16-bit LINEAR16 so output is already FreeSWITCH format
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=GOOGLE_TTS_SAMPLE_RATE,
            speaking_rate=speed,
            pitch=pitch,
        )
        start = time.perf_counter()

        def _sync_synthesize() -> Any:
            return self._client.synthesize_speech(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config,
            )

        try:
            response = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _sync_synthesize),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as e:
            raise AdapterException(
                f"Google TTS request timed out after {self._timeout}s"
            ) from e
        except Exception as e:
            _raise_google_error(e, self._timeout)

        raw = response.audio_content
        # If API returns different rate, resample
        pcm_8k = resample_pcm_to_8k(raw, GOOGLE_TTS_SAMPLE_RATE)
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
        """Google TTS does not support true streaming; yield single chunk."""
        response = await self.synthesize(
            text=text,
            voice_id=voice_id,
            speed=speed,
            pitch=pitch,
        )
        if response.audio_bytes:
            yield response.audio_bytes


def _raise_google_error(e: Exception, timeout: float) -> None:
    err = str(e).lower()
    if "401" in str(e) or "auth" in err or "unauthorized" in err:
        raise AdapterException(f"Google TTS authentication failed: {e}") from e
    if "429" in str(e) or "rate" in err or "resource_exhausted" in err:
        raise AdapterException(
            f"Google TTS rate limit exceeded: {e}",
            retry_after=60.0,
        ) from e
    if "timeout" in err or "deadline" in err:
        raise AdapterException(f"Google TTS request timed out: {e}") from e
    raise AdapterException(f"Google TTS error: {e}") from e