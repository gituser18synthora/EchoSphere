"""ElevenLabs streaming TTS over the multi-context WebSocket.

Wire protocol (elevenlabs.io/docs — multi-stream-input):
- URL   wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/multi-stream-input
        ?model_id=...&output_format=...&auto_mode=...&inactivity_timeout=...
- Auth  handshake header ``xi-api-key``
- C→S   context init {"text":" ","voice_settings":{...},"generation_config":
        {"chunk_length_schedule":[...]},"context_id":...}
        | text {"text":"...","context_id":...}
        | flush {"context_id":...,"flush":true}
        | cancel {"context_id":...,"close_context":true}
        | close {"close_socket":true}
        | keepalive {"text":""} (context-less) or {"text":"","context_id":...}
- S→C   {"audio":"<b64>","contextId":...} / {"isFinal":true,"contextId":...}
        Servers have emitted both camelCase and snake_case keys — both parsed.

One socket per call; each bot reply is an ElevenLabs *context* (== our
generation). ``close_context`` on barge-in makes the server stop synthesis,
and chunks for closed contexts are dropped locally as well. The voice id is
part of the URL, so switching voices requires a reconnect — the runtime keeps
one provider instance per (provider, voice) to avoid mid-call churn.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.protocol import State

from shared.providers.base import ProviderError
from shared.providers.languages import to_provider_language
from shared.providers.tts.streaming import (
    StreamingTTSProvider,
    TTSStreamEvent,
    TTSStreamSettings,
)

logger = logging.getLogger("providers.tts.elevenlabs_ws")

# Overridable for ElevenLabs regional hosts (api.in.residency.elevenlabs.io …)
# and mocked end-to-end verification.
_WS_BASE = os.environ.get("ELEVENLABS_WS_BASE", "wss://api.elevenlabs.io")
_KEEPALIVE_SECONDS = 10
# Connect attempts happen inline on the reply path: two short attempts, then
# error/fallback — never tens of seconds of handshake retries while the
# caller waits in silence.
_MAX_CONNECT_ATTEMPTS = 2
_CONNECT_TIMEOUT_S = 3.0
_CLOSE_HANDSHAKE_TIMEOUT = 2.0

# Models that accept the language_code enforcement query parameter.
_LANGUAGE_ENFORCING_MODELS = {"eleven_flash_v2_5", "eleven_turbo_v2_5"}

# Models the ElevenLabs realtime WebSocket does not accept (official docs:
# "That endpoint does not support the eleven_v3 model"). Configurations with
# these models must run the REST adapter (shared/providers/tts/elevenlabs.py);
# rejecting here turns a misrouted config into a clear error instead of a
# cryptic server-side close.
_WS_UNSUPPORTED_MODELS = {"eleven_v3"}

_VOICE_SETTING_KEYS = ("stability", "similarity_boost", "style",
                       "use_speaker_boost", "speed")


def _output_format(codec: str, sample_rate: int) -> str:
    if codec in ("mulaw", "ulaw"):
        return "ulaw_8000"
    if codec == "alaw":
        return "alaw_8000"
    return f"pcm_{sample_rate}"


class ElevenLabsWebSocketTTSProvider(StreamingTTSProvider):
    name = "elevenlabs-ws"

    def __init__(self, settings: TTSStreamSettings) -> None:
        super().__init__(settings)
        self._ws = None
        self._receive_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._initialized_contexts: set[str] = set()
        self._send_lock = asyncio.Lock()
        # Serializes concurrent connect() calls (a background warm-up racing
        # the next dispatch) so only one socket is opened.
        self._connect_lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────
    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("provider is closed")
        model = self._settings.model or ""
        if model in _WS_UNSUPPORTED_MODELS:
            message = (
                f"ElevenLabs model '{model}' is not supported on the realtime "
                "WebSocket — use a streaming model (e.g. eleven_flash_v2_5) "
                "for live synthesis"
            )
            await self._emit_error("invalid_input", message)
            raise ProviderError(self.name, "invalid_input", message)
        async with self._connect_lock:
            if self._ws is not None and self._ws.state is State.OPEN:
                return
            url = self._build_url()
            last_category = "timeout"
            for attempt in range(_MAX_CONNECT_ATTEMPTS):
                try:
                    self._ws = await asyncio.wait_for(
                        websocket_connect(
                            url,
                            additional_headers={"xi-api-key": self._settings.api_key},
                            max_size=16 * 1024 * 1024,
                        ),
                        timeout=min(_CONNECT_TIMEOUT_S, self._settings.timeout_seconds),
                    )
                    break
                except InvalidStatus as exc:
                    status = exc.response.status_code
                    category = self.categorize_close(status)
                    await self._emit_error(
                        category, f"ElevenLabs handshake rejected ({status})"
                    )
                    if category == "auth":
                        raise ProviderError(self.name, "auth",
                                            "ElevenLabs rejected the API key") from exc
                    last_category = category
                except (TimeoutError, OSError, ConnectionClosed):
                    last_category = "timeout"
                    await asyncio.sleep(0.2 * (attempt + 1))
            else:
                await self._emit_error(last_category, "Could not connect to ElevenLabs")
                raise ProviderError(self.name, last_category, "Could not connect to ElevenLabs")

            self._initialized_contexts.clear()
            if self._receive_task is None or self._receive_task.done():
                self._receive_task = asyncio.create_task(self._receive_loop())
            if self._keepalive_task is None or self._keepalive_task.done():
                self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def configure(self, settings: TTSStreamSettings) -> None:
        """Apply new settings. Reconnect only when the connection itself — the
        wire URL (voice/model/codec/rate, and language only on enforcing
        models) or the handshake credential — actually changes; anything else
        (e.g. a language flip on a non-enforcing model, per-context
        voice_settings) applies without dropping the socket."""
        old_url = self._build_url()
        old_key = self._settings.api_key
        self._settings = settings
        needs_reconnect = (
            self._build_url() != old_url or settings.api_key != old_key
        )
        if needs_reconnect and self._ws is not None:
            await self._teardown_socket()

    async def synthesize_stream(self, text: str, *, generation_id: str) -> None:
        if not text:
            return
        await self.connect()
        self._begin_generation(generation_id)
        if generation_id not in self._initialized_contexts:
            await self._send(self._context_init(generation_id))
            self._initialized_contexts.add(generation_id)
        # Trailing space per ElevenLabs input-stream conventions.
        payload = text if text.endswith(" ") else text + " "
        await self._send({"text": payload, "context_id": generation_id})

    async def flush(self, generation_id: str) -> None:
        if (self._ws is not None and self._ws.state is State.OPEN
                and generation_id in self._initialized_contexts):
            await self._send({"context_id": generation_id, "flush": True})

    async def finish(self, generation_id: str) -> None:
        """Close the server-side context so is_final arrives right after the
        last audio byte. The generation stays alive locally until is_final."""
        if (generation_id in self._initialized_contexts
                and self._ws is not None and self._ws.state is State.OPEN):
            try:
                await self._send({"context_id": generation_id, "close_context": True})
            except (ConnectionError, ConnectionClosed):
                pass

    async def cancel(self, generation_id: str) -> None:
        self._end_generation(generation_id)
        if generation_id in self._initialized_contexts:
            self._initialized_contexts.discard(generation_id)
            if self._ws is not None and self._ws.state is State.OPEN:
                try:
                    await self._send({"context_id": generation_id, "close_context": True})
                except (ConnectionError, ConnectionClosed):
                    pass

    async def close(self) -> None:
        self._closed = True
        self._live_generations.clear()
        self._initialized_contexts.clear()
        ws = self._ws
        if ws is not None and ws.state is State.OPEN:
            # Two-step close: ask the server to close, then wait briefly so we
            # don't race the closing handshake.
            try:
                async with self._send_lock:
                    await ws.send(json.dumps({"close_socket": True}))
                await asyncio.wait_for(ws.wait_closed(), timeout=_CLOSE_HANDSHAKE_TIMEOUT)
            except (TimeoutError, ConnectionClosed, OSError):
                pass
        await self._teardown_socket()

    # ── internals ────────────────────────────────────────────────────────
    def _build_url(self) -> str:
        s = self._settings
        params = dict(s.params or {})
        model = s.model or "eleven_flash_v2_5"
        url = (
            f"{_WS_BASE}/v1/text-to-speech/{s.voice}/multi-stream-input"
            f"?model_id={model}"
            f"&output_format={_output_format(s.codec, s.sample_rate)}"
            f"&auto_mode={str(bool(params.get('auto_mode', True))).lower()}"
            f"&inactivity_timeout={int(params.get('inactivity_timeout', 60))}"
        )
        normalization = params.get("apply_text_normalization")
        if normalization in ("auto", "on", "off"):
            url += f"&apply_text_normalization={normalization}"
        if params.get("sync_alignment"):
            url += "&sync_alignment=true"
        if s.language and model in _LANGUAGE_ENFORCING_MODELS:
            iso = to_provider_language("elevenlabs", s.language)
            if iso:
                url += f"&language_code={iso.split('-')[0]}"
        return url

    def _context_init(self, context_id: str) -> dict:
        params = self._settings.params or {}
        voice_settings = {
            key: params[key] for key in _VOICE_SETTING_KEYS if params.get(key) is not None
        }
        message: dict = {"text": " ", "context_id": context_id}
        if voice_settings:
            message["voice_settings"] = voice_settings
        schedule = params.get("chunk_length_schedule")
        if schedule:
            message["generation_config"] = {"chunk_length_schedule": list(schedule)}
        return message

    async def _send(self, message: dict) -> None:
        if self._ws is None or self._ws.state is not State.OPEN:
            raise ConnectionError("ElevenLabs websocket is not connected")
        async with self._send_lock:
            await self._ws.send(json.dumps(message))

    async def _receive_loop(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("elevenlabs: discarding non-JSON frame")
                    continue
                await self._handle_message(message)
        except ConnectionClosed as exc:
            if not self._closed and self._live_generations:
                code = exc.rcvd.code if exc.rcvd else None
                reason = exc.rcvd.reason if exc.rcvd else ""
                category = self.categorize_close(code, reason)
                for generation in list(self._live_generations):
                    await self._emit_error(
                        category, "ElevenLabs connection closed mid-generation",
                        generation_id=generation,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("elevenlabs: receive loop failed")
        finally:
            if not self._closed:
                await self._emit(TTSStreamEvent(kind="disconnected"))

    async def _handle_message(self, message: dict) -> None:
        # The server has emitted both key casings historically — accept both.
        context = message.get("contextId") or message.get("context_id")
        is_final = message.get("isFinal")
        if is_final is None:
            is_final = message.get("is_final")

        is_error = bool(message.get("error")) or (
            message.get("code") is not None and message.get("message")
        )
        if is_error:
            text = str(message.get("message") or message.get("error"))[:200]
            code = message.get("code")
            category = self.categorize_close(code if isinstance(code, int) else None, text)
            await self._emit_error(category, text, generation_id=context)
            return

        if is_final:
            if self.generation_alive(context):
                self._end_generation(context)
                self._initialized_contexts.discard(context)
                await self._emit(TTSStreamEvent(kind="final", generation_id=context))
            return

        audio_b64 = message.get("audio")
        if audio_b64:
            # Late-audio rejection: closed/cancelled contexts are dropped.
            if not self.generation_alive(context):
                return
            try:
                audio = base64.b64decode(audio_b64)
            except (binascii.Error, ValueError):
                logger.warning("elevenlabs: discarding invalid base64 audio chunk")
                return
            if audio:
                await self._emit(TTSStreamEvent(
                    kind="audio", generation_id=context, audio=audio,
                ))

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_SECONDS)
                if self._ws is None or self._ws.state is not State.OPEN:
                    continue
                # Context-less keepalive unless a context init has been sent —
                # a context's first message must carry voice_settings.
                live_initialized = [
                    g for g in self._live_generations if g in self._initialized_contexts
                ]
                message: dict = {"text": ""}
                if live_initialized:
                    message["context_id"] = live_initialized[0]
                async with self._send_lock:
                    await self._ws.send(json.dumps(message))
        except (asyncio.CancelledError, ConnectionClosed):
            pass

    async def _teardown_socket(self) -> None:
        for task in (self._receive_task, self._keepalive_task):
            if task is not None and not task.done():
                task.cancel()
        self._receive_task = None
        self._keepalive_task = None
        self._initialized_contexts.clear()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 — best-effort close
                pass
