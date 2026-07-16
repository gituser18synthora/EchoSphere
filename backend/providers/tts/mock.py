"""Deterministic TTS for tests: encodes the text into the 'audio' payload so
MockSTT can round-trip it, plus a sine-ish PCM tail so audio paths see bytes."""

import math
import struct

from backend.providers.base import ProviderConfig, TTSProvider, TTSResult

MOCK_AUDIO_PREFIX = b"MOCKPCM:"


class MockTTS(TTSProvider):
    name = "mock-tts"

    def __init__(self, config: ProviderConfig) -> None:
        self._rate = int(config.extra.get("sample_rate", 16000))

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None,
        speed: float = 1.0,
    ) -> TTSResult:
        header = MOCK_AUDIO_PREFIX + text.encode("utf-8")
        # 100 ms of quiet tone so downstream audio handling has real PCM to chew.
        samples = int(self._rate * 0.1)
        tone = b"".join(
            struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / self._rate)))
            for i in range(samples)
        )
        return TTSResult(audio=header + tone, sample_rate=self._rate, duration_ms=100.0)
