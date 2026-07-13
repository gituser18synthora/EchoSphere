"""PCM helpers: fade edges and gapless join for 16-bit mono."""

from __future__ import annotations

import numpy as np

DEFAULT_CROSSFADE_MS = 10


def _fade_edges_int16(
    samples: np.ndarray,
    fade_samples: int,
    *,
    fade_in: bool,
    fade_out: bool,
) -> np.ndarray:
    if samples.size == 0:
        return samples
    out = samples.astype(np.float32, copy=True)
    n = min(fade_samples, out.size // 2)
    if n < 1:
        return samples
    if fade_in:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        out[:n] *= ramp
    if fade_out:
        ramp = np.linspace(1.0, 0.0, n, dtype=np.float32)
        out[-n:] *= ramp
    return np.clip(out, -32768, 32767).astype(np.int16)


def prepare_pcm_for_playback(
    pcm: bytes,
    *,
    sample_rate: int = 8000,
    crossfade_ms: int = DEFAULT_CROSSFADE_MS,
    fade_in: bool = True,
    fade_out: bool = True,
) -> bytes:
    """Apply short fade-in/out to reduce clicks at clip boundaries."""
    if not pcm or len(pcm) < 4:
        return pcm
    arr = np.frombuffer(pcm, dtype=np.int16).copy()
    fade_samples = max(1, int(sample_rate * crossfade_ms / 1000))
    faded = _fade_edges_int16(
        arr, fade_samples, fade_in=fade_in, fade_out=fade_out,
    )
    return faded.tobytes()


def join_pcm_chunks(
    chunks: list[bytes],
    *,
    sample_rate: int = 8000,
    crossfade_ms: int = DEFAULT_CROSSFADE_MS,
) -> bytes:
    """
    Join PCM segments with per-chunk fades (first fades in, last fades out,
    middle segments fade both ends). Ensures each chunk has even byte length.
    """
    if not chunks:
        return b""
    prepared: list[bytes] = []
    valid = [c for c in chunks if c and len(c) >= 2]
    if not valid:
        return b""
    for i, raw in enumerate(valid):
        if len(raw) % 2 == 1:
            raw = raw[:-1]
        prepared.append(
            prepare_pcm_for_playback(
                raw,
                sample_rate=sample_rate,
                crossfade_ms=crossfade_ms,
                fade_in=i > 0,
                fade_out=i < len(valid) - 1,
            )
        )
    return b"".join(prepared)
