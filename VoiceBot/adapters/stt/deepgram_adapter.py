"""Deepgram STT adapter. Input: raw PCM 8kHz 16-bit mono. Nova-2, streaming-capable."""
 
import asyncio
import concurrent.futures
import time
from typing import Any
 
from deepgram import DeepgramClient
 
from adapters.base import AdapterException, STTAdapter, STTResponse
from config.settings import Settings
 
# Bounded executor — prevents unbounded thread growth under bulk calls.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="deepgram")
 
 
class DeepgramSTTAdapter(STTAdapter):
    """Deepgram Nova-2. Single transcribe() uses transcribe_file; returns final result."""
 
    def __init__(self, **kwargs: Any) -> None:
        settings = Settings()
        self._client = DeepgramClient(api_key=settings.deepgram_api_key)
        self._timeout = getattr(settings, "stt_tts_max_latency", 8.0)
 
    async def transcribe(
        self,
        audio_bytes: bytes,
        language: str = "en",
        auto_detect: bool = False,
    ) -> STTResponse:
        start = time.perf_counter()
 
        def _sync_call() -> Any:
            return self._client.listen.v1.media.transcribe_file(
                request=audio_bytes,
                model="nova-2",
                language=language if not auto_detect else None,
                smart_format=True,
                encoding="linear16",
            )
 
        loop = asyncio.get_running_loop()
        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(_EXECUTOR, _sync_call),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as e:
            raise AdapterException(
                f"Deepgram request timed out after {self._timeout}s"
            ) from e
        except Exception as e:
            err = str(e).lower()
            if "401" in str(e) or "auth" in err or "unauthorized" in err:
                raise AdapterException(f"Deepgram authentication failed: {e}") from e
            if "429" in str(e) or "rate" in err:
                retry_after = 60.0
                raise AdapterException(
                    f"Deepgram rate limit exceeded: {e}",
                    retry_after=retry_after,
                ) from e
            if "timeout" in err or "deadline" in err:
                raise AdapterException(f"Deepgram request timed out: {e}") from e
            if "503" in str(e) or "unavailable" in err:
                raise AdapterException(f"Deepgram service unavailable: {e}") from e
            raise AdapterException(f"Deepgram error: {e}") from e
 
        latency_ms = (time.perf_counter() - start) * 1000
        text = ""
        detected = language if not auto_detect else "en"
        confidence = 1.0
        if hasattr(response, "results") and response.results is not None:
            results = response.results
            if hasattr(results, "channels") and results.channels:
                ch = results.channels[0] if results.channels else None
                if ch and hasattr(ch, "alternatives") and ch.alternatives:
                    alt = ch.alternatives[0]
                    text = getattr(alt, "transcript", "") or ""
                    confidence = getattr(alt, "confidence", 1.0) or 1.0
            if hasattr(response, "metadata") and response.metadata:
                meta = response.metadata
                if hasattr(meta, "detected_language") and meta.detected_language:
                    detected = getattr(meta.detected_language, "language", detected) or detected
        return STTResponse(
            text=text,
            detected_language=detected,
            confidence=confidence,
            is_final=True,
        )