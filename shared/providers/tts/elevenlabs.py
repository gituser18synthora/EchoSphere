"""ElevenLabs TTS via the REST text-to-speech endpoint.

Used for previews and live segments of models the ElevenLabs realtime
WebSocket does not accept (Eleven v3); streaming-capable models use
``elevenlabs_ws.ElevenLabsWebSocketTTSProvider`` instead. Uses httpx REST
(no elevenlabs SDK) and requests PCM output at the consumer's rate when
ElevenLabs serves it natively (8/16/22.05/24 kHz), else pcm_16000.

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
from shared.providers.languages import (
    ELEVENLABS_LANGUAGE_ENFORCING_MODELS,
    elevenlabs_language_code,
    elevenlabs_supports_language,
    elevenlabs_unsupported_language_message,
)
from shared.providers.tts.delivery import provider_speed

logger = logging.getLogger("providers.tts.elevenlabs")

_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_PCM_RATE = 16000
# PCM output rates ElevenLabs serves on every plan (44.1 kHz needs Pro). The
# consumer may ask for its own pipeline rate via ``extra["output_sample_rate"]``
# so a 24 kHz call or preview is not synthesized at 16 kHz and upsampled.
_SUPPORTED_PCM_RATES = (8000, 16000, 22050, 24000)

# Catalog-governed configurations always carry a model; this guard only covers
# direct ProviderConfig construction without one. Matches the WS adapter.
_DEFAULT_MODEL = "eleven_flash_v2_5"

# Which models accept the language_code enforcement parameter, and which
# languages each one speaks, both live in shared.providers.languages — the one
# place that knows ElevenLabs' wire spelling (bare ISO 639-1, never a locale).
_LANGUAGE_ENFORCING_MODELS = ELEVENLABS_LANGUAGE_ENFORCING_MODELS

# voice_settings fields per model family. Eleven v3 (alpha) supports only the
# documented v3 settings; sending speed/use_speaker_boost is rejected.
_V3_VOICE_SETTING_KEYS = ("stability", "similarity_boost", "style")
_FULL_VOICE_SETTING_KEYS = ("stability", "similarity_boost", "style",
                            "use_speaker_boost", "speed")


def _unsupported_language_error(provider: str, model: str, language: str) -> ProviderError:
    """Refusal for a model that provably cannot speak the language.

    Omitting ``language_code`` is NOT a workaround: the model still cannot
    produce that language. ElevenLabs either rejects the request outright
    (HTTP 400 / a 1008 ``unsupported_language`` frame) or — with the parameter
    dropped — returns audio that mispronounces the text in a language it was
    never trained on. The guidance comes from shared.providers.languages so
    every call site names the same, actually-selectable alternative.
    """
    return ProviderError(
        provider, "invalid_input",
        elevenlabs_unsupported_language_message(model, language),
    )


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
        # Output rate: the consumer's pipeline rate when it is one ElevenLabs
        # serves natively, else 16 kHz (consumers resample the remainder).
        requested = self._params.pop("output_sample_rate", None)
        try:
            requested = int(requested) if requested is not None else None
        except (TypeError, ValueError):
            requested = None
        self.output_sample_rate = (
            requested if requested in _SUPPORTED_PCM_RATES else _PCM_RATE
        )

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
            return TTSResult(audio=b"", sample_rate=self.output_sample_rate)
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
        if language and elevenlabs_supports_language(self._model, language) is False:
            raise _unsupported_language_error(self.name, self._model, language)
        if language and self._model in _LANGUAGE_ENFORCING_MODELS:
            iso = elevenlabs_language_code(self._model, language)
            if iso:
                payload["language_code"] = iso
        started = time.perf_counter()
        try:
            response = await self._client.post(
                _TTS_URL.format(voice_id=voice_id),
                params={"output_format": f"pcm_{self.output_sample_rate}"},
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
            sample_rate=self.output_sample_rate,
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
