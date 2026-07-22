"""Streaming (WebSocket) TTS provider interface.

One provider instance == one persistent provider connection scoped to a single
call/session. Never share an instance across unrelated calls.

The lifecycle contract consumed by the voice runtime and the preview proxy:

    provider = SarvamWebSocketTTSProvider(settings)
    await provider.connect()                     # open WS + send provider config
    await provider.synthesize_stream(text, generation_id="g1")
    await provider.flush("g1")                   # force buffered text to render
    ... consume provider.events ...              # audio / final / error events
    await provider.cancel("g1")                  # barge-in: discard generation
    await provider.close()                       # graceful teardown

Generations map to one bot reply each. Audio arriving for a cancelled or
unknown generation is dropped by the provider (late-audio rejection) — the
consumer never sees stale chunks. All errors are emitted as sanitized
``ProviderError`` values on the event queue; API keys never appear in
messages or logs.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from shared.providers.base import ProviderError

logger = logging.getLogger("providers.tts.streaming")

# Error categories that are safe to retry / fall back on. Auth and
# configuration problems must surface to the operator instead.
TRANSIENT_ERROR_CATEGORIES = frozenset({"timeout", "rate_limit", "upstream"})


@dataclass
class TTSStreamSettings:
    """Normalized settings translated per provider into wire parameters.

    ``params`` holds provider-specific fields already validated against the
    provider model's parameter schema; adapters send only what the selected
    model supports and log a sanitized warning for ignored optional fields.
    """

    provider: str
    model: str
    voice: str                      # provider wire voice id / speaker code
    language: str                   # platform locale code (e.g. "hi-IN")
    sample_rate: int = 16000
    codec: str = "linear16"         # linear16 | pcm | mulaw | alaw
    params: dict[str, Any] = field(default_factory=dict)
    api_key: str = ""               # resolved secret, held in memory only
    timeout_seconds: float = 15.0


@dataclass
class TTSStreamEvent:
    """Event emitted on the provider's queue."""

    kind: str                       # audio | final | error | disconnected
    generation_id: str | None = None
    audio: bytes = b""
    error: ProviderError | None = None


class StreamingTTSProvider(ABC):
    """Common interface for WebSocket TTS providers (task-spec §15).

    The interface is intentionally thin: providers accept *normalized*
    settings and translate them into their own wire formats — they are never
    forced to accept another provider's raw parameters.
    """

    name = "tts-stream"

    def __init__(self, settings: TTSStreamSettings) -> None:
        self._settings = settings
        self.events: asyncio.Queue[TTSStreamEvent] = asyncio.Queue()
        self._live_generations: set[str] = set()
        self._closed = False

    # ── lifecycle ────────────────────────────────────────────────────────
    @abstractmethod
    async def connect(self) -> None:
        """Open (or reuse) the provider connection and send configuration."""

    @abstractmethod
    async def configure(self, settings: TTSStreamSettings) -> None:
        """Apply new normalized settings (voice/language/params) to the connection."""

    @abstractmethod
    async def synthesize_stream(self, text: str, *, generation_id: str) -> None:
        """Queue text for synthesis under the given generation."""

    @abstractmethod
    async def flush(self, generation_id: str) -> None:
        """Force any buffered text of the generation to render immediately."""

    async def finish(self, generation_id: str) -> None:
        """Signal that no more text will arrive for this generation.

        Providers with server-side generation state (ElevenLabs contexts) use
        this to close it so the final event arrives promptly. Default: no-op.
        """

    @abstractmethod
    async def cancel(self, generation_id: str) -> None:
        """Abandon a generation: stop synthesis, drop queued/late audio."""

    @abstractmethod
    async def close(self) -> None:
        """Tear the connection down gracefully. Idempotent."""

    # ── shared helpers ───────────────────────────────────────────────────
    @property
    def settings(self) -> TTSStreamSettings:
        return self._settings

    def generation_alive(self, generation_id: str | None) -> bool:
        return generation_id is not None and generation_id in self._live_generations

    def _begin_generation(self, generation_id: str) -> None:
        self._live_generations.add(generation_id)

    def _end_generation(self, generation_id: str | None) -> None:
        if generation_id:
            self._live_generations.discard(generation_id)

    async def _emit(self, event: TTSStreamEvent) -> None:
        await self.events.put(event)

    async def _emit_error(self, category: str, message: str,
                          generation_id: str | None = None) -> None:
        await self._emit(TTSStreamEvent(
            kind="error", generation_id=generation_id,
            error=ProviderError(self.name, category, message),
        ))

    @staticmethod
    def categorize_close(code: int | None, reason: str = "") -> str:
        """Map a WS close/handshake status to a ProviderError category."""
        reason = (reason or "").lower()
        if code in (401, 403, 4401, 4403) or "auth" in reason or "key" in reason:
            return "auth"
        if code == 429 or code == 1008 or "rate" in reason or "quota" in reason:
            return "rate_limit"
        if "timeout" in reason:
            return "timeout"
        return "upstream"
