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
    """Categorized provider failure (sanitized; never carries secrets).

    ``status`` is the HTTP status the provider answered with, when there was
    one: it is what tells a spend limit (429) from an auth failure (401/403)
    or an upstream incident (5xx) in the recorded events.
    """

    def __init__(
        self, provider: str, category: str, message: str, *, status: int | None = None,
    ) -> None:
        self.provider = provider
        self.category = category  # auth | timeout | rate_limit | invalid_input | upstream
        self.message = message
        self.status = status
        super().__init__(f"[{provider}:{category}] {message}")


def error_fields(exc: BaseException | None, prefix: str = "error") -> dict:
    """Event fields describing a provider failure: its type and, for a
    :class:`ProviderError`, the category, HTTP status and (sanitized)
    message. ``{}`` for no error."""
    if exc is None:
        return {}
    fields: dict = {prefix: type(exc).__name__}
    if isinstance(exc, ProviderError):
        fields[f"{prefix}_category"] = exc.category
        fields[f"{prefix}_status"] = exc.status
        fields[f"{prefix}_message"] = exc.message[:200]
    else:
        fields[f"{prefix}_message"] = str(exc)[:200]
    return fields


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
    duration_ms: float = 0.0  # request latency, NOT audio duration
    # Optional quality metadata (provider-dependent) consumed by the voice
    # runtime's transcript gate; None whenever the provider doesn't report it.
    language_probability: float | None = None
    no_speech_prob: float | None = None


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None
    # Provider report, when available: prompt tokens served from the prompt
    # cache (included in input_tokens), the service tier the request actually
    # ran on, and the resolved model id.
    cached_tokens: int = 0
    service_tier: str | None = None
    model: str | None = None
    # Reasoning tokens (already included in output_tokens) — observability.
    reasoning_tokens: int = 0


@dataclass
class LLMStreamUsage:
    """Provider-reported token usage for one completed stream() call.

    Adapters set `last_stream_usage` when the provider reports usage on the
    streaming API; callers that need billing-grade numbers read it right
    after the stream ends (one generation at a time per provider instance —
    the realtime voice path never runs concurrent generations on one call).

    Normalized convention (billing depends on it):
    - ``input_tokens`` is the GROSS prompt count INCLUDING ``cached_tokens``
      (OpenAI prompt_tokens and Gemini prompt_token_count already are;
      Anthropic reports cache reads separately, so its adapter adds them in).
    - ``cached_tokens`` is the cache-hit subset, billed at the cached rate;
      pricing nets it out of the full-rate input component.
    - ``reasoning_tokens`` are informational: providers include them in
      ``output_tokens``, so they are never billed separately.
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
