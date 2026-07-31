"""ElevenLabs TTS via the REST text-to-speech endpoint.

Used for previews and live segments of models the ElevenLabs realtime
WebSocket does not accept (Eleven v3); streaming-capable models use
``elevenlabs_ws.ElevenLabsWebSocketTTSProvider`` instead. Uses httpx REST
(no elevenlabs SDK) and requests pcm_16000 output directly.

The selected model is passed through dynamically (``model_id`` in the
request body) — never hardcoded per request — and only the voice settings
the model supports are sent:

- Eleven v3 (alpha): stability (discrete 0.0/0.5/1.0), similarity_boost,
  style. speed and use_speaker_boost are NOT supported and never sent; the
  ``language_code`` enforcement parameter is Flash/Turbo v2.5 only.
- v2.5 family: full voice settings incl. use_speaker_boost and speed, plus
  ``language_code`` enforcement.
"""

import logging
import time

import httpx

from shared.config import get_settings
from shared.providers.base import ProviderConfig, ProviderError, TTSProvider, TTSResult
from shared.providers.languages import to_provider_language
from shared.providers.tts.delivery import provider_speed

logger = logging.getLogger("providers.tts.elevenlabs")

_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_PCM_RATE = 16000

# Catalog-governed configurations always carry a model; this guard only covers
# direct ProviderConfig construction without one. Matches the WS adapter.
_DEFAULT_MODEL = "eleven_flash_v2_5"

# Models that accept the language_code enforcement parameter (official docs:
# Flash/Turbo v2.5 only — Eleven v3 rejects it).
_LANGUAGE_ENFORCING_MODELS = {"eleven_flash_v2_5", "eleven_turbo_v2_5"}

# voice_settings fields per model family. Eleven v3 (alpha) supports only the
# documented v3 settings; sending speed/use_speaker_boost is rejected.
_V3_VOICE_SETTING_KEYS = ("stability", "similarity_boost", "style")
_FULL_VOICE_SETTING_KEYS = ("stability", "similarity_boost", "style",
                            "use_speaker_boost", "speed")


def voice_setting_keys(model: str) -> tuple[str, ...]:
    """Voice-settings fields the given ElevenLabs model accepts."""
    if model == "eleven_v3":
        return _V3_VOICE_SETTING_KEYS
    return _FULL_VOICE_SETTING_KEYS


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
        self._model = (config.model or "").strip() or _DEFAULT_MODEL
        self._voice = config.voice or ""
        self._timeout = config.timeout_seconds
        # Provider-specific synthesis parameters already validated against the
        # model's catalog schema (bot tts_settings / preview params).
        self._params = dict(config.extra or {})
        # Fixed output rate — consumers resample when their pipeline differs.
        self.output_sample_rate = _PCM_RATE

    def _voice_settings(self, speed: float) -> dict:
        allowed = voice_setting_keys(self._model)
        settings = {
            key: self._params[key]
            for key in allowed if self._params.get(key) is not None
        }
        if speed:
            if "speed" in allowed:
                # Canonical Delivery-tuning speed is authoritative: it
                # overrides any legacy speed left in stored provider params.
                settings["speed"] = provider_speed("elevenlabs", self._model, speed)
            elif speed != 1.0:
                logger.debug(
                    "elevenlabs: model %s does not support the speed setting — "
                    "ignoring speed=%.2f", self._model, speed,
                )
        return settings

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
        payload: dict = {"text": text, "model_id": self._model}
        voice_settings = self._voice_settings(speed)
        if voice_settings:
            payload["voice_settings"] = voice_settings
        if language and self._model in _LANGUAGE_ENFORCING_MODELS:
            iso = to_provider_language("elevenlabs", language)
            if iso:
                payload["language_code"] = iso.split("-")[0]
        started = time.perf_counter()
        try:
            response = await self._client.post(
                _TTS_URL.format(voice_id=voice_id),
                params={"output_format": f"pcm_{_PCM_RATE}"},
                json=payload,
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
            if response.status_code in (400, 422):
                # Model/voice/parameter rejections are configuration errors —
                # surfaced as-is, never silently retried on another model.
                raise ProviderError(
                    self.name, "invalid_input", f"HTTP {response.status_code}: {detail}"
                )
            raise ProviderError(self.name, "upstream", f"HTTP {response.status_code}: {detail}")
        return TTSResult(
            audio=response.content,
            sample_rate=_PCM_RATE,
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
