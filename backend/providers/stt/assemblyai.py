"""AssemblyAI STT via the REST API (upload → create transcript → poll).

Migrated from VoiceBot/adapters/stt/assemblyai_adapter.py. The legacy adapter
mutated the SDK's global ``aai.settings.api_key``; this port uses httpx REST
with a per-instance client, so no assemblyai SDK and no global state.
"""

import asyncio
import time

import httpx

from backend.config import get_settings
from backend.providers.base import ProviderConfig, ProviderError, STTProvider, STTResult
from backend.voice_runtime.audio.pcm import pcm_to_wav_bytes

_UPLOAD_URL = "https://api.assemblyai.com/v2/upload"
_TRANSCRIPT_URL = "https://api.assemblyai.com/v2/transcript"
_POLL_INTERVAL_SECONDS = 0.5


class AssemblyAISTT(STTProvider):
    name = "assemblyai"

    def __init__(self, config: ProviderConfig) -> None:
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.stt_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds,
            headers={"Authorization": key},
        )
        self._language = config.language or None
        self._timeout = config.timeout_seconds

    async def transcribe(
        self, audio: bytes, *, sample_rate: int = 16000, language: str | None = None
    ) -> STTResult:
        if not audio:
            return STTResult(text="")
        started = time.perf_counter()
        deadline = started + self._timeout
        wav = pcm_to_wav_bytes(audio, sample_rate)
        lang = language or self._language

        upload = await self._request(
            "POST", _UPLOAD_URL, content=wav,
            headers={"Content-Type": "application/octet-stream"},
        )
        upload_url = upload.get("upload_url")
        if not upload_url:
            raise ProviderError(self.name, "upstream", "Upload did not return an upload_url")

        body: dict = {"audio_url": upload_url}
        if lang:
            body["language_code"] = lang
        job = await self._request("POST", _TRANSCRIPT_URL, json=body)
        transcript_id = job.get("id")
        if not transcript_id:
            raise ProviderError(self.name, "upstream", "Transcript request did not return an id")

        while True:
            if time.perf_counter() >= deadline:
                raise ProviderError(
                    self.name, "timeout",
                    f"Transcription did not complete within {self._timeout}s",
                )
            status = await self._request("GET", f"{_TRANSCRIPT_URL}/{transcript_id}")
            state = status.get("status")
            if state == "completed":
                break
            if state == "error":
                raise ProviderError(
                    self.name, "upstream",
                    str(status.get("error") or "Transcription failed")[:200],
                )
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        return STTResult(
            text=(status.get("text") or "").strip(),
            language=status.get("language_code") or lang,
            confidence=status.get("confidence"),
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = await self._client.request(method, url, **kwargs)
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
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(self.name, "upstream", "Non-JSON response from AssemblyAI") from exc
