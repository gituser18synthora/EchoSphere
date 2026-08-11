"""Sarvam AI TTS (Bulbul) — lazy-imports the sarvamai SDK.

Migrated from the legacy voice engines sarvam_adapter.py. Keeps the
script-based Indic language auto-detection but honors an explicit
``language`` argument first. The WAV parsing and resampling now use the
shared numpy helpers in shared.audio.pcm (replacing the
legacy per-sample pure-python loop), and output is 16 kHz 16-bit mono PCM.
"""

import asyncio
import base64
import logging
import re
import time

from shared.config import get_settings
from shared.providers.base import ProviderConfig, ProviderError, TTSProvider, TTSResult
from shared.providers.languages import SARVAM_SUPPORTED_LOCALES, short_code_to_locale
from shared.audio.pcm import resample_pcm, wav_to_pcm
from shared.audio.text import sanitize_for_tts

_PCM_RATE = 16000

# Unicode ranges for Indic scripts → Sarvam language code.
_SCRIPT_LANG_MAP = [
    (r"[\u0900-\u097F]", "hi-IN"),  # Devanagari -> Hindi
    (r"[\u0980-\u09FF]", "bn-IN"),  # Bengali
    (r"[\u0A00-\u0A7F]", "pa-IN"),  # Gurmukhi -> Punjabi
    (r"[\u0A80-\u0AFF]", "gu-IN"),  # Gujarati
    (r"[\u0B00-\u0B7F]", "or-IN"),  # Odia
    (r"[\u0B80-\u0BFF]", "ta-IN"),  # Tamil
    (r"[\u0C00-\u0C7F]", "te-IN"),  # Telugu
    (r"[\u0C80-\u0CFF]", "kn-IN"),  # Kannada
    (r"[\u0D00-\u0D7F]", "ml-IN"),  # Malayalam
]
_COMPILED_SCRIPT_MAP = [(re.compile(pattern), lang) for pattern, lang in _SCRIPT_LANG_MAP]

# Language canonicalization is shared with the WebSocket implementation via
# shared.providers.languages (SARVAM_SUPPORTED_LOCALES + short_code_to_locale)
# so REST and streaming can never diverge on locale handling again.

logger = logging.getLogger(__name__)

# Used only when NO speaker is configured. Speaker validity is enforced by the
# DB voice catalog (backend/core/provider_catalog.py) — the single source of
# truth — and ultimately by the Sarvam API itself; an unknown speaker surfaces
# as a ProviderError("invalid_input"), never as a silent substitution.
_MODEL_DEFAULT_SPEAKER = {"bulbul:v2": "anushka", "bulbul:v3": "shubh"}
_FALLBACK_DEFAULT_SPEAKER = "shubh"


def _detect_language(text: str) -> str:
    """Detect a Sarvam language code from the script used in ``text``.

    Mixed text picks the script with the most characters; plain Latin text
    falls back to "en-IN".
    """
    counts: dict[str, int] = {}
    for pattern, lang in _COMPILED_SCRIPT_MAP:
        matches = pattern.findall(text)
        if matches:
            counts[lang] = len(matches)
    if not counts:
        return "en-IN"
    return max(counts, key=lambda key: counts[key])


def _resolve_language(explicit: str | None, text: str) -> str:
    if explicit:
        code = short_code_to_locale("sarvam", explicit)
        if code in SARVAM_SUPPORTED_LOCALES:
            return code
    return _detect_language(text)


def _normalize_speaker(voice: object) -> str:
    """Sarvam speaker wire codes are lowercase strings without padding."""
    return str(voice).strip().lower() if voice is not None else ""


class SarvamTTS(TTSProvider):
    name = "sarvam-tts"

    def __init__(self, config: ProviderConfig) -> None:
        try:
            from sarvamai import AsyncSarvamAI
        except ImportError as exc:
            raise ProviderError(
                self.name, "invalid_input",
                "sarvamai SDK is not installed; run `pip install sarvamai` "
                "to use the sarvam TTS provider",
            ) from exc
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.tts_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._client = AsyncSarvamAI(
            api_subscription_key=key, timeout=config.timeout_seconds
        )
        self._model = config.model or "bulbul:v3"
        self._voice = config.voice or ""
        self._language = config.language or ""
        self._timeout = config.timeout_seconds
        # Fixed output rate — synthesize() resamples any other WAV rate to
        # it; consumers resample when their pipeline differs.
        self.output_sample_rate = _PCM_RATE

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None,
        speed: float = 1.0,
    ) -> TTSResult:
        text = sanitize_for_tts(text)
        if not text:
            return TTSResult(audio=b"", sample_rate=_PCM_RATE)
        started = time.perf_counter()
        language_code = _resolve_language(language or self._language or None, text)
        speaker = _normalize_speaker(voice) or _normalize_speaker(self._voice)
        if not speaker:
            speaker = _MODEL_DEFAULT_SPEAKER.get(self._model, _FALLBACK_DEFAULT_SPEAKER)
            logger.info(
                "sarvam-tts: no speaker configured; using model default '%s' for %s",
                speaker, self._model,
            )
        try:
            response = await asyncio.wait_for(
                self._client.text_to_speech.convert(
                    text=text,
                    model=self._model,
                    target_language_code=language_code,
                    speaker=speaker,
                    pace=max(0.5, min(2.0, speed)),
                    speech_sample_rate=_PCM_RATE,
                    output_audio_codec="wav",
                ),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            raise ProviderError(
                self.name, "timeout", f"Request timed out after {self._timeout}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — SDK error types are lazy-loaded
            raise _categorize(self.name, exc) from exc

        audios = getattr(response, "audios", None) or []
        pcm = b""
        if audios:
            try:
                wav_bytes = base64.b64decode(audios[0], validate=True)
                pcm, rate = wav_to_pcm(wav_bytes)
            except Exception as exc:  # noqa: BLE001 — corrupt payload, not our bug
                raise ProviderError(
                    self.name, "upstream",
                    f"Provider returned an unparseable audio payload: {type(exc).__name__}",
                ) from exc
            if not pcm:
                # wav_to_pcm returns (b"", 0) for non-WAV bytes — corrupt payload.
                raise ProviderError(
                    self.name, "upstream",
                    "Provider returned an audio payload that is not a 16-bit PCM WAV",
                )
            if rate and rate != _PCM_RATE:
                pcm = resample_pcm(pcm, rate, _PCM_RATE)
        else:
            logger.warning(
                "sarvam-tts: provider returned no audio for speaker '%s' (%s)",
                speaker, self._model,
            )
        return TTSResult(
            audio=pcm,
            sample_rate=_PCM_RATE,
            duration_ms=(time.perf_counter() - started) * 1000,
        )


def _categorize(provider: str, exc: Exception) -> ProviderError:
    # sarvamai SDK errors expose status_code/body; prefer the structured
    # message over str(exc), which leads with an unreadable header dump.
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    detail = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
    text = detail or str(exc)
    lowered = text.lower()
    if status in (401, 403) or "401" in text or "403" in text or "unauthorized" in lowered:
        return ProviderError(provider, "auth", text[:300])
    if status == 429 or "429" in text or "rate limit" in lowered:
        return ProviderError(provider, "rate_limit", text[:300])
    if status == 400 or "invalid_request" in lowered:
        # e.g. "Speaker 'x' is not compatible with model bulbul:v3. …" —
        # configuration errors must surface, never trigger engine fallback.
        return ProviderError(provider, "invalid_input", text[:300])
    if "timeout" in lowered or "timed out" in lowered:
        return ProviderError(provider, "timeout", text[:300])
    return ProviderError(provider, "upstream", text[:300])
