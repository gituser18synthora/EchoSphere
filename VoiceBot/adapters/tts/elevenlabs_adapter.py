"""ElevenLabs TTS adapter. Output: PCM 8kHz 16-bit mono. Streaming for low latency."""
 
import asyncio
import concurrent.futures
import time
from typing import Any, AsyncIterator
 
from elevenlabs.client import ElevenLabs
 
from adapters.audio_utils import resample_pcm_to_8k
from adapters.base import AdapterException, TTSAdapter, TTSResponse
from config.settings import Settings
 
# ElevenLabs PCM formats: pcm_22050_16, pcm_44100_16, etc. We use 22050 and resample to 8k.
ELEVENLABS_PCM_RATE = 22050
 
# Bounded thread pool — prevents unbounded thread growth under bulk calls.
# 10 threads handles ~10 concurrent ElevenLabs requests without blocking the event loop.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="elevenlabs")
 
 
class ElevenLabsTTSAdapter(TTSAdapter):
    """ElevenLabs TTS with eleven_turbo_v2. Streams PCM chunks; resamples to 8kHz.
 
    The ElevenLabs SDK is sync-only. Both synthesize() and synthesize_stream()
    run the blocking SDK call in a bounded thread pool so the asyncio event loop
    is never blocked — critical for handling concurrent calls.
    """
 
    def __init__(self, **kwargs: Any) -> None:
        settings = Settings()
        self._client = ElevenLabs(api_key=settings.elevenlabs_api_key)
        self._timeout = getattr(settings, "stt_tts_max_latency", 8.0)
 
    def _collect_chunks(self, text: str, voice_id: str) -> bytes:
        """Sync: call ElevenLabs and collect all PCM chunks. Runs in executor."""
        chunks = []
        response = self._client.text_to_speech.convert(
            voice_id=voice_id,
            text=text,
            model_id="eleven_turbo_v2",
            output_format=f"pcm_{ELEVENLABS_PCM_RATE}_16",
        )
        for chunk in response:
            if chunk:
                chunks.append(chunk)
        return b"".join(chunks)
 
    async def synthesize(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> TTSResponse:
        start = time.perf_counter()
        loop = asyncio.get_running_loop()
        try:
            raw = await asyncio.wait_for(
                loop.run_in_executor(_EXECUTOR, self._collect_chunks, text, voice_id),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as e:
            raise AdapterException(
                f"ElevenLabs request timed out after {self._timeout}s"
            ) from e
        except Exception as e:
            _raise_elevenlabs_error(e, self._timeout)
 
        pcm_8k = resample_pcm_to_8k(raw, ELEVENLABS_PCM_RATE)
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
        """Collect all audio in executor, then yield as single chunk.
 
        ElevenLabs sync SDK cannot be iterated incrementally from an async context
        without blocking the loop. We collect in the thread pool and yield once —
        same latency profile as before but without blocking the event loop.
        """
        loop = asyncio.get_running_loop()
        try:
            raw = await asyncio.wait_for(
                loop.run_in_executor(_EXECUTOR, self._collect_chunks, text, voice_id),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as e:
            raise AdapterException(
                f"ElevenLabs request timed out after {self._timeout}s"
            ) from e
        except Exception as e:
            _raise_elevenlabs_error(e, self._timeout)
 
        pcm_8k = resample_pcm_to_8k(raw, ELEVENLABS_PCM_RATE)
        if pcm_8k:
            yield pcm_8k
 
 
def _raise_elevenlabs_error(e: Exception, timeout: float) -> None:
    err = str(e).lower()
    if "401" in str(e) or "auth" in err or "unauthorized" in err:
        raise AdapterException(f"ElevenLabs authentication failed: {e}") from e
    if "429" in str(e) or "rate" in err:
        raise AdapterException(
            f"ElevenLabs rate limit exceeded: {e}",
            retry_after=60.0,
        ) from e
    if "timeout" in err:
        raise AdapterException(f"ElevenLabs request timed out: {e}") from e
    raise AdapterException(f"ElevenLabs error: {e}") from e