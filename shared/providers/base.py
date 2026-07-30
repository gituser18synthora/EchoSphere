"""Typed provider interfaces and shared result types.

All providers are async; implementations wrapping sync SDKs MUST run the SDK
call in a bounded thread pool (never on the event loop). Adapters must be
safe for concurrent calls — no per-call mutation of shared client state.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

# Bounded executor shared by sync-SDK adapters so they can't exhaust threads.
_SDK_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="provider-sdk")


async def run_in_sdk_pool(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    if kwargs:
        import functools

        func = functools.partial(func, **kwargs)
    return await loop.run_in_executor(_SDK_EXECUTOR, func, *args)


class ProviderError(Exception):
    """Categorized provider failure (sanitized; never carries secrets)."""

    def __init__(self, provider: str, category: str, message: str) -> None:
        self.provider = provider
        self.category = category  # auth | timeout | rate_limit | invalid_input | upstream
        super().__init__(f"[{provider}:{category}] {message}")


class ProviderConfig(BaseModel):
    """Typed provider selection for a bot/tenant. Secrets are references only."""

    provider: str
    model: str = ""
    voice: str = ""
    language: str = "en"
    api_key_reference: str = ""
    timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    extra: dict[str, Any] = Field(default_factory=dict)


@dataclass
class STTResult:
    text: str
    language: str | None = None
    confidence: float | None = None
    is_final: bool = True
    duration_ms: float = 0.0


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None


@dataclass
class LLMStreamUsage:
    """Provider-reported token usage for one completed stream() call.

    Adapters set `last_stream_usage` when the provider reports usage on the
    streaming API; callers that need billing-grade numbers read it right
    after the stream ends (one generation at a time per provider instance —
    the realtime voice path never runs concurrent generations on one call).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    source: str = "provider"  # provider | estimated


@dataclass
class TTSResult:
    audio: bytes  # PCM 16-bit mono
    sample_rate: int = 16000
    duration_ms: float = 0.0


class STTProvider(ABC):
    name: str = "stt"

    @abstractmethod
    async def transcribe(self, audio: bytes, *, sample_rate: int = 16000,
                         language: str | None = None) -> STTResult:
        """Transcribe a complete utterance (16-bit mono PCM)."""

    async def health_check(self) -> dict:
        return {"ok": True, "provider": self.name}


class TTSProvider(ABC):
    name: str = "tts"

    @abstractmethod
    async def synthesize(self, text: str, *, voice: str | None = None,
                         language: str | None = None, speed: float = 1.0) -> TTSResult:
        """Synthesize one sentence/segment to PCM."""

    async def stream_synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None,
        speed: float = 1.0,
    ) -> AsyncIterator[bytes]:
        """Default streaming shim: yield the full segment once."""
        result = await self.synthesize(text, voice=voice, language=language, speed=speed)
        yield result.audio

    async def health_check(self) -> dict:
        return {"ok": True, "provider": self.name}


class LLMProvider(ABC):
    name: str = "llm"
    # Usage reported by the provider for the most recent completed stream();
    # None when the stream failed or the provider doesn't report usage.
    last_stream_usage: LLMStreamUsage | None = None

    @abstractmethod
    async def generate(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> LLMResult:
        """Non-streaming completion (used by routing / extraction)."""

    @abstractmethod
    def stream(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> AsyncIterator[str]:
        """Token stream for the realtime voice path."""

    async def health_check(self) -> dict:
        return {"ok": True, "provider": self.name}


class TelephonyProvider(ABC):
    name: str = "telephony"

    @abstractmethod
    async def accept_call(self, call_context: dict) -> None: ...

    @abstractmethod
    async def send_audio(self, audio: bytes) -> None: ...

    @abstractmethod
    async def transfer_call(self, destination: str) -> None: ...

    @abstractmethod
    async def hangup(self) -> None: ...

    async def health_check(self) -> dict:
        return {"ok": True, "provider": self.name}
