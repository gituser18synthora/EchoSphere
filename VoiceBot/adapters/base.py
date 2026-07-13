"""Base interfaces and response types for LLM, STT, and TTS adapters."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Optional


class AdapterException(Exception):
    """Raised when a provider adapter fails (auth, rate limit, timeout, unavailable)."""

    def __init__(
        self,
        message: str,
        retry_after: Optional[float] = None,
        *args: object,
    ) -> None:
        super().__init__(message, *args)
        self.message = message
        self.retry_after = retry_after


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    model_used: str


@dataclass
class STTResponse:
    text: str
    detected_language: str
    confidence: float
    is_final: bool


@dataclass
class TTSResponse:
    audio_bytes: bytes  # PCM audio, 8kHz 16-bit mono (FreeSWITCH format)
    sample_rate: int
    duration_ms: float


class LLMAdapter(ABC):
    @abstractmethod
    async def generate(
        self,
        messages: list[dict],  # [{"role": "system/user/assistant", "content": "..."}]
        system_prompt: str,
        max_tokens: int = 300,
        temperature: float = 0.7,
    ) -> LLMResponse:
        pass


class STTAdapter(ABC):
    @abstractmethod
    async def transcribe(
        self,
        audio_bytes: bytes,  # Raw PCM from FreeSWITCH
        language: str = "en",
        auto_detect: bool = False,
    ) -> STTResponse:
        pass


class TTSAdapter(ABC):
    @abstractmethod
    async def synthesize(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> TTSResponse:
        pass

    @abstractmethod
    async def synthesize_stream(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> AsyncIterator[bytes]:  # Stream PCM chunks for low-latency playback
        pass
