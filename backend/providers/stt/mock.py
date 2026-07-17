"""Deterministic STT for tests: decodes text embedded by MockTTS, or returns
a canned transcript from config.extra['transcripts'] round-robin."""

import itertools

from backend.providers.base import ProviderConfig, STTProvider, STTResult

MOCK_AUDIO_PREFIX = b"MOCKPCM:"


class MockSTT(STTProvider):
    name = "mock-stt"

    def __init__(self, config: ProviderConfig) -> None:
        transcripts = config.extra.get("transcripts") or ["hello"]
        self._cycle = itertools.cycle(transcripts)

    async def transcribe(
        self, audio: bytes, *, sample_rate: int = 16000, language: str | None = None
    ) -> STTResult:
        if audio.startswith(MOCK_AUDIO_PREFIX):
            payload = audio[len(MOCK_AUDIO_PREFIX):].split(b"\x00", 1)[0]
            return STTResult(text=payload.decode("utf-8", errors="ignore"), confidence=1.0)
        return STTResult(text=next(self._cycle), confidence=0.95)
