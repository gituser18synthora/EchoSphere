"""Deepgram STT (nova-2) via the REST /v1/listen endpoint.

Migrated from the legacy voice engines deepgram_adapter.py. The legacy adapter
called a non-existent SDK path (``client.listen.v1.media.transcribe_file``);
this port talks to Deepgram's documented REST API with httpx directly, so no
deepgram SDK is required.

Vendor-level configuration — the API key reference, the regional host and the
auth header shape — comes from :mod:`shared.providers.deepgram_common`, which
the Deepgram TTS adapters share. Only the /v1/listen wire protocol lives here;
the realtime Flux path is a separate implementation
(``voice_runtime/deepgram_stt.py``) and neither knows about the other.
"""

import time

import httpx

from shared.config import get_settings
from shared.providers.base import ProviderConfig, ProviderError, STTProvider, STTResult
from shared.providers.deepgram_common import (
    auth_headers,
    resolve_api_key,
    rest_base_url,
)
from shared.audio.pcm import pcm_to_wav_bytes

_LISTEN_PATH = "/v1/listen"


class DeepgramSTT(STTProvider):
    name = "deepgram"

    def __init__(self, config: ProviderConfig) -> None:
        settings = get_settings()
        key = resolve_api_key(
            config.api_key_reference, settings.stt_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        # Data residency: the platform default (DEEPGRAM_REGION) unless this
        # engine pins its own region. Same key and same API on every host.
        self._base_url = rest_base_url((config.extra or {}).get("region"))
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds,
            headers=auth_headers(key),
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
                f"{self._base_url}{_LISTEN_PATH}",
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
    """Deliberately NOT the shared deepgram_common helper.

    That one categorizes 400/404/422 as ``invalid_input``, which is what the
    TTS adapters need so a bad model/encoding never triggers engine fallback.
    This endpoint has always reported them as ``upstream``, and changing that
    here would be an unrelated STT behaviour change.
    """
    if response.status_code < 400:
        return
    detail = response.text[:200]
    if response.status_code in (401, 403):
        raise ProviderError(provider, "auth", f"HTTP {response.status_code}: {detail}")
    if response.status_code == 429:
        raise ProviderError(provider, "rate_limit", f"HTTP 429: {detail}")
    raise ProviderError(provider, "upstream", f"HTTP {response.status_code}: {detail}")
