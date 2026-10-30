"""Deepgram TTS (Aura / Aura-2) via the REST ``/v1/speak`` endpoint.

Used for previews and for any consumer that wants one finished segment; live
calls stream over :mod:`shared.providers.tts.deepgram_ws` instead. httpx REST
only — no Deepgram SDK is required, exactly like the Deepgram STT adapter.

Wire shape (developers.deepgram.com/reference/text-to-speech-api/speak,
verified 2026-09-21)::

    POST {base}/v1/speak?model=aura-2-thalia-en&encoding=linear16
                         &sample_rate=24000&container=none[&speed=1.1]
    Authorization: Token <key>
    {"text": "..."}
    → raw 16-bit little-endian mono PCM

``container=none`` is what makes the response headerless PCM the pipeline can
use directly; without it Deepgram wraps linear16 in a WAV.

Model vs voice
--------------
Deepgram has no separate voice parameter: the ``model`` query parameter IS
the voice (``aura-2-thalia-en``). The platform catalog splits that into a
model row for the Aura *generation* (``aura-2`` / ``aura``) and a voice row
holding the full voice id, so the provider/model/voice UI behaves the same as
for every other vendor. This adapter recombines them, and refuses a voice
from the wrong generation instead of letting Deepgram 400 on it.

Language
--------
There is no language field to send, so a wrong locale cannot be rejected
upstream: Deepgram would read Devanagari with an English voice and return
confident gibberish. The refusal therefore happens here, against the explicit
capability tables in :mod:`shared.providers.languages`.
"""

import logging
import time

import httpx

from shared.audio.text import sanitize_for_tts
from shared.providers.base import ProviderConfig, ProviderError, TTSProvider, TTSResult
from shared.providers.deepgram_common import (
    auth_headers,
    raise_for_status,
    resolve_api_key,
    rest_base_url,
)
from shared.providers.languages import (
    deepgram_supports_language,
    deepgram_tts_language_tag,
    deepgram_unsupported_language_message,
    deepgram_voice_language_tag,
    deepgram_voice_model_family,
)
from shared.providers.tts.deepgram_voices import (
    DEFAULT_SAMPLE_RATE,
    DEFAULT_VOICE_FOR_MODEL,
    is_native_sample_rate,
)
from shared.providers.tts.delivery import provider_speed

logger = logging.getLogger("providers.tts.deepgram")

_SPEAK_PATH = "/v1/speak"
# Catalog-governed configurations always carry a model; this guard only covers
# direct ProviderConfig construction without one. Matches the WS adapter.
_DEFAULT_MODEL = "aura-2"


def unsupported_language_error(
    provider: str, model: str, language: str
) -> ProviderError:
    """Refusal for a language Deepgram has no voice for.

    Shared with the WebSocket adapter so both transports refuse identically
    and name the same alternative.
    """
    return ProviderError(
        provider, "invalid_input",
        deepgram_unsupported_language_message(model, language),
    )


def check_voice_language(
    provider: str, voice_model: str, language: str | None
) -> None:
    """Refuse a voice whose own language tag contradicts the locale.

    Belt and braces behind the model-level check: a catalog row could list an
    English voice under a Spanish locale, and the voice id is the only thing
    Deepgram actually obeys. Silent when either side is unmodelled.
    """
    if not language:
        return
    tag = deepgram_voice_language_tag(voice_model)
    expected = deepgram_tts_language_tag(language)
    if tag is None or expected is None or tag == expected:
        return
    raise ProviderError(
        provider, "invalid_input",
        f"Deepgram voice '{voice_model}' speaks '{tag}', not '{language}'. "
        f"Pick a '{expected}' voice for this language.",
    )


def resolve_wire_model(provider: str, model: str, voice: str) -> str:
    """The ``model`` query value: the voice id, checked against its family.

    A voice from another Aura generation is a configuration error — the
    catalog already scopes voices by ``model_codes``, so reaching here means
    something bypassed it. A MISSING voice falls back to the model default
    (logged), mirroring the Sarvam speaker rule; a WRONG one never does.
    """
    voice_id = (voice or "").strip()
    if not voice_id:
        fallback = DEFAULT_VOICE_FOR_MODEL.get(model)
        if not fallback:
            raise ProviderError(
                provider, "invalid_input",
                "Deepgram requires a voice (set config.voice or pass voice=)",
            )
        logger.info(
            "deepgram-tts: no voice configured; using model default '%s' for %s",
            fallback, model,
        )
        voice_id = fallback
    family = deepgram_voice_model_family(voice_id)
    if family is not None and family != model:
        raise ProviderError(
            provider, "invalid_input",
            f"Deepgram voice '{voice_id}' belongs to model '{family}', not the "
            f"selected model '{model}'.",
        )
    return voice_id


class DeepgramTTS(TTSProvider):
    name = "deepgram"

    def __init__(self, config: ProviderConfig) -> None:
        # Same account and the same env reference as Deepgram STT — the
        # platform never holds a second Deepgram credential.
        key = resolve_api_key(config.api_key_reference)
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._timeout = config.timeout_seconds
        self._model = (config.model or "").strip() or _DEFAULT_MODEL
        self._voice = (config.voice or "").strip()
        self._language = config.language or ""
        # Provider parameters already validated against the model's catalog
        # schema (bot tts_settings / preview params).
        self._params = dict(config.extra or {})
        # Data residency: the platform default (DEEPGRAM_REGION) unless this
        # engine pins its own region. Same key and API on every host.
        self._base_url = rest_base_url(self._params.get("region"))
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds, headers=auth_headers(key),
        )
        requested = self._params.pop("output_sample_rate", None)
        try:
            requested = int(requested) if requested is not None else None
        except (TypeError, ValueError):
            requested = None
        # Deepgram synthesizes natively at both pipeline rates (telephony
        # 8 kHz, browser 24 kHz), so a matching request means no resampling.
        self.output_sample_rate = (
            requested if is_native_sample_rate(requested) else DEFAULT_SAMPLE_RATE
        )

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None,
        speed: float = 1.0,
    ) -> TTSResult:
        text = sanitize_for_tts(text)
        if not text:
            return TTSResult(audio=b"", sample_rate=self.output_sample_rate)
        wire_model = resolve_wire_model(
            self.name, self._model, voice or self._voice
        )
        locale = language or self._language
        if locale and deepgram_supports_language(self._model, locale) is False:
            raise unsupported_language_error(self.name, self._model, locale)
        check_voice_language(self.name, wire_model, locale)

        params: dict[str, str] = {
            "model": wire_model,
            "encoding": "linear16",
            "sample_rate": str(self.output_sample_rate),
            # Headerless PCM — the pipeline's own format, no WAV to strip.
            "container": "none",
        }
        # Canonical Delivery speed is authoritative over a stored ``speed``
        # param; 1.0 is Deepgram's default, so the parameter is then omitted.
        requested_speed = speed if speed else self._params.get("speed")
        wire_speed = provider_speed("deepgram", self._model, requested_speed or 1.0)
        if wire_speed != 1.0:
            params["speed"] = f"{wire_speed:g}"

        started = time.perf_counter()
        try:
            response = await self._client.post(
                f"{self._base_url}{_SPEAK_PATH}", params=params, json={"text": text},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                self.name, "timeout", f"Request timed out after {self._timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, "upstream", str(exc)[:200]) from exc
        raise_for_status(self.name, response)
        return TTSResult(
            audio=response.content,
            sample_rate=self.output_sample_rate,
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
