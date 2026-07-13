"""Tests for PCM join/fade helpers."""

import struct

from voicebot.audio.pcm_utils import join_pcm_chunks


def test_join_pcm_chunks_even_length():
    a = struct.pack("<4h", 1000, 2000, 3000, 4000)
    b = struct.pack("<4h", -1000, -2000, -3000, -4000)
    out = join_pcm_chunks([a, b])
    assert len(out) % 2 == 0
    assert len(out) > len(a)
