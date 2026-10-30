"""Deepgram streaming TTS over the ``/v1/speak`` WebSocket (Aura / Aura-2).

Wire protocol (developers.deepgram.com/reference/text-to-speech-api/
speak-streaming, verified 2026-09-21):

- URL   wss://api.deepgram.com/v1/speak?model=<voice>&encoding=linear16
        &sample_rate=<rate>[&speed=<0.7-1.5>]
        (regional hosts: api.in / api.eu / api.au — same key, same API)
- Auth  handshake header ``Authorization: Token <key>``
- C→S   {"type":"Speak","text":"…"} | {"type":"Flush"} | {"type":"Clear"}
        | {"type":"Close"}
- S→C   BINARY frames = raw audio in the requested encoding
        {"type":"Metadata", "request_id":…, "model_name":…}
        {"type":"Flushed", "sequence_id":n}   — that Speak/Flush is complete
        {"type":"Cleared", "sequence_id":n}   — buffered audio discarded
        {"type":"Warning", "description":…, "code":…}

Mapping onto the platform's streaming contract
----------------------------------------------
Deepgram has no per-generation context ids: the socket is a single ordered
pipeline, and ``Flushed`` arrives once per ``Flush`` in dispatch order. So
generations are tracked in a FIFO — binary audio is attributed to the oldest
live generation and ``Flushed`` completes it. That is correct whether the
consumer serializes sentences (preview, pause mode) or pipelines them.

``Clear`` is a real server-side cancel, which is what barge-in needs: unlike
Sarvam (where cancelling means dropping the connection) the socket survives,
so the next reply pays no reconnect. Audio already in flight for a cleared
generation is dropped locally as well — late-audio rejection is not optional
on a protocol with no ids on the audio frames.

The language never appears on the wire; it is part of the voice id, and an
unspeakable locale is refused before connecting (see
``shared.providers.languages``). The voice, encoding and sample rate live in
the URL, so changing any of them reconnects — the runtime keeps one provider
instance per (provider, model, voice) to avoid mid-call churn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from urllib.parse import urlencode

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.protocol import State

from shared.providers.base import ProviderError
from shared.providers.deepgram_common import auth_headers, ws_base_url
from shared.providers.languages import (
    DEEPGRAM_STREAMING_MODELS,
    deepgram_supports_language,
)
from shared.providers.tts.deepgram import (
    check_voice_language,
    resolve_wire_model,
    unsupported_language_error,
)
from shared.providers.tts.deepgram_voices import (
    encoding_for_codec,
    wire_sample_rate,
)
from shared.providers.tts.delivery import provider_speed
from shared.providers.tts.streaming import (
    StreamingTTSProvider,
    TTSStreamEvent,
    TTSStreamSettings,
)

logger = logging.getLogger("providers.tts.deepgram_ws")

_SPEAK_PATH = "/v1/speak"
# Connect attempts happen inline on the reply path: two short attempts, then
# error/fallback — never tens of seconds of handshake retries while the
# caller waits in silence. Same budget as the Sarvam/ElevenLabs adapters.
_MAX_CONNECT_ATTEMPTS = 2
_CONNECT_TIMEOUT_S = 3.0
_CLOSE_HANDSHAKE_TIMEOUT = 2.0
# Deepgram documents no application-level keepalive for /v1/speak, so the
# connection is kept alive by the websockets library's protocol-level pings
# rather than by an invented JSON message the server would reject.
_PING_INTERVAL_S = 15
_PING_TIMEOUT_S = 15
_DEFAULT_MODEL = "aura-2"


class DeepgramWebSocketTTSProvider(StreamingTTSProvider):
    name = "deepgram-ws"

    def __init__(self, settings: TTSStreamSettings) -> None:
        super().__init__(settings)
        self._ws = None
        self._receive_task: asyncio.Task | None = None
        # Generations dispatched and not yet completed, in dispatch order.
        # The protocol carries no ids, so order IS the correspondence.
        self._pending: deque[str] = deque()
        # Bytes delivered per live generation: a Flushed carrying no audio at
        # all is a failure (an exhausted account, a rejected voice), not a
        # successful empty reply that would render as silence.
        self._generation_bytes: dict[str, int] = {}
        self._send_lock = asyncio.Lock()
        # Serializes concurrent connect() calls (a background warm-up racing
        # the next dispatch) so only one socket is opened.
        self._connect_lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────
    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("provider is closed")
        self._guard_settings()
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
                            additional_headers=auth_headers(self._settings.api_key),
                            ping_interval=_PING_INTERVAL_S,
                            ping_timeout=_PING_TIMEOUT_S,
                            max_size=16 * 1024 * 1024,
                        ),
                        timeout=min(_CONNECT_TIMEOUT_S, self._settings.timeout_seconds),
                    )
                    break
                except InvalidStatus as exc:  # HTTP handshake rejection
                    status = exc.response.status_code
                    category = self.categorize_close(status)
                    await self._emit_error(
                        category, f"Deepgram TTS handshake rejected ({status})"
                    )
                    if category == "auth":
                        raise ProviderError(
                            self.name, "auth", "Deepgram rejected the API key"
                        ) from exc
                    if category == "invalid_input":
                        # A bad model/encoding/sample_rate cannot be retried
                        # into working, and must not burn the fallback.
                        raise ProviderError(
                            self.name, "invalid_input",
                            f"Deepgram rejected the stream configuration "
                            f"(HTTP {status}) — check the voice, encoding and "
                            "sample rate for this model.",
                        ) from exc
                    last_category = category
                except (TimeoutError, OSError, ConnectionClosed):
                    last_category = "timeout"
                    await asyncio.sleep(0.2 * (attempt + 1))
            else:
                await self._emit_error(last_category, "Could not connect to Deepgram TTS")
                raise ProviderError(
                    self.name, last_category, "Could not connect to Deepgram TTS"
                )

            if self._receive_task is None or self._receive_task.done():
                self._receive_task = asyncio.create_task(self._receive_loop())

    async def configure(self, settings: TTSStreamSettings) -> None:
        """Apply new settings. Everything Deepgram negotiates lives in the
        URL (voice, encoding, sample rate, speed), so a change that alters
        the URL — or the credential — reconnects; anything else is a no-op."""
        old_url = self._build_url()
        old_key = self._settings.api_key
        self._settings = settings
        if self._build_url() != old_url or settings.api_key != old_key:
            if self._ws is not None:
                await self._teardown_socket()

    async def synthesize_stream(self, text: str, *, generation_id: str) -> None:
        if not text:
            return
        await self.connect()
        self._begin_generation(generation_id)
        if generation_id not in self._pending:
            self._pending.append(generation_id)
        await self._send({"type": "Speak", "text": text})

    async def flush(self, generation_id: str) -> None:
        """Force buffered text to render. Deepgram answers each Flush with a
        ``Flushed``, which is what completes the generation."""
        if self._ws is not None and self._ws.state is State.OPEN:
            await self._send({"type": "Flush"})

    async def cancel(self, generation_id: str) -> None:
        """Barge-in: ``Clear`` stops server-side synthesis and drops what is
        already buffered, and the socket stays up for the next reply."""
        was_live = self.generation_alive(generation_id)
        self._end_generation(generation_id)
        self._generation_bytes.pop(generation_id, None)
        try:
            self._pending.remove(generation_id)
        except ValueError:
            pass
        if was_live and self._ws is not None and self._ws.state is State.OPEN:
            try:
                await self._send({"type": "Clear"})
            except (ConnectionError, ConnectionClosed):
                pass

    async def close(self) -> None:
        self._closed = True
        self._live_generations.clear()
        self._pending.clear()
        self._generation_bytes.clear()
        ws = self._ws
        if ws is not None and ws.state is State.OPEN:
            # Two-step close: ask the server to finish, then wait briefly so
            # we do not race the closing handshake.
            try:
                async with self._send_lock:
                    await ws.send(json.dumps({"type": "Close"}))
                await asyncio.wait_for(ws.wait_closed(), timeout=_CLOSE_HANDSHAKE_TIMEOUT)
            except (TimeoutError, ConnectionClosed, OSError):
                pass
        await self._teardown_socket()

    # ── internals ────────────────────────────────────────────────────────
    def _guard_settings(self) -> None:
        """Refuse configurations Deepgram cannot serve, before connecting."""
        model = (self._settings.model or "").strip() or _DEFAULT_MODEL
        language = self._settings.language or ""
        if language and deepgram_supports_language(model, language) is False:
            error = unsupported_language_error(self.name, model, language)
            # Emitted as well as raised: a consumer draining the queue (the
            # preview collector) must see why the stream never produced audio.
            self._emit_error_soon(error.category, str(error))
            raise error
        if model not in DEEPGRAM_STREAMING_MODELS:
            message = (
                f"Deepgram model '{model}' is not available on the realtime "
                "text-to-speech WebSocket."
            )
            self._emit_error_soon("invalid_input", message)
            raise ProviderError(self.name, "invalid_input", message)
        wire_model = resolve_wire_model(self.name, model, self._settings.voice)
        check_voice_language(self.name, wire_model, language)

    def _emit_error_soon(self, category: str, message: str) -> None:
        """Queue an error without awaiting — ``_guard_settings`` runs on the
        synchronous part of connect() and must not block on a full queue."""
        try:
            self.events.put_nowait(TTSStreamEvent(
                kind="error", error=ProviderError(self.name, category, message),
            ))
        except asyncio.QueueFull:  # pragma: no cover — the raise still lands
            logger.warning("deepgram-tts: event queue full, dropping error event")

    def _build_url(self) -> str:
        s = self._settings
        params = dict(s.params or {})
        model = (s.model or "").strip() or _DEFAULT_MODEL
        query: dict[str, str] = {
            # Deepgram's ``model`` IS the voice id (aura-2-thalia-en).
            "model": resolve_wire_model(self.name, model, s.voice),
            "encoding": encoding_for_codec(s.codec),
            "sample_rate": str(wire_sample_rate(s.codec, s.sample_rate)),
        }
        # Canonical Delivery speed arrives in params (resolve_engine_params);
        # 1.0 is Deepgram's default, so it is omitted rather than sent.
        speed = params.get("speed")
        if speed is not None:
            wire_speed = provider_speed("deepgram", model, speed)
            if wire_speed != 1.0:
                query["speed"] = f"{wire_speed:g}"
        if params.get("mip_opt_out"):
            query["mip_opt_out"] = "true"
        base = ws_base_url(params.get("region"))
        return f"{base}{_SPEAK_PATH}?{urlencode(query)}"

    async def _send(self, message: dict) -> None:
        if self._ws is None or self._ws.state is not State.OPEN:
            raise ConnectionError("Deepgram TTS websocket is not connected")
        async with self._send_lock:
            await self._ws.send(json.dumps(message))

    def _current_generation(self) -> str | None:
        """Oldest generation still awaiting audio/Flushed, if any."""
        while self._pending and not self.generation_alive(self._pending[0]):
            self._pending.popleft()
        return self._pending[0] if self._pending else None

    async def _receive_loop(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    await self._handle_audio(bytes(raw))
                    continue
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("deepgram-tts: discarding non-JSON frame")
                    continue
                await self._handle_message(message)
        except ConnectionClosed as exc:
            if not self._closed and self._live_generations:
                code = exc.rcvd.code if exc.rcvd else None
                reason = exc.rcvd.reason if exc.rcvd else ""
                category = self.categorize_close(code, reason)
                for generation in list(self._pending):
                    await self._emit_error(
                        category, "Deepgram TTS connection closed mid-generation",
                        generation_id=generation,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("deepgram-tts: receive loop failed")
        finally:
            if self._receive_task is asyncio.current_task():
                # connect() only spawns a new loop when the old one is gone;
                # clearing here closes the race with a reconnect landing
                # while this task is still finishing.
                self._receive_task = None
            if not self._closed:
                await self._emit(TTSStreamEvent(kind="disconnected"))

    async def _handle_audio(self, audio: bytes) -> None:
        generation = self._current_generation()
        # Late-audio rejection: the protocol puts no id on audio frames, so
        # anything arriving for a cancelled/unknown generation is dropped.
        if not audio or not self.generation_alive(generation):
            return
        self._generation_bytes[generation] = (
            self._generation_bytes.get(generation, 0) + len(audio)
        )
        await self._emit(TTSStreamEvent(
            kind="audio", generation_id=generation, audio=audio,
        ))

    async def _handle_message(self, message: dict) -> None:
        kind = str(message.get("type") or "")
        if kind == "Flushed":
            await self._complete_generation()
            return
        if kind == "Cleared":
            # Our own cancel already dropped the generation locally; nothing
            # further to report, and no final for a cancelled reply.
            return
        if kind == "Metadata":
            logger.debug(
                "deepgram-tts: stream metadata model=%s request=%s",
                message.get("model_name"), message.get("request_id"),
            )
            return
        if kind == "Warning":
            # Advisory (e.g. a truncated input); synthesis continues.
            logger.warning(
                "deepgram-tts: provider warning %s: %s",
                str(message.get("code") or "")[:60],
                str(message.get("description") or "")[:200],
            )
            return
        if kind in ("Error", "Fatal") or message.get("err_code") or message.get("error"):
            text = str(
                message.get("description")
                or message.get("err_msg")
                or message.get("message")
                or message.get("error")
                or "Deepgram TTS error"
            )[:200]
            code = message.get("code") or message.get("err_code")
            category = self.categorize_close(
                code if isinstance(code, int) else None, text
            )
            generation = self._current_generation()
            await self._emit_error(category, text, generation_id=generation)
            if generation is not None:
                self._end_generation(generation)
                self._generation_bytes.pop(generation, None)
                if self._pending and self._pending[0] == generation:
                    self._pending.popleft()

    async def _complete_generation(self) -> None:
        generation = self._current_generation()
        if generation is None:
            return
        self._pending.popleft()
        delivered = self._generation_bytes.pop(generation, 0)
        self._end_generation(generation)
        if not delivered:
            # Deepgram flushed without synthesizing anything. Reporting a
            # "successful" final here would render as silence on the call.
            await self._emit_error(
                "upstream",
                "Deepgram completed the generation without returning any "
                "audio — check the voice/model and the account's quota.",
                generation_id=generation,
            )
            return
        await self._emit(TTSStreamEvent(kind="final", generation_id=generation))

    async def _teardown_socket(self) -> None:
        task = self._receive_task
        if task is not None and not task.done():
            task.cancel()
        self._receive_task = None
        self._pending.clear()
        self._generation_bytes.clear()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 — best-effort close
                pass
