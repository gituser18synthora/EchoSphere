"""OpenAI TTS provider (tts-1 / gpt-4o-mini-tts), PCM output."""

import time

from openai import AsyncOpenAI

from shared.config import get_settings
from shared.providers.base import ProviderConfig, ProviderError, TTSProvider, TTSResult

_OPENAI_PCM_RATE = 24000


class OpenAITTS(TTSProvider):
    name = "openai-tts"

    def __init__(self, config: ProviderConfig) -> None:
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.tts_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._client = AsyncOpenAI(api_key=key, timeout=config.timeout_seconds)
        self._model = config.model or settings.tts_model or "tts-1"
        self._voice = config.voice or settings.tts_voice or "alloy"

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None,
        speed: float = 1.0,
    ) -> TTSResult:
        if not text.strip():
            return TTSResult(audio=b"", sample_rate=_OPENAI_PCM_RATE)
        started = time.perf_counter()
        try:
            response = await self._client.audio.speech.create(
                model=self._model,
                voice=voice or self._voice,
                input=text,
                response_format="pcm",
                speed=max(0.25, min(4.0, speed)),
            )
            audio = response.content
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(self.name, "upstream", str(exc)[:200]) from exc
        return TTSResult(
            audio=audio,
            sample_rate=_OPENAI_PCM_RATE,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
