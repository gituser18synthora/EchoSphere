"""OpenAI Whisper STT (batch transcription of complete utterances).

Migrated from the legacy voice engines whisper_adapter.py: same PCM→WAV approach,
with a realistic timeout and latency reported on the result.
"""

import io
import time
import wave

from openai import AsyncOpenAI

from backend.config import get_settings
from backend.providers.base import ProviderConfig, ProviderError, STTProvider, STTResult


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


class WhisperSTT(STTProvider):
    name = "openai-whisper"

    def __init__(self, config: ProviderConfig) -> None:
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.stt_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._client = AsyncOpenAI(api_key=key, timeout=config.timeout_seconds)
        self._model = config.model or settings.stt_model or "whisper-1"
        self._language = config.language or None

    async def transcribe(
        self, audio: bytes, *, sample_rate: int = 16000, language: str | None = None
    ) -> STTResult:
        if not audio:
            return STTResult(text="")
        started = time.perf_counter()
        wav = pcm_to_wav_bytes(audio, sample_rate)
        try:
            response = await self._client.audio.transcriptions.create(
                model=self._model,
                file=("audio.wav", wav, "audio/wav"),
                language=(language or self._language or None),
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(self.name, "upstream", str(exc)[:200]) from exc
        return STTResult(
            text=(response.text or "").strip(),
            language=language or self._language,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
