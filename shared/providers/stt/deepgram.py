"""Deepgram STT (nova-2) via the REST /v1/listen endpoint.

Migrated from the legacy voice engines deepgram_adapter.py. The legacy adapter
called a non-existent SDK path (``client.listen.v1.media.transcribe_file``);
this port talks to Deepgram's documented REST API with httpx directly, so no
deepgram SDK is required.
"""

import time

import httpx

from shared.config import get_settings
from shared.providers.base import ProviderConfig, ProviderError, STTProvider, STTResult
from shared.audio.pcm import pcm_to_wav_bytes

_LISTEN_URL = "https://api.deepgram.com/v1/listen"


class DeepgramSTT(STTProvider):
    name = "deepgram"

    def __init__(self, config: ProviderConfig) -> None:
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.stt_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds,
            headers={"Authorization": f"Token {key}"},
        )
        self._model = config.model or "nova-2"
        self._language = config.language or None
        self._timeout = config.timeout_seconds

    async def transcribe(
        self, audio: bytes, *, sample_rate: int = 16000, language: str | None = None
    ) -> STTResult:
        if not audio:
            return STTResult(text="")
        started = time.perf_counter()
        wav = pcm_to_wav_bytes(audio, sample_rate)
        params: dict[str, str] = {"model": self._model, "smart_format": "true"}
        lang = language or self._language
        if lang:
            params["language"] = lang
        try:
            response = await self._client.post(
                _LISTEN_URL,
                params=params,
                content=wav,
                headers={"Content-Type": "audio/wav"},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                self.name, "timeout", f"Request timed out after {self._timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, "upstream", str(exc)[:200]) from exc
        _raise_for_status(self.name, response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(self.name, "upstream", "Non-JSON response from Deepgram") from exc
        channels = (payload.get("results") or {}).get("channels") or []
        alternatives = (channels[0].get("alternatives") if channels else None) or []
        text = (alternatives[0].get("transcript") if alternatives else "") or ""
        confidence = alternatives[0].get("confidence") if alternatives else None
        return STTResult(
            text=text.strip(),
            language=lang,
            confidence=confidence,
            duration_ms=(time.perf_counter() - started) * 1000,
        )


def _raise_for_status(provider: str, response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    detail = response.text[:200]
    if response.status_code in (401, 403):
        raise ProviderError(provider, "auth", f"HTTP {response.status_code}: {detail}")
    if response.status_code == 429:
        raise ProviderError(provider, "rate_limit", f"HTTP 429: {detail}")
    raise ProviderError(provider, "upstream", f"HTTP {response.status_code}: {detail}")
