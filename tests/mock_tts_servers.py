"""Scriptable mock WebSocket servers speaking the Sarvam and ElevenLabs TTS protocols.

Used by provider/router tests. Each server binds 127.0.0.1:0 and records every
message it receives for assertions. `behavior` selects failure injection:

  normal            happy path (chunks on flush + completion/final event)
  auth_fail         reject the WS handshake with HTTP 401
  rate_limit        reject the WS handshake with HTTP 429
  invalid_json      emit a non-JSON frame before the audio
  invalid_b64       emit an audio message whose payload is not base64
  error_message     emit a protocol error message instead of audio on flush
  invalid_config    (Sarvam) emit the API's 422 config-rejection error instead
                    of audio — a configuration error that must never fall back
  silent            accept everything, never emit audio
  drop_conn         close the connection right after the first text message
  late_after_close  (ElevenLabs) keep emitting audio for a context after the
                    client sent close_context — clients must reject it
  snake_case        (ElevenLabs) reply with snake_case keys (is_final, ...)
"""

from __future__ import annotations

import asyncio
import base64
import http
import json
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import serve

PCM_CHUNK = (b"\x01\x02" * 160)  # 20ms of fake 16-bit PCM @ 16kHz
API_KEY = "test-provider-key"


class _BaseMockServer:
    def __init__(self, *, behavior: str = "normal", chunks: int = 3,
                 first_chunk_delay: float = 0.0, api_key: str = API_KEY):
        self.behavior = behavior
        self.chunks = chunks
        self.first_chunk_delay = first_chunk_delay
        self.api_key = api_key
        self.received: list[dict] = []
        self.connections = 0
        self.raw_frames: list[str] = []
        self._server = None
        self.port: int | None = None

    auth_header = "api-subscription-key"

    def _process_request(self, connection, request):
        if self.behavior == "auth_fail" or request.headers.get(self.auth_header) != self.api_key:
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "unauthorized")
        if self.behavior == "rate_limit":
            return connection.respond(http.HTTPStatus.TOO_MANY_REQUESTS, "rate limited")
        return None

    async def __aenter__(self):
        self._server = await serve(
            self._handler, "127.0.0.1", 0, process_request=self._process_request
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def _handler(self, websocket):  # pragma: no cover - overridden
        raise NotImplementedError

    def texts(self) -> list[str]:
        raise NotImplementedError


class MockSarvamTTSServer(_BaseMockServer):
    """Speaks the Sarvam text-to-speech/ws protocol."""

    auth_header = "api-subscription-key"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.configs: list[dict] = []
        self.queries: list[dict] = []

    def texts(self) -> list[str]:
        return [m["data"]["text"] for m in self.received if m.get("type") == "text"]

    async def _emit_audio_burst(self, websocket, query):
        if self.behavior == "invalid_json":
            await websocket.send("this is not json {{{")
        if self.behavior == "invalid_b64":
            await websocket.send(json.dumps(
                {"type": "audio", "data": {"audio": "!!not-base64!!"}}
            ))
        if self.behavior == "error_message":
            await websocket.send(json.dumps({
                "type": "error",
                "data": {"message": "synthesis backend unavailable", "code": 503},
            }))
            return
        if self.behavior == "invalid_config":
            # Exactly what api.sarvam.ai returns for a bad config payload
            # (e.g. an unsupported target_language_code).
            await websocket.send(json.dumps({
                "type": "error",
                "data": {"request_id": "req-invalid",
                         "message": "Input parameters has to be a valid dictionary",
                         "code": 422},
            }))
            return
        if self.behavior == "silent":
            return
        for index in range(self.chunks):
            if index == 0 and self.first_chunk_delay:
                await asyncio.sleep(self.first_chunk_delay)
            await websocket.send(json.dumps({
                "type": "audio",
                "data": {"audio": base64.b64encode(PCM_CHUNK).decode(),
                         "request_id": f"req-{index}"},
            }))
        if "send_completion_event=true" in (query.get("_raw") or ""):
            await websocket.send(json.dumps({
                "type": "event", "data": {"event_type": "final", "message": "done"},
            }))

    async def _handler(self, websocket):
        self.connections += 1
        raw_query = urlparse(websocket.request.path).query
        query = {k: v[0] for k, v in parse_qs(raw_query).items()}
        query["_raw"] = raw_query
        self.queries.append(query)
        try:
            async for raw in websocket:
                self.raw_frames.append(raw if isinstance(raw, str) else "<binary>")
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                self.received.append(message)
                kind = message.get("type")
                if kind == "config":
                    self.configs.append(message.get("data") or {})
                elif kind == "text" and self.behavior == "drop_conn":
                    await websocket.close(code=1011, reason="server going away")
                    return
                elif kind == "flush":
                    await self._emit_audio_burst(websocket, query)
        except Exception:  # noqa: BLE001 — mock server should never propagate
            pass


class MockElevenLabsServer(_BaseMockServer):
    """Speaks the ElevenLabs multi-stream-input protocol."""

    auth_header = "xi-api-key"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.paths: list[str] = []
        self.inits: list[dict] = []
        self.closed_contexts: list[str] = []

    def texts(self) -> list[str]:
        return [
            m["text"] for m in self.received
            if m.get("text") not in (None, "", " ") and not m.get("flush")
        ] + [m["text"] for m in self.received if m.get("flush") and m.get("text")]

    def _key(self, name: str) -> str:
        if self.behavior == "snake_case":
            return {"contextId": "context_id", "isFinal": "is_final"}[name]
        return name

    async def _emit_audio_burst(self, websocket, context_id: str):
        if self.behavior == "invalid_json":
            await websocket.send("not-json[[")
        if self.behavior == "invalid_b64":
            await websocket.send(json.dumps(
                {"audio": "%%%bad%%%", self._key("contextId"): context_id}
            ))
        if self.behavior == "error_message":
            await websocket.send(json.dumps({
                "message": "rate limit exceeded", "code": 429,
                self._key("contextId"): context_id,
            }))
            return
        if self.behavior == "silent":
            return
        for index in range(self.chunks):
            if index == 0 and self.first_chunk_delay:
                await asyncio.sleep(self.first_chunk_delay)
            await websocket.send(json.dumps({
                "audio": base64.b64encode(PCM_CHUNK).decode(),
                self._key("contextId"): context_id,
                self._key("isFinal"): None,
            }))

    async def _handler(self, websocket):
        self.connections += 1
        self.paths.append(websocket.request.path)
        pending_flush: set[str] = set()
        try:
            async for raw in websocket:
                self.raw_frames.append(raw if isinstance(raw, str) else "<binary>")
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                self.received.append(message)
                context_id = message.get("context_id") or message.get("contextId")
                if message.get("close_socket"):
                    await websocket.close()
                    return
                if message.get("close_context"):
                    self.closed_contexts.append(context_id)
                    if self.behavior == "late_after_close":
                        # Emit audio for the closed context — must be rejected.
                        saved, self.behavior = self.behavior, "normal"
                        await self._emit_audio_burst(websocket, context_id)
                        self.behavior = saved
                    await websocket.send(json.dumps({
                        self._key("isFinal"): True,
                        self._key("contextId"): context_id,
                    }))
                    continue
                if message.get("text") == " " and context_id:
                    self.inits.append(message)
                    continue
                if message.get("text") and self.behavior == "drop_conn":
                    await websocket.close(code=1011, reason="gone")
                    return
                if message.get("flush") and context_id:
                    pending_flush.add(context_id)
                    await self._emit_audio_burst(websocket, context_id)
        except Exception:  # noqa: BLE001
            pass
