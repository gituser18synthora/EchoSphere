"""OpenAI Whisper STT adapter. Input: raw PCM 8kHz 16-bit mono."""

import asyncio
import time
from typing import Any

from openai import AsyncOpenAI, APIError, APITimeoutError, AuthenticationError, RateLimitError

from adapters.audio_utils import pcm_to_wav_bytes
from adapters.base import AdapterException, STTAdapter, STTResponse
from config.settings import Settings


class WhisperSTTAdapter(STTAdapter):
    """OpenAI Whisper API. Uses openai_api_key. Batch only -> is_final=True."""

    def __init__(self, **kwargs: Any) -> None:
        settings = Settings()
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._timeout = getattr(settings, "stt_tts_max_latency", 2.0)

    async def transcribe(
        self,
        audio_bytes: bytes,
        language: str = "en",
        auto_detect: bool = False,
    ) -> STTResponse:
        # Wrap PCM 8kHz 16-bit mono in WAV for Whisper
        wav_bytes = pcm_to_wav_bytes(audio_bytes, sample_rate=8000)
        start = time.perf_counter()

        async def _call() -> Any:
            return await self._client.audio.transcriptions.create(
                model="whisper-1",
                file=("audio.wav", wav_bytes, "audio/wav"),
                language=None if auto_detect else language,
            )

        try:
            response = await asyncio.wait_for(_call(), timeout=self._timeout)
        except asyncio.TimeoutError as e:
            raise AdapterException(
                f"Whisper request timed out after {self._timeout}s"
            ) from e
        except AuthenticationError as e:
            raise AdapterException(f"Whisper authentication failed: {e}") from e
        except RateLimitError as e:
            retry_after = None
            if hasattr(e, "response") and e.response is not None:
                h = getattr(e.response, "headers", None) or {}
                ra = h.get("retry-after")
                if ra is not None:
                    try:
                        retry_after = float(ra)
                    except (TypeError, ValueError):
                        pass
            raise AdapterException(
                f"Whisper rate limit exceeded: {e}",
                retry_after=retry_after,
            ) from e
        except APITimeoutError as e:
            raise AdapterException(f"Whisper request timed out: {e}") from e
        except APIError as e:
            status = getattr(e, "status_code", None)
            if status == 401 or (status and "auth" in str(e).lower()):
                raise AdapterException(f"Whisper authentication failed: {e}") from e
            if status == 503 or (status and status >= 500):
                raise AdapterException(f"Whisper service unavailable: {e}") from e
            raise AdapterException(f"Whisper API error: {e}") from e
        except Exception as e:
            if "401" in str(e) or "auth" in str(e).lower() or "unauthorized" in str(e).lower():
                raise AdapterException(f"Whisper authentication failed: {e}") from e
            raise

        latency_ms = (time.perf_counter() - start) * 1000
        text = getattr(response, "text", "") or ""
        return STTResponse(
            text=text,
            detected_language=language if not auto_detect else "en",
            confidence=1.0,
            is_final=True,
        )
