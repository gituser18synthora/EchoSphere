"""Deepgram TTS wire constants shared by the REST and WebSocket adapters.

Output formats and per-model defaults only — no connection or protocol logic.
Vendor-level configuration (credentials, regional hosts, auth) lives in
:mod:`shared.providers.deepgram_common`; language capability lives in
:mod:`shared.providers.languages`.

Verified 2026-09-21 against
developers.deepgram.com/reference/text-to-speech-api/speak and
developers.deepgram.com/reference/text-to-speech-api/speak-streaming.
"""

from __future__ import annotations

#: Sample rates Deepgram synthesizes natively for linear16. Both of the
#: platform's pipeline rates are on it — telephony 8 kHz (voice_runtime/app.py
#: pins the PSTN leg there end to end) and browser 24 kHz — so asking for the
#: consumer's own rate means no resampling anywhere on the audio path.
NATIVE_SAMPLE_RATES: frozenset[int] = frozenset({8000, 16000, 24000, 32000, 48000})

#: Fallback when the consumer asks for a rate Deepgram cannot synthesize.
DEFAULT_SAMPLE_RATE = 24000

#: mulaw/alaw are 8 kHz-only on Deepgram, as on the telephony leg itself.
_COMPANDED_RATE = 8000

#: Platform codec name → Deepgram ``encoding`` value. The platform's
#: normalized ``linear16`` and ``pcm`` both mean headerless 16-bit LE PCM,
#: which is exactly what ``encoding=linear16&container=none`` returns.
_CODEC_ENCODINGS: dict[str, str] = {
    "linear16": "linear16",
    "pcm": "linear16",
    "mulaw": "mulaw",
    "ulaw": "mulaw",
    "alaw": "alaw",
}

#: Voice used when an engine selects a model but no voice at all. This is
#: never a substitution for a WRONG voice — only for a missing one, and it is
#: logged, mirroring the Sarvam speaker-default rule.
DEFAULT_VOICE_FOR_MODEL: dict[str, str] = {
    "aura-2": "aura-2-thalia-en",
    "aura": "aura-asteria-en",
}

#: Documented range of the ``speed`` query parameter. Mirrored in
#: ``shared.providers.tts.delivery`` so canonical Delivery tuning clamps to it.
SPEED_RANGE: tuple[float, float] = (0.7, 1.5)


def is_native_sample_rate(rate: int | None) -> bool:
    return rate in NATIVE_SAMPLE_RATES


def encoding_for_codec(codec: str | None) -> str:
    """Deepgram ``encoding`` for a platform codec name (default linear16)."""
    return _CODEC_ENCODINGS.get((codec or "").strip().lower(), "linear16")


def wire_sample_rate(codec: str | None, sample_rate: int | None) -> int:
    """The ``sample_rate`` to request for a codec.

    Companded codecs are 8 kHz by definition; linear16 falls back to
    :data:`DEFAULT_SAMPLE_RATE` when the consumer asks for a rate Deepgram
    does not synthesize natively (the consumer then resamples, exactly as it
    does for every other provider).
    """
    if encoding_for_codec(codec) in ("mulaw", "alaw"):
        return _COMPANDED_RATE
    return sample_rate if is_native_sample_rate(sample_rate) else DEFAULT_SAMPLE_RATE
