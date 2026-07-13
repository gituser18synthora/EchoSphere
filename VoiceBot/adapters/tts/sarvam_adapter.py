"""Sarvam AI Text-to-Speech adapter. Output: PCM 8kHz 16-bit mono."""
 
import base64
import asyncio
import logging
import os
import re
import time
import struct
from typing import Any, AsyncIterator
 
from sarvamai import AsyncSarvamAI
 
from adapters.base import AdapterException, TTSAdapter, TTSResponse
from config.settings import Settings
from voicebot.audio.tts_text import sanitize_for_tts
 
logger = logging.getLogger(__name__)
 
# ── Language detection ────────────────────────────────────────────────────────

# Unicode ranges for Indic scripts → Sarvam language code
_SCRIPT_LANG_MAP = [
    (r"[\u0900-\u097F]", "hi-IN"),   # Devanagari  → Hindi
    (r"[\u0980-\u09FF]", "bn-IN"),   # Bengali
    (r"[\u0A00-\u0A7F]", "pa-IN"),   # Gurmukhi    → Punjabi
    (r"[\u0A80-\u0AFF]", "gu-IN"),   # Gujarati
    (r"[\u0B00-\u0B7F]", "or-IN"),   # Odia
    (r"[\u0B80-\u0BFF]", "ta-IN"),   # Tamil
    (r"[\u0C00-\u0C7F]", "te-IN"),   # Telugu
    (r"[\u0C80-\u0CFF]", "kn-IN"),   # Kannada
    (r"[\u0D00-\u0D7F]", "ml-IN"),   # Malayalam
]

_COMPILED_SCRIPT_MAP = [(re.compile(p), lang) for p, lang in _SCRIPT_LANG_MAP]

# Sarvam supported language codes
_SUPPORTED_LANGS = {
    "hi-IN", "bn-IN", "kn-IN", "ml-IN", "mr-IN",
    "od-IN", "pa-IN", "raj-IN", "ta-IN", "te-IN",
    "en-IN", "gu-IN", "or-IN",
}


def _detect_language(text: str) -> str:
    """
    Detect language from script in text.
    - Checks Unicode script ranges for Indic languages.
    - Mixed text: picks the script with the most characters.
    - Falls back to 'en-IN' if no Indic script found.
    """
    counts: dict[str, int] = {}
    for pattern, lang in _COMPILED_SCRIPT_MAP:
        matches = pattern.findall(text)
        if matches:
            counts[lang] = len(matches)

    if not counts:
        return "en-IN"

    detected = max(counts, key=lambda k: counts[k])
    logger.debug("[SarvamTTS] Detected language: %s (counts=%s)", detected, counts)
    return detected


# ── Voice / speaker mapping ───────────────────────────────────────────────────

_ALLOWED_SPEAKERS = {"anushka", "abhilash", "manisha", "vidya", "arya", "karun", "hitesh", "rohan"}


def _map_voice_to_speaker(voice_id: str) -> str:
    """Map voice_id to Sarvam speaker. Default rohan."""
    v = (voice_id or "").strip().lower()
    return v if v in _ALLOWED_SPEAKERS else "rohan"


# ── PCM conversion ────────────────────────────────────────────────────────────

def _parse_wav_pcm(wav_bytes: bytes) -> tuple[bytes, int, int]:
    """
    Parse RIFF/WAVE and return (pcm_bytes, sample_rate, channels).
    Walks fmt/data chunks instead of assuming a fixed 44-byte header.
    """
    if len(wav_bytes) < 12 or wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        return b"", 0, 0

    pos = 12
    sample_rate = 8000
    channels = 1
    bits_per_sample = 16
    pcm = b""

    while pos + 8 <= len(wav_bytes):
        chunk_id = wav_bytes[pos : pos + 4]
        chunk_size = struct.unpack_from("<I", wav_bytes, pos + 4)[0]
        pos += 8
        chunk_data = wav_bytes[pos : pos + chunk_size]
        pos += chunk_size
        if chunk_size % 2 == 1:
            pos += 1

        if chunk_id == b"fmt " and len(chunk_data) >= 16:
            audio_format = struct.unpack_from("<H", chunk_data, 0)[0]
            channels = struct.unpack_from("<H", chunk_data, 2)[0] or 1
            sample_rate = struct.unpack_from("<I", chunk_data, 4)[0] or 8000
            bits_per_sample = struct.unpack_from("<H", chunk_data, 14)[0] or 16
            if audio_format != 1 or bits_per_sample != 16:
                logger.warning(
                    "[SarvamTTS] Unsupported WAV fmt: format=%s bits=%s",
                    audio_format,
                    bits_per_sample,
                )
        elif chunk_id == b"data":
            pcm = chunk_data

    if not pcm or channels < 1:
        return b"", sample_rate, channels

    if channels > 1:
        n_frames = len(pcm) // (2 * channels)
        if n_frames == 0:
            return b"", sample_rate, channels
        samples = struct.unpack(f"<{n_frames * channels}h", pcm[: n_frames * 2 * channels])
        mono = [samples[i] for i in range(0, len(samples), channels)]
        pcm = struct.pack(f"<{len(mono)}h", *mono)
        channels = 1

    return pcm, sample_rate, channels


