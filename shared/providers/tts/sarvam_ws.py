"""Sarvam AI streaming TTS over WebSocket (bulbul models).

Wire protocol (docs.sarvam.ai/api-reference/text-to-speech/stream):
- URL   wss://api.sarvam.ai/text-to-speech/ws?model=<model>&send_completion_event=true
- Auth  handshake header ``api-subscription-key``
- C→S   {"type":"config","data":{...}} | {"type":"text","data":{"text":...}}
        | {"type":"flush"} | {"type":"ping"}
- S→C   {"type":"audio","data":{"audio":"<b64>", ...}}
        | {"type":"event","data":{"event_type":"final", ...}}
        | {"type":"error","data":{"message":..., "code":<int>}}

Sarvam has no per-generation contexts: exactly one generation is active at a
time, and ``cancel`` closes the socket to stop server-side synthesis (a
background reconnect then rebuilds it so the next generation doesn't pay the
cold handshake). Re-sending config mid-connection is supported
by the API (it auto-flushes) and is how voice/language switches avoid a
reconnect. The server idles out after ~60s, so a ping is sent every 20s.
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
from shared.providers.languages import (
    SARVAM_SUPPORTED_LOCALES,
    to_platform_language,
    to_provider_language,
)
from shared.providers.tts.streaming import (
    StreamingTTSProvider,
    TTSStreamEvent,
    TTSStreamSettings,
)

logger = logging.getLogger("providers.tts.sarvam_ws")

# Overridable for self-hosted gateways and mocked end-to-end verification.
_WS_URL = os.environ.get("SARVAM_TTS_WS_URL", "wss://api.sarvam.ai/text-to-speech/ws")
_KEEPALIVE_SECONDS = 20
# Connect attempts happen inline on the reply path: two short attempts, then
# error/fallback — never tens of seconds of handshake retries while the
# caller waits in silence.
_MAX_CONNECT_ATTEMPTS = 2
_CONNECT_TIMEOUT_S = 3.0
# How long a new dispatch waits for a still-rendering previous generation
# before superseding it (Sarvam has no server-side generation contexts).
_GENERATION_HANDOFF_TIMEOUT_S = 1.5

# Parameters the two bulbul generations accept on the config message.
_V3_PARAMS = {"pace", "temperature", "min_buffer_size", "max_chunk_length",
              "enable_preprocessing", "dict_id"}
_V2_PARAMS = {"pace", "pitch", "loudness", "min_buffer_size", "max_chunk_length",
              "enable_preprocessing"}


class SarvamWebSocketTTSProvider(StreamingTTSProvider):
    name = "sarvam-tts-ws"

    def __init__(self, settings: TTSStreamSettings) -> None:
        super().__init__(settings)
        self._ws = None
        self._receive_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._current_generation: str | None = None
        self._send_lock = asyncio.Lock()
        # Serializes concurrent connect() calls (a post-cancel background
        # reconnect racing the next dispatch) so only one socket is opened.
        self._connect_lock = asyncio.Lock()
        self._reconnect_task: asyncio.Task | None = None
        # Config last sent on the CURRENT socket — a resend auto-flushes
        # server-side, so identical configs are skipped.
        self._last_config: dict | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("provider is closed")
        async with self._connect_lock:
            if self._ws is not None and self._ws.state is State.OPEN:
                return
            model = self._settings.model or "bulbul:v3"
            url = f"{_WS_URL}?model={model}&send_completion_event=true"
            last_category = "timeout"
            for attempt in range(_MAX_CONNECT_ATTEMPTS):
                try:
                    self._ws = await asyncio.wait_for(
                        websocket_connect(
                            url,
                            additional_headers={"api-subscription-key": self._settings.api_key},
                        ),
                        timeout=min(_CONNECT_TIMEOUT_S, self._settings.timeout_seconds),
                    )
                    break
                except InvalidStatus as exc:  # HTTP handshake rejection
                    status = exc.response.status_code
                    category = self.categorize_close(status)
                    await self._emit_error(category, f"Sarvam TTS handshake rejected ({status})")
                    if category == "auth":
                        raise ProviderError(self.name, "auth",
                                            "Sarvam rejected the API key") from exc
                    last_category = category
                except (TimeoutError, OSError, ConnectionClosed):
                    last_category = "timeout"
                    await asyncio.sleep(0.2 * (attempt + 1))
            else:
                await self._emit_error(last_category, "Could not connect to Sarvam TTS")
                raise ProviderError(self.name, last_category, "Could not connect to Sarvam TTS")

            # A fresh socket always gets a config, whatever was sent before.
            self._last_config = None
            await self._send_config()
            if self._receive_task is None or self._receive_task.done():
                self._receive_task = asyncio.create_task(self._receive_loop())
            if self._keepalive_task is None or self._keepalive_task.done():
                self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def configure(self, settings: TTSStreamSettings) -> None:
        """Swap voice/language/params. Reuses the connection when open."""
        self._settings = settings
        if self._ws is not None and self._ws.state is State.OPEN:
            await self._send_config()

    async def synthesize_stream(self, text: str, *, generation_id: str) -> None:
        if not text:
            return
        await self.connect()
        previous = self._current_generation
        if (
            previous is not None
            and previous != generation_id
            and self.generation_alive(previous)
        ):
            # Sarvam has no server-side generation contexts: dispatching new
            # text while the previous generation is still rendering would
            # misattribute its remaining audio and its final. Wait briefly
            # for the in-flight generation to complete; supersede if it
            # doesn't (its tail audio is then attributed to this generation,
            # which is at least in playback order).
            deadline = asyncio.get_running_loop().time() + _GENERATION_HANDOFF_TIMEOUT_S
            while (
                self.generation_alive(previous)
                and self._current_generation == previous
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.05)
            if self.generation_alive(previous) and self._current_generation == previous:
                logger.info(
                    "sarvam-tts: superseding still-open generation before a new dispatch"
                )
                self._end_generation(previous)
                await self._emit(TTSStreamEvent(kind="final", generation_id=previous))
        self._begin_generation(generation_id)
        self._current_generation = generation_id
        await self._send({"type": "text", "data": {"text": text}})

    async def flush(self, generation_id: str) -> None:
        if self._ws is not None and self._ws.state is State.OPEN:
            await self._send({"type": "flush"})

    async def cancel(self, generation_id: str) -> None:
        """Sarvam cannot cancel server-side synthesis: drop the connection."""
        self._end_generation(generation_id)
        if self._current_generation == generation_id:
            self._current_generation = None
            await self._teardown_socket()
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Rebuild the socket in the background after a barge-in teardown so
        the next reply doesn't pay the cold-connect handshake inline."""
        if self._closed:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        try:
            await self.connect()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the next dispatch reconnects lazily
            logger.info(
                "sarvam-tts: background reconnect after cancel failed; the "
                "next dispatch will reconnect"
            )

    async def close(self) -> None:
        self._closed = True
        self._current_generation = None
        self._live_generations.clear()
        task, self._reconnect_task = self._reconnect_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        await self._teardown_socket()

    # ── internals ────────────────────────────────────────────────────────
    async def _send(self, message: dict) -> None:
        if self._ws is None or self._ws.state is not State.OPEN:
            raise ConnectionError("Sarvam TTS websocket is not connected")
        async with self._send_lock:
            await self._ws.send(json.dumps(message))

    def _build_config(self) -> dict:
        s = self._settings
        model = s.model or "bulbul:v3"
        is_v3 = model.startswith("bulbul:v3")
        allowed = _V3_PARAMS if is_v3 else _V2_PARAMS
        params = dict(s.params or {})

        # Canonical language mapping shared with the REST implementation: bare
        # ISO codes expand to full locales ("en" → "en-IN") and the platform
        # Odia alias is applied. Sarvam 422-rejects the whole config message on
        # an unsupported target_language_code (no audio at all), so anything
        # outside the supported set is normalized to en-IN — loudly, never
        # silently — mirroring the REST implementation's Latin-text fallback.
        language = to_provider_language("sarvam", s.language) or "en-IN"
        if to_platform_language("sarvam", language) not in SARVAM_SUPPORTED_LOCALES:
            logger.warning(
                "sarvam-tts: language '%s' is not supported by the Sarvam TTS API — "
                "using en-IN for this stream (model=%s)", s.language, model,
            )
            language = "en-IN"
        codec = "linear16" if s.codec in ("linear16", "pcm") else s.codec
        # Speaker codes must be lowercase, unpadded strings on the wire. An
        # unknown speaker is rejected by Sarvam (surfaced as an error event) —
        # it is never silently replaced here; only a MISSING speaker falls
        # back to the model default, and that fallback is logged.
        speaker = str(s.voice or "").strip().lower()
        if not speaker:
            speaker = "shubh" if is_v3 else "anushka"
            logger.info(
                "sarvam-tts: no speaker configured; using model default '%s' for %s",
                speaker, model,
            )
        config = {
            "model": model,
            "target_language_code": language,
            "speaker": speaker,
            "speech_sample_rate": str(s.sample_rate),
            "output_audio_codec": codec,
        }
        if is_v3:
            # v3 always preprocesses; the flag is not negotiable.
            params["enable_preprocessing"] = True

        ignored: list[str] = []
        for key, value in params.items():
            if value is None or key in ("send_completion_event",):
                continue
            if key in allowed:
                config[key] = value
            else:
                ignored.append(key)
        if ignored:
            logger.warning(
                "sarvam-tts: ignoring parameters unsupported by %s: %s",
                model, ", ".join(sorted(ignored)),
            )
        return config

    async def _send_config(self) -> None:
        # A config resend auto-flushes server-side — skipped when nothing
        # changed on the current socket. Fresh sockets always send (the cache
        # resets on connect/teardown).
        config = self._build_config()
        if config == self._last_config:
            return
        await self._send({"type": "config", "data": config})
        self._last_config = config

    async def _receive_loop(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("sarvam-tts: discarding non-JSON frame")
                    continue
                await self._handle_message(message)
        except ConnectionClosed as exc:
            if not self._closed and self._current_generation is not None:
                category = self.categorize_close(exc.rcvd.code if exc.rcvd else None,
                                                 exc.rcvd.reason if exc.rcvd else "")
                await self._emit_error(
                    category, "Sarvam TTS connection closed mid-generation",
                    generation_id=self._current_generation,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sarvam-tts: receive loop failed")
        finally:
            if self._receive_task is asyncio.current_task():
                # connect() only spawns a new receive loop when the old one is
                # gone; clearing here (not just on teardown) closes the race
                # where a reconnect lands while this task is still finishing.
                self._receive_task = None
            if not self._closed:
                await self._emit(TTSStreamEvent(kind="disconnected"))
                # Whatever ended this socket (server idle-out, post-error
                # close), rebuild it in the background so the next reply does
                # not pay the cold handshake. Deduplicated inside.
                self._schedule_reconnect()

    async def _handle_message(self, message: dict) -> None:
        kind = message.get("type")
        data = message.get("data") or {}
        generation = self._current_generation
        if kind == "audio":
            # Late-audio rejection: chunks for cancelled generations are dropped.
            if not self.generation_alive(generation):
                return
            try:
                audio = base64.b64decode(data.get("audio") or "")
            except (binascii.Error, ValueError):
                logger.warning("sarvam-tts: discarding invalid base64 audio chunk")
                return
            if audio:
                await self._emit(TTSStreamEvent(
                    kind="audio", generation_id=generation, audio=audio,
                ))
        elif kind == "event" and data.get("event_type") == "final":
            if self.generation_alive(generation):
                self._end_generation(generation)
                self._current_generation = None
                await self._emit(TTSStreamEvent(kind="final", generation_id=generation))
        elif kind == "error":
            message_text = str(data.get("message") or "Sarvam TTS error")
            code = data.get("code")
            category = self.categorize_close(
                code if isinstance(code, int) else None, message_text
            )
            await self._emit_error(category, message_text[:200], generation_id=generation)
            # Sarvam closes the socket right after an in-band error (observed
            # live: 422 → close 1000). Drop the failed generation now so the
            # close is not reported as a SECOND mid-generation failure; the
            # receive loop's exit schedules the reconnect.
            self._end_generation(generation)
            self._current_generation = None

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_SECONDS)
                if self._ws is not None and self._ws.state is State.OPEN:
                    async with self._send_lock:
                        await self._ws.send(json.dumps({"type": "ping"}))
        except (asyncio.CancelledError, ConnectionClosed):
            pass

    async def _teardown_socket(self) -> None:
        for task in (self._receive_task, self._keepalive_task):
            if task is not None and not task.done():
                task.cancel()
        self._receive_task = None
        self._keepalive_task = None
        self._last_config = None
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 — best-effort close
                pass
