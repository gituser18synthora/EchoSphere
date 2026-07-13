"""Shared audio helpers: PCM 8kHz 16-bit mono (FreeSWITCH format) to/from WAV."""

import io
import struct

# FreeSWITCH standard
TARGET_SAMPLE_RATE = 8000
TARGET_SAMPLE_WIDTH = 2  # 16-bit
TARGET_CHANNELS = 1


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int = TARGET_SAMPLE_RATE) -> bytes:
    """Wrap raw PCM (16-bit mono) in a minimal WAV header."""
    n = len(pcm)
    # WAV: 44-byte header
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + n))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, TARGET_CHANNELS, sample_rate,
                         sample_rate * TARGET_SAMPLE_WIDTH * TARGET_CHANNELS,
                         TARGET_SAMPLE_WIDTH * TARGET_CHANNELS, 16))
    buf.write(b"data")
    buf.write(struct.pack("<I", n))
    buf.write(pcm)
    return buf.getvalue()


def resample_pcm_to_8k(pcm_16bit_mono: bytes, from_sample_rate: int) -> bytes:
    """Resample 16-bit mono PCM to 8000 Hz using linear interpolation."""
    if from_sample_rate == TARGET_SAMPLE_RATE:
        return pcm_16bit_mono
    n = len(pcm_16bit_mono) // 2
    samples = struct.unpack(f"<{n}h", pcm_16bit_mono)
    out_n = int(n * TARGET_SAMPLE_RATE / from_sample_rate)
    out_samples = []
    for i in range(out_n):
        pos = i * from_sample_rate / TARGET_SAMPLE_RATE
        idx = int(pos)
        frac = pos - idx
        if idx >= n - 1:
            s = samples[-1]
        else:
            s = int(samples[idx] * (1 - frac) + samples[idx + 1] * frac)
        out_samples.append(max(-32768, min(32767, s)))
    return struct.pack(f"<{len(out_samples)}h", *out_samples)