def _resample_pcm_to_8k(pcm: bytes, sample_rate: int) -> bytes:
    if sample_rate == 8000:
        return pcm
    n = len(pcm) // 2
    if n == 0:
        return b""
    samples = struct.unpack(f"<{n}h", pcm)
    out_n = max(1, int(n * 8000 / sample_rate))
    out = []
    for i in range(out_n):
        pos = i * sample_rate / 8000
        idx = int(pos)
        frac = pos - idx
        if idx >= n - 1:
            s = samples[-1]
        else:
            s = int(samples[idx] * (1 - frac) + samples[idx + 1] * frac)
        out.append(max(-32768, min(32767, s)))
    return struct.pack(f"<{len(out)}h", *out)


def _wav_to_pcm_8k(wav_bytes: bytes) -> bytes:
    """Extract PCM from WAV and resample to 8kHz 16-bit mono if needed."""
    try:
        pcm, sample_rate, _channels = _parse_wav_pcm(wav_bytes)
        if not pcm or sample_rate <= 0:
            return b""
        if len(pcm) % 2 == 1:
            pcm = pcm[:-1]
        return _resample_pcm_to_8k(pcm, sample_rate)
    except Exception:
        logger.exception("[SarvamTTS] WAV parse failed")
        return b""


# ── Adapter ───────────────────────────────────────────────────────────────────

class SarvamTTSAdapter(TTSAdapter):
    """Sarvam AI Text-to-Speech (Bulbul). Returns PCM 8kHz 16-bit mono."""

    def __init__(self, **kwargs: Any) -> None:
        settings = Settings()
        key = (getattr(settings, "sarvam_api_key", None) or "").strip() or os.environ.get("SARVAM_API_KEY")
        self._client = AsyncSarvamAI(
            api_subscription_key=key,
            timeout=30.0,
        )
        self._timeout = getattr(settings, "stt_tts_max_latency", 2.0)

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> TTSResponse:
        start = time.perf_counter()
        pcm = await self._convert(text, voice_id, speed, pitch)
        latency_ms = (time.perf_counter() - start) * 1000
        duration_ms = len(pcm) / (8000 * 2) * 1000
        return TTSResponse(
            audio_bytes=pcm,
            sample_rate=8000,
            duration_ms=duration_ms,
        )

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> AsyncIterator[bytes]:
        """Yield PCM 8kHz 16-bit in one chunk (Sarvam has no streaming)."""
        pcm = await self._convert(text, voice_id, speed, pitch)
        if pcm:
            yield pcm

    async def _convert(
        self,
        text: str,
        voice_id: str,
        speed: float,
        pitch: float,
    ) -> bytes:
        text = sanitize_for_tts(text)
        if not text:
            return b""

        speaker = _map_voice_to_speaker(voice_id)
        pace = max(0.5, min(2.0, speed))

        # Auto-detect language from text content
        language_code = _detect_language(text)
        logger.info("[SarvamTTS] text=%r → language=%s speaker=%s", text[:60], language_code, speaker)

        # bulbul:v3 does NOT support pitch or loudness parameters
        tts_params = dict(
            text=text,
            model="bulbul:v3",
            target_language_code=language_code,
            speaker=speaker,
            pace=pace,
            speech_sample_rate=8000,
            output_audio_codec="wav",
        )

        try:
            response = await asyncio.wait_for(
                self._client.text_to_speech.convert(**tts_params),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as e:
            raise AdapterException("Sarvam TTS request timed out") from e
        except Exception as e:
            _raise_tts_error(e)

        audios = getattr(response, "audios", None) or []
        if not audios:
            return b""
        b64 = audios[0]
        wav_bytes = base64.b64decode(b64)
        pcm = _wav_to_pcm_8k(wav_bytes)
        if not pcm:
            logger.warning(
                "[SarvamTTS] Empty PCM — wav_bytes=%d bytes, speaker=%s, language=%s",
                len(wav_bytes), speaker, language_code,
            )
        return pcm


def _raise_tts_error(e: Exception) -> None:
    err = str(e).lower()
    if "401" in str(e) or "auth" in err or "unauthorized" in err:
        raise AdapterException(f"Sarvam TTS authentication failed: {e}") from e
    if "429" in str(e) or "rate" in err:
        raise AdapterException(
            f"Sarvam TTS rate limit exceeded: {e}",
            retry_after=60.0,
        ) from e
    if "timeout" in err:
        raise AdapterException(f"Sarvam TTS request timed out: {e}") from e
    raise AdapterException(f"Sarvam TTS error: {e}") from e