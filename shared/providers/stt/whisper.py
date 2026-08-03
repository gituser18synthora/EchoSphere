"""OpenAI Whisper STT (batch transcription of complete utterances).

Migrated from the legacy voice engines whisper_adapter.py: same PCM→WAV approach,
with a realistic timeout and latency reported on the result.

whisper-* models are asked for ``verbose_json`` so the result carries the
detected language and per-segment quality (no-speech probability, token
log-probs) for the voice runtime's transcript gate; the newer gpt-4o-*
transcribe models only support the plain ``json`` format and report none of
that, so they keep the legacy request shape.
"""

import io
import math
import time
import wave

from openai import AsyncOpenAI

from shared.config import get_settings
from shared.providers.base import ProviderConfig, ProviderError, STTProvider, STTResult

# verbose_json reports the detected language as a lowercase English name.
_WHISPER_LANGUAGE_CODES = {
    "english": "en", "hindi": "hi", "bengali": "bn", "tamil": "ta",
    "telugu": "te", "marathi": "mr", "gujarati": "gu", "kannada": "kn",
    "malayalam": "ml", "punjabi": "pa", "urdu": "ur", "nepali": "ne",
    "assamese": "as", "odia": "or", "oriya": "or",
}


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _language_code(detected) -> str | None:
    """Normalize verbose_json's language ("english") to an ISO code ("en")."""
    if not detected:
        return None
    name = str(detected).strip().lower()
    if len(name) <= 3:  # already a code
        return name
    return _WHISPER_LANGUAGE_CODES.get(name)


def _segment_field(segment, name: str):
    if isinstance(segment, dict):
        return segment.get(name)
    return getattr(segment, name, None)


def _segment_quality(segments) -> tuple[float | None, float | None]:
    """(no_speech_prob, confidence) aggregated across verbose_json segments.

    Both take the utterance's WEAKEST-speech direction conservatively for
    real speech: no-speech uses min (if any segment is clearly speech the
    utterance contains speech) and confidence uses the minimum of
    exp(avg_logprob) (any truly garbage segment marks the utterance).
    """
    no_speech_values: list[float] = []
    confidences: list[float] = []
    for segment in segments or []:
        no_speech = _segment_field(segment, "no_speech_prob")
        if no_speech is not None:
            no_speech_values.append(float(no_speech))
        avg_logprob = _segment_field(segment, "avg_logprob")
        if avg_logprob is not None:
            confidences.append(math.exp(min(float(avg_logprob), 0.0)))
    return (
        min(no_speech_values) if no_speech_values else None,
        min(confidences) if confidences else None,
    )


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
        wants_quality = self._model.startswith("whisper")
        extra: dict = {"response_format": "verbose_json"} if wants_quality else {}
        try:
            response = await self._client.audio.transcriptions.create(
                model=self._model,
                file=("audio.wav", wav, "audio/wav"),
                language=(language or self._language or None),
                **extra,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(self.name, "upstream", str(exc)[:200]) from exc
        detected = _language_code(getattr(response, "language", None))
        no_speech_prob, confidence = _segment_quality(
            getattr(response, "segments", None)
        )
        return STTResult(
            text=(response.text or "").strip(),
            language=detected or language or self._language,
            confidence=confidence,
            no_speech_prob=no_speech_prob,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
