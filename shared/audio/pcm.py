"""Shared PCM helpers for the voice runtime.

Merged from the legacy VoiceBot ``adapters/audio_utils.py`` and
``audio/pcm_utils.py``. All functions operate on 16-bit signed little-endian
mono PCM unless noted otherwise. Resampling uses numpy linear interpolation
(replacing the legacy pure-python per-sample loop); no audio-device
(sounddevice) code is included.
"""

from __future__ import annotations

import io
import struct
import wave

import numpy as np

DEFAULT_FADE_MS = 10

_WAVE_FORMAT_PCM = 1
_WAVE_FORMAT_EXTENSIBLE = 0xFFFE


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap raw 16-bit PCM in a WAV (RIFF) container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    """Parse a RIFF/WAVE container and return ``(pcm_16bit_mono, sample_rate)``.

    Walks fmt/data chunks instead of assuming a fixed 44-byte header.
    Multi-channel audio is reduced to mono by taking the first channel.
    Returns ``(b"", 0)`` when the input is not a parseable 16-bit PCM WAV.
    """
    if len(wav_bytes) < 12 or wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        return b"", 0

    pos = 12
    sample_rate = 0
    channels = 1
    bits_per_sample = 16
    audio_format = _WAVE_FORMAT_PCM
    pcm = b""

    while pos + 8 <= len(wav_bytes):
        chunk_id = wav_bytes[pos : pos + 4]
        chunk_size = struct.unpack_from("<I", wav_bytes, pos + 4)[0]
        pos += 8
        chunk_data = wav_bytes[pos : pos + chunk_size]
        pos += chunk_size + (chunk_size % 2)  # chunks are word-aligned

        if chunk_id == b"fmt " and len(chunk_data) >= 16:
            audio_format = struct.unpack_from("<H", chunk_data, 0)[0]
            channels = struct.unpack_from("<H", chunk_data, 2)[0] or 1
            sample_rate = struct.unpack_from("<I", chunk_data, 4)[0]
            bits_per_sample = struct.unpack_from("<H", chunk_data, 14)[0] or 16
        elif chunk_id == b"data":
            pcm = chunk_data

    if not pcm or sample_rate <= 0:
        return b"", sample_rate
    if audio_format not in (_WAVE_FORMAT_PCM, _WAVE_FORMAT_EXTENSIBLE) or bits_per_sample != 16:
        return b"", sample_rate

    if len(pcm) % 2 == 1:
        pcm = pcm[:-1]
    if channels > 1:
        samples = np.frombuffer(pcm, dtype="<i2")
        frames = samples.size // channels
        if frames == 0:
            return b"", sample_rate
        mono = samples[: frames * channels].reshape(frames, channels)[:, 0]
        pcm = np.ascontiguousarray(mono).tobytes()
    return pcm, sample_rate


def resample_pcm(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample 16-bit mono PCM between arbitrary rates (numpy linear interpolation)."""
    if from_rate <= 0 or to_rate <= 0:
        raise ValueError("sample rates must be positive")
    if not pcm or from_rate == to_rate:
        return pcm
    if len(pcm) % 2 == 1:
        pcm = pcm[:-1]
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    n = samples.size
    if n == 0:
        return b""
    out_n = max(1, int(n * to_rate / from_rate))
    positions = np.arange(out_n, dtype=np.float64) * (from_rate / to_rate)
    resampled = np.interp(positions, np.arange(n, dtype=np.float64), samples)
    return np.clip(np.rint(resampled), -32768, 32767).astype("<i2").tobytes()


def _apply_fades(
    pcm: bytes, sample_rate: int, fade_ms: int, *, fade_in: bool, fade_out: bool
) -> bytes:
    if not pcm or len(pcm) < 4:
        return pcm
    samples = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype="<i2").astype(np.float32)
    fade_samples = min(max(1, int(sample_rate * fade_ms / 1000)), samples.size // 2)
    if fade_samples < 1:
        return pcm
    if fade_in:
        samples[:fade_samples] *= np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
    if fade_out:
        samples[-fade_samples:] *= np.linspace(1.0, 0.0, fade_samples, dtype=np.float32)
    return np.clip(samples, -32768, 32767).astype("<i2").tobytes()


def apply_fade_in(pcm: bytes, *, sample_rate: int = 16000, fade_ms: int = DEFAULT_FADE_MS) -> bytes:
    """Apply a short linear fade-in to reduce clicks at a clip start."""
    return _apply_fades(pcm, sample_rate, fade_ms, fade_in=True, fade_out=False)


def apply_fade_out(pcm: bytes, *, sample_rate: int = 16000, fade_ms: int = DEFAULT_FADE_MS) -> bytes:
    """Apply a short linear fade-out to reduce clicks at a clip end."""
    return _apply_fades(pcm, sample_rate, fade_ms, fade_in=False, fade_out=True)


def join_pcm_chunks(
    chunks: list[bytes], *, sample_rate: int = 16000, crossfade_ms: int = DEFAULT_FADE_MS
) -> bytes:
    """Join PCM segments with per-chunk fades at interior boundaries.

    The first chunk keeps its attack and the last chunk keeps its natural end;
    every interior boundary gets a fade-out/fade-in pair so the joined stream
    is click-free. Odd trailing bytes are dropped per chunk.
    """
    valid = [chunk for chunk in (chunks or []) if chunk and len(chunk) >= 2]
    if not valid:
        return b""
    prepared: list[bytes] = []
    last = len(valid) - 1
    for i, raw in enumerate(valid):
        if len(raw) % 2 == 1:
            raw = raw[:-1]
        prepared.append(
            _apply_fades(
                raw,
                sample_rate,
                crossfade_ms,
                fade_in=i > 0,
                fade_out=i < last,
            )
        )
    return b"".join(prepared)
