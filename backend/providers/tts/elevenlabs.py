"""ElevenLabs TTS via the REST text-to-speech endpoint.

Migrated from the legacy voice engines elevenlabs_adapter.py. Uses httpx REST
(no elevenlabs SDK) and requests pcm_16000 output directly, which removes the
legacy 22050 Hz to 8 kHz pure-python resample step entirely.
"""

import time

import httpx

from backend.config import get_settings
from backend.providers.base import ProviderConfig, ProviderError, TTSProvider, TTSResult

_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_PCM_RATE = 16000


class ElevenLabsTTS(TTSProvider):
    name = "elevenlabs"

    def __init__(self, config: ProviderConfig) -> None:
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.tts_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds,
            headers={"xi-api-key": key},
        )
        self._model = config.model or "eleven_turbo_v2"
        self._voice = config.voice or ""
        self._timeout = config.timeout_seconds

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None,
        speed: float = 1.0,
    ) -> TTSResult:
        if not text.strip():
            return TTSResult(audio=b"", sample_rate=_PCM_RATE)
        voice_id = (voice or self._voice).strip()
        if not voice_id:
            raise ProviderError(
                self.name, "invalid_input",
                "ElevenLabs requires a voice id (set config.voice or pass voice=)",
            )
        started = time.perf_counter()
        try:
            response = await self._client.post(
                _TTS_URL.format(voice_id=voice_id),
                params={"output_format": f"pcm_{_PCM_RATE}"},
                json={"text": text, "model_id": self._model},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                self.name, "timeout", f"Request timed out after {self._timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, "upstream", str(exc)[:200]) from exc
        if response.status_code >= 400:
            detail = response.text[:200]
            if response.status_code in (401, 403):
                raise ProviderError(self.name, "auth", f"HTTP {response.status_code}: {detail}")
            if response.status_code == 429:
                raise ProviderError(self.name, "rate_limit", f"HTTP 429: {detail}")
            raise ProviderError(self.name, "upstream", f"HTTP {response.status_code}: {detail}")
        return TTSResult(
            audio=response.content,
            sample_rate=_PCM_RATE,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
