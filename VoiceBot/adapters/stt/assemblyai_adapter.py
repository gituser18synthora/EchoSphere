"""AssemblyAI STT adapter. Input: raw PCM 8kHz 16-bit mono."""
 
import asyncio
import concurrent.futures
import io
import time
from typing import Any
 
import assemblyai as aai
 
from adapters.audio_utils import pcm_to_wav_bytes
from adapters.base import AdapterException, STTAdapter, STTResponse
from config.settings import Settings
 
# Bounded executor — prevents unbounded thread growth under bulk calls.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="assemblyai")
 
 
class AssemblyAISTTAdapter(STTAdapter):
    """AssemblyAI transcript API. Converts PCM to WAV for upload."""
 
    def __init__(self, **kwargs: Any) -> None:
        settings = Settings()
        aai.settings.api_key = settings.assemblyai_api_key
        self._timeout = getattr(settings, "stt_tts_max_latency", 8.0)
 
    async def transcribe(
        self,
        audio_bytes: bytes,
        language: str = "en",
        auto_detect: bool = False,
    ) -> STTResponse:
        wav_bytes = pcm_to_wav_bytes(audio_bytes, sample_rate=8000)
        start = time.perf_counter()
 
        def _sync_transcribe() -> Any:
            config = aai.TranscriptionConfig(
                language_code=language if not auto_detect else None,
            )
            transcriber = aai.Transcriber(config=config)
            return transcriber.transcribe(io.BytesIO(wav_bytes))
 
        loop = asyncio.get_running_loop()
        try:
            transcript = await asyncio.wait_for(
                loop.run_in_executor(_EXECUTOR, _sync_transcribe),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as e:
            raise AdapterException(
                f"AssemblyAI request timed out after {self._timeout}s"
            ) from e
        except Exception as e:
            err = str(e).lower()
            if "401" in str(e) or "auth" in err or "unauthorized" in err:
                raise AdapterException(
                    f"AssemblyAI authentication failed: {e}"
                ) from e
            if "429" in str(e) or "rate" in err:
                raise AdapterException(
                    f"AssemblyAI rate limit exceeded: {e}",
                    retry_after=60.0,
                ) from e
            if "503" in str(e) or "unavailable" in err:
                raise AdapterException(
                    f"AssemblyAI service unavailable: {e}"
                ) from e
            raise AdapterException(f"AssemblyAI error: {e}") from e
 
        latency_ms = (time.perf_counter() - start) * 1000
        text = getattr(transcript, "text", "") or ""
        confidence = getattr(transcript, "confidence", 1.0) or 1.0
        detected = getattr(transcript, "language_code", language) or language
        return STTResponse(
            text=text,
            detected_language=detected,
            confidence=confidence,
            is_final=True,
        )