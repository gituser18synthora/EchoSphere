"""ElevenLabs Eleven v3 streaming TTS over the Text-to-Dialogue multi-context
WebSocket.

Eleven v3 is NOT accepted on the Text-to-Speech realtime socket that
``elevenlabs_ws.ElevenLabsWebSocketTTSProvider`` speaks — that endpoint
answers a v3 ``model_id`` with an HTTP 400 at the handshake (probed
2026-09-22). ElevenLabs serves v3 realtime on a separate endpoint with its
own wire protocol, which this adapter implements. Flash/Turbo v2.5 keep using
the other adapter unchanged.

Wire protocol (elevenlabs.io/docs/api-reference/text-to-dialogue/
ttd-multi-websocket):
- URL   wss://api.elevenlabs.io/v1/text-to-dialogue/multi-stream-input
        ?model_id=...&output_format=...[&language_code=...]
- Auth  handshake header ``xi-api-key``
- C->S  init  {"context_id":X,"voices":[vid],"voice_settings":{"stability":s}}
        text  {"context_id":X,"inputs":[{"text":...,"voice_id":vid,
                                         "new_turn":false}]}
        flush {"context_id":X,"flush":true}
        close {"context_id":X,"close_context":true}
        ka    {"context_id":X,"keep_alive":true}
        bye   {"close_socket":true}
- S->C  {"audio":"<b64>","context_id":X}
        {"is_final_audio_for_turn":true,"context_id":X}
        {"is_final":true,"context_id":X}
        {"message":...,"error":...,"code":...,"param":...}

Three protocol facts drive the whole design here; all three were verified
against the live API before this adapter was written:

1. ``close_context`` FLUSHES the context's remaining audio and only then
   emits ``is_final``. It is not a server-side cancellation. A barge-in
   measured 30,604 bytes (~3.8 s) of audio arriving AFTER the close was
   sent. So the local "stop accepting" flag is what actually protects the
   caller, and a graceful end-of-reply close must keep accepting that tail
   or the reply loses its final words. Hence two distinct post-close states:
   ``draining`` (keep the audio) and ``cancelled`` (drop it).

2. Sending anything to a context that is closing is a protocol error that
   closes the WHOLE SOCKET (``{"error":"context_closing","code":1008}`` then
   a 1008 close). One stray sentence after a barge-in would therefore kill
   the call, not just the generation. Every send is gated on the context
   being ``live``, retired generations are remembered so a late sentence can
   never re-open one, and the keepalive only ever touches live contexts.

3. At most 5 simultaneous contexts per connection
   (``{"error":"too_many_contexts","code":1008}``). A closing context still
   counts until its ``is_final`` arrives, so admission control waits for a
   release instead of letting the server error.

``is_final_audio_for_turn`` marks the end of a spoken TURN, not of the
generation: a reply streamed as three sentences into one context emits one
turn-final with ``new_turn=false`` and three with ``new_turn=true``. The
router needs exactly ONE completion per reply after all text and audio are
handled, so only ``is_final`` (the context is closed and drained) is mapped
to the ``final`` event, and continuation sentences always use
``new_turn=false``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import random
from dataclasses import dataclass, field

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.protocol import State

from shared.providers.base import ProviderError
from shared.providers.languages import (
    ELEVENLABS_DIALOGUE_MODELS,
    elevenlabs_supports_language,
    tts_unsupported_language_message,
)
from shared.providers.tts.streaming import (
    StreamingTTSProvider,
    TTSStreamEvent,
    TTSStreamSettings,
)

logger = logging.getLogger("providers.tts.elevenlabs_v3_ws")

# Overridable for ElevenLabs regional hosts (api.in.residency.elevenlabs.io …)
# and mocked end-to-end verification. Separate from the Flash adapter's base
# so a test can point one endpoint at a mock without moving the other.
_WS_BASE = os.environ.get(
    "ELEVENLABS_TTD_WS_BASE", os.environ.get("ELEVENLABS_WS_BASE", "wss://api.elevenlabs.io")
)
# The server closes an idle context after 20 s; keep well inside that.
_KEEPALIVE_SECONDS = 8
# Connect attempts happen inline on the reply path: two short attempts, then
# error/fallback — never tens of seconds of handshake retries while the
# caller waits in silence. Matches the Flash adapter.
_MAX_CONNECT_ATTEMPTS = 2
_CONNECT_TIMEOUT_S = 3.0
_CLOSE_HANDSHAKE_TIMEOUT = 2.0

#: Documented ceiling on simultaneous contexts per connection.
_MAX_CONTEXTS = 5
#: Bounded wait for a closing context to release a slot. In practice at most
#: two contexts overlap (one draining after a barge-in, one new), so this
#: only ever fires if the server stops emitting is_final.
_CONTEXT_SLOT_TIMEOUT_S = 2.0

#: The only voice setting the v3 dialogue models accept. Everything else
#: (similarity_boost/style/speed/use_speaker_boost/auto_mode/
#: chunk_length_schedule) belongs to other models and is dropped rather than
#: sent — the catalog schema is what stops it being configured at all.
_VOICE_SETTING_KEYS = ("stability",)

#: Audio tag used for NATIVE breathing — a breath produced by ElevenLabs
#: inside the speech itself, instead of one of our own pre-rendered clips.
#:
#: Chosen from generated samples, not the documentation. Probed 2026-09-22
#: against this endpoint with Monika, Hindi and English, each tag transcribed
#: back with STT:
#:
#:   tag           hi delta   en delta   leading segment        verdict
#:   [exhales]      +0.96 s    +1.12 s   broadband noise, both  CHOSEN
#:   [sighs]        +0.48 s    +0.16 s   weak/inconsistent
#:   [breathes]     -0.08 s    +0.32 s   no breath at all in hi
#:   [inhales]      +0.08 s    +0.56 s   near-silent in en (a pause)
#:
#: ``[exhales]`` is the only candidate that produced a real breath in BOTH
#: languages. It is quiet — roughly 17 dB below the speech that follows — so
#: it reads as a breath rather than a dramatic sigh. No tag was ever spoken
#: literally: every transcript came back as the plain sentence.
_BREATH_TAG = "[exhales]"

#: Fraction of replies that get the tag when Breathing is ON. Deliberately
#: occasional: a breath before every single reply is a mannerism, not
#: naturalness.
_DEFAULT_BREATH_PROBABILITY = 0.3


def _output_format(codec: str, sample_rate: int) -> str:
    if codec in ("mulaw", "ulaw"):
        return "ulaw_8000"
    if codec == "alaw":
        return "alaw_8000"
    return f"pcm_{sample_rate}"


@dataclass
class _Context:
    """One server-side context == one router generation.

    ``state`` transitions:
        live      -> draining  (finish: end of reply, keep the flushed tail)
        live      -> cancelled (barge-in: drop everything still coming)
        draining  -> cancelled (barge-in during the drain)
        any       -> closed    (is_final received; slot released)
    """

    server_id: str
    state: str = "live"
    delivered: int = 0            # bytes emitted downstream
    discarded: int = 0            # bytes dropped after a cancel
    turn_finals: int = 0
    init_sent: bool = False
    # A reply is streamed as several sentences into ONE context; the breath
    # tag belongs to the reply, so it is decided once on the first chunk and
    # never repeated on the continuations.
    text_sent: bool = False

    @property
    def accepting(self) -> bool:
        """A graceful drain still wants its audio; a cancelled one never does."""
        return self.state in ("live", "draining")

    @property
    def sendable(self) -> bool:
        """Messaging a closing context kills the socket — see module docstring."""
        return self.state == "live"

    @property
    def occupies_slot(self) -> bool:
        """Closing contexts count against the 5-context limit until is_final."""
        return self.state != "closed"


class ElevenLabsV3DialogueTTSProvider(StreamingTTSProvider):
    name = "elevenlabs-v3-ws"

    def __init__(self, settings: TTSStreamSettings) -> None:
        super().__init__(settings)
        self._ws = None
        self._receive_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        # generation_id -> context. Server ids are minted per context and
        # never reused, so a retired generation cannot collide with a live one.
        self._contexts: dict[str, _Context] = {}
        self._by_server: dict[str, str] = {}
        # Generations that must never open a context again: a sentence that
        # arrives after a barge-in would otherwise re-init a closing context
        # and take the socket down with it.
        self._retired: set[str] = set()
        self._context_seq = 0
        self._send_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        # Signalled whenever a context reaches is_final and frees a slot.
        self._slot_released = asyncio.Event()
        # An error frame closes the connection server-side; the close handler
        # must not then report the same failure a second time.
        self._error_reported = False
        # Native-breathing RNG. Instance-local so one call's breath rhythm is
        # independent of every other call, and seedable in tests.
        self._breath_random = random.Random()
        # Set by the consumer when something else already breathed for this
        # reply (the pre-reply latency filler), so the caller never hears two
        # breaths in a row. Cleared once consumed.
        self._breath_suppressed = False

    # ── lifecycle ────────────────────────────────────────────────────────
    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("provider is closed")
        model = (self._settings.model or "").strip()
        language = self._settings.language or ""
        if language and elevenlabs_supports_language(model, language) is False:
            message = tts_unsupported_language_message("elevenlabs", model, language)
            await self._emit_error("invalid_input", message)
            raise ProviderError(self.name, "invalid_input", message)
        if model not in ELEVENLABS_DIALOGUE_MODELS:
            # A misroute (a Flash model reaching this adapter) is a clear
            # configuration error, not a cryptic server-side rejection.
            message = (
                f"ElevenLabs model '{model}' is not a Text-to-Dialogue model — "
                "use the text-to-speech WebSocket adapter for it"
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
                raise ProviderError(self.name, last_category,
                                    "Could not connect to ElevenLabs")

            # A fresh socket has no contexts: whatever the previous one held is
            # gone server-side, so the local bookkeeping starts clean too.
            self._reset_contexts()
            self._error_reported = False
            if self._receive_task is None or self._receive_task.done():
                self._receive_task = asyncio.create_task(self._receive_loop())
            if self._keepalive_task is None or self._keepalive_task.done():
                self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def configure(self, settings: TTSStreamSettings) -> None:
        """Apply new settings, reconnecting only when the wire URL or the
        credential changes. The voice is a per-context field here (not part of
        the URL as on the text-to-speech socket), and ``stability`` rides the
        context init, so a voice or settings change needs no reconnect."""
        old_url = self._build_url()
        old_key = self._settings.api_key
        self._settings = settings
        if self._build_url() != old_url or settings.api_key != old_key:
            if self._ws is not None:
                await self._teardown_socket()

    async def synthesize_stream(self, text: str, *, generation_id: str) -> None:
        if not text:
            return
        if generation_id in self._retired:
            # Late sentence for a barge-in that already happened. Silently
            # dropping it is the whole point: re-opening the context would
            # take the socket (and the call) down.
            logger.debug(
                "elevenlabs-v3: dropping text for retired generation %s",
                str(generation_id)[:12],
            )
            return
        await self.connect()
        if generation_id not in self._contexts:
            await self._open_context(generation_id)
        text = self._with_native_breath(generation_id, text)
        sent = await self._send_to_context(generation_id, {
            # new_turn stays False for every sentence of a reply: the context
            # IS the turn. Setting it per sentence emits one
            # is_final_audio_for_turn per sentence and re-plans prosody
            # mid-reply (verified: 3 turn-finals vs 1).
            "inputs": [{"text": text, "voice_id": self._voice(), "new_turn": False}],
        })
        if not sent:
            logger.debug(
                "elevenlabs-v3: dropped text for a closing/closed context %s",
                str(generation_id)[:12],
            )

    async def flush(self, generation_id: str) -> None:
        try:
            await self._send_to_context(generation_id, {"flush": True})
        except (ConnectionError, ConnectionClosed):
            pass

    async def finish(self, generation_id: str) -> None:
        """End of reply: close the context so its tail flushes and is_final
        arrives promptly. The generation stays ACCEPTING until then — that
        flushed tail carries the reply's final words."""
        try:
            await self._send_to_context(
                generation_id, {"close_context": True}, transition_to="draining")
        except (ConnectionError, ConnectionClosed):
            # The socket is gone; the context died with it.
            self._release_context(generation_id)

    async def cancel(self, generation_id: str) -> None:
        """Barge-in: stop accepting audio immediately, then close the context.

        Order matters. ``close_context`` flushes, so audio keeps arriving for
        a while; the local flag is what keeps it off the caller's line.
        """
        self._retired.add(generation_id)
        self._end_generation(generation_id)
        try:
            # A generation already draining has had its close_context sent;
            # sending a second one would be a message to a closing context.
            # Flipping the state alone is enough to drop the rest of the tail.
            await self._send_to_context(
                generation_id, {"close_context": True},
                transition_to="cancelled", also_transition_from="draining",
            )
        except (ConnectionError, ConnectionClosed):
            self._release_context(generation_id)

    async def close(self) -> None:
        self._closed = True
        self._live_generations.clear()
        ws = self._ws
        if ws is not None and ws.state is State.OPEN:
            try:
                async with self._send_lock:
                    await ws.send(json.dumps({"close_socket": True}))
                await asyncio.wait_for(ws.wait_closed(), timeout=_CLOSE_HANDSHAKE_TIMEOUT)
            except (TimeoutError, ConnectionClosed, OSError):
                pass
        await self._teardown_socket()

    # ── native breathing ─────────────────────────────────────────────────
    def suppress_next_breath(self) -> None:
        """Skip the native breath on the next reply.

        Called when something else has already breathed for this reply — the
        pre-reply latency filler plays one of our clips into the thinking gap,
        and a tag on the first sentence would land a second breath a moment
        later.
        """
        self._breath_suppressed = True

    def _native_breath_enabled(self) -> bool:
        return bool((self._settings.params or {}).get("native_breathing"))

    def _with_native_breath(self, generation_id: str, text: str) -> str:
        """Prefix the reply's FIRST chunk with the breath tag, sometimes.

        The tag is added here, at the very last moment before the wire, for
        three reasons:
        - every sanitizer has already run. ``sanitize_spoken_text`` strips
          bracketed placeholders (``[aapka naam]``) and would eat the tag;
        - the consumer's transcript and conversation history are built from
          the text it dispatched, which never contains the tag;
        - no other provider's adapter can ever see it, so a Flash or Sarvam
          engine cannot be handed a v3-only audio tag.
        """
        ctx = self._contexts.get(generation_id)
        if ctx is None or ctx.text_sent:
            return text
        ctx.text_sent = True
        suppressed, self._breath_suppressed = self._breath_suppressed, False
        if suppressed or not self._native_breath_enabled():
            return text
        params = self._settings.params or {}
        try:
            probability = float(params.get("native_breath_probability",
                                           _DEFAULT_BREATH_PROBABILITY))
        except (TypeError, ValueError):
            probability = _DEFAULT_BREATH_PROBABILITY
        probability = min(1.0, max(0.0, probability))
        if self._breath_random.random() >= probability:
            return text
        logger.debug("elevenlabs-v3: native breath on generation %s",
                     str(generation_id)[:12])
        return f"{_BREATH_TAG} {text}"

    # ── internals ────────────────────────────────────────────────────────
    def _voice(self) -> str:
        return (self._settings.voice or "").strip()

    def _build_url(self) -> str:
        s = self._settings
        params = dict(s.params or {})
        model = (s.model or "").strip() or "eleven_v3_conversational"
        url = (
            f"{_WS_BASE}/v1/text-to-dialogue/multi-stream-input"
            f"?model_id={model}"
            f"&output_format={_output_format(s.codec, s.sample_rate)}"
        )
        normalization = params.get("apply_text_normalization")
        if normalization in ("auto", "on", "off"):
            url += f"&apply_text_normalization={normalization}"
        if params.get("sync_alignment"):
            url += "&sync_alignment=true"
        # No language_code: the v3 dialogue models are not in
        # ELEVENLABS_LANGUAGE_ENFORCING_MODELS. They infer the language from
        # the text (verified for hi/mr/ur/ml, with and without the
        # parameter), and enforcing a code we have not validated per language
        # risks the 400/1008 unsupported_language failure mode that made
        # Flash v2.5 go mute on six of our languages.
        return url

    def _voice_settings(self) -> dict:
        params = self._settings.params or {}
        return {
            key: params[key]
            for key in _VOICE_SETTING_KEYS if params.get(key) is not None
        }

    async def _open_context(self, generation_id: str) -> _Context:
        """Mint a fresh server context for a generation, respecting the limit."""
        # Waiting for a slot must happen OUTSIDE the send lock, or a context
        # can never be released while we hold it.
        await self._await_context_slot()
        async with self._send_lock:
            existing = self._contexts.get(generation_id)
            if existing is not None:      # opened while we waited
                return existing
            self._context_seq += 1
            # Never derive the server id from the generation id: ids must be
            # unique for the lifetime of the socket so a retired context can
            # never be addressed again.
            server_id = f"g{self._context_seq}"
            ctx = _Context(server_id=server_id)
            self._contexts[generation_id] = ctx
            self._by_server[server_id] = generation_id
            self._begin_generation(generation_id)
            message: dict = {"context_id": server_id, "voices": [self._voice()]}
            voice_settings = self._voice_settings()
            if voice_settings:
                message["voice_settings"] = voice_settings
            try:
                await self._raw_send(message)
            except (ConnectionError, ConnectionClosed):
                ctx.state = "closed"
                self._by_server.pop(server_id, None)
                self._contexts.pop(generation_id, None)
                self._end_generation(generation_id)
                self._slot_released.set()
                raise
            ctx.init_sent = True
            return ctx

    async def _send_to_context(
        self, generation_id: str, message: dict, *,
        transition_to: str | None = None,
        also_transition_from: str | None = None,
    ) -> bool:
        """Send one context message, re-checking the context state while
        holding the send lock.

        The re-check has to happen under the lock: a barge-in landing between
        a caller's ``sendable`` check and its ``await ws.send`` would deliver
        a message to a closing context, and the server answers that by
        closing the WHOLE socket. Returns False when the context is gone or
        no longer accepts messages.

        ``transition_to`` flips the state in the same critical section, so a
        concurrent sender can never observe the pre-transition state.
        ``also_transition_from`` names one extra state that may still be
        transitioned (a cancel over a draining context) WITHOUT resending —
        that context already had its close_context.
        """
        async with self._send_lock:
            ctx = self._contexts.get(generation_id)
            if ctx is None or ctx.state == "closed":
                return False
            if ctx.state != "live":
                if (also_transition_from is not None
                        and ctx.state == also_transition_from
                        and transition_to is not None):
                    ctx.state = transition_to
                return False
            if transition_to is not None:
                ctx.state = transition_to
            await self._raw_send({**message, "context_id": ctx.server_id})
            return True

    async def _await_context_slot(self) -> None:
        """Block until fewer than 5 contexts occupy the connection."""
        deadline = asyncio.get_running_loop().time() + _CONTEXT_SLOT_TIMEOUT_S
        while self._occupied_slots() >= _MAX_CONTEXTS:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProviderError(
                    self.name, "timeout",
                    "ElevenLabs connection is holding the maximum of "
                    f"{_MAX_CONTEXTS} contexts and none was released in time",
                )
            self._slot_released.clear()
            try:
                await asyncio.wait_for(self._slot_released.wait(), timeout=remaining)
            except TimeoutError:
                continue

    def _occupied_slots(self) -> int:
        return sum(1 for c in self._contexts.values() if c.occupies_slot)

    def _release_context(self, generation_id: str) -> None:
        """Mark a context closed and free its slot (idempotent)."""
        ctx = self._contexts.get(generation_id)
        if ctx is None:
            return
        ctx.state = "closed"
        self._by_server.pop(ctx.server_id, None)
        self._contexts.pop(generation_id, None)
        self._end_generation(generation_id)
        self._slot_released.set()

    def _reset_contexts(self) -> None:
        self._breath_suppressed = False
        self._contexts.clear()
        self._by_server.clear()
        self._live_generations.clear()
        self._slot_released.set()

    async def _raw_send(self, message: dict) -> None:
        """Write a frame. Callers MUST already hold ``_send_lock`` — every
        send is paired with a context-state check that has to be atomic
        against a concurrent barge-in."""
        if self._ws is None or self._ws.state is not State.OPEN:
            raise ConnectionError("ElevenLabs websocket is not connected")
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
                    logger.warning("elevenlabs-v3: discarding non-JSON frame")
                    continue
                await self._handle_message(message)
        except ConnectionClosed as exc:
            if not self._closed and not self._error_reported and self._live_generations:
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
            logger.exception("elevenlabs-v3: receive loop failed")
        finally:
            # The socket is gone: every context died with it. Reconnecting
            # lazily on the next generation is the recovery path.
            self._reset_contexts()
            if not self._closed:
                await self._emit(TTSStreamEvent(kind="disconnected"))

    async def _handle_message(self, message: dict) -> None:
        server_id = message.get("context_id") or message.get("contextId")
        generation = self._by_server.get(server_id) if server_id else None
        ctx = self._contexts.get(generation) if generation else None

        if message.get("error") or (
            message.get("code") is not None and message.get("message")
        ):
            await self._handle_error_frame(message, generation)
            return

        if message.get("audio"):
            if ctx is None:
                return
            try:
                audio = base64.b64decode(message["audio"])
            except (binascii.Error, ValueError):
                logger.warning("elevenlabs-v3: discarding invalid base64 audio chunk")
                return
            if not audio:
                return
            if not ctx.accepting:
                # Post-barge-in flush audio. Counted so the discard is
                # observable in logs, never forwarded.
                ctx.discarded += len(audio)
                return
            ctx.delivered += len(audio)
            await self._emit(TTSStreamEvent(
                kind="audio", generation_id=generation, audio=audio,
            ))
            return

        if message.get("is_final_audio_for_turn"):
            # End of a spoken turn, NOT of the generation. The router owes
            # exactly one completion per reply, so this is bookkeeping only.
            if ctx is not None:
                ctx.turn_finals += 1
            logger.debug(
                "elevenlabs-v3: turn audio complete (context=%s, turn=%d)",
                str(server_id), ctx.turn_finals if ctx else -1,
            )
            return

        if message.get("is_final"):
            if ctx is None:
                return
            cancelled = ctx.state == "cancelled"
            delivered, discarded = ctx.delivered, ctx.discarded
            self._release_context(generation)
            if cancelled:
                logger.info(
                    "elevenlabs-v3: context closed after barge-in "
                    "(delivered=%dB discarded=%dB)", delivered, discarded,
                )
                return
            if not delivered:
                # ElevenLabs reports some account-level failures (unpaid
                # invoice, gated voice) by ending the generation with no audio
                # instead of an error frame. A "successful" final that renders
                # as silence is worse than an error.
                await self._emit_error(
                    "upstream",
                    "ElevenLabs ended the generation without returning any "
                    "audio. The REST text-to-speech endpoint reports the "
                    "underlying reason (account, quota or voice/model "
                    "configuration).",
                    generation_id=generation,
                )
                return
            await self._emit(TTSStreamEvent(kind="final", generation_id=generation))
            return

    async def _handle_error_frame(self, message: dict, generation: str | None) -> None:
        """An error frame closes the whole connection server-side.

        So it is reported for every live generation (not just the addressed
        one) and the socket is torn down; the next generation reconnects.
        """
        text = str(message.get("message") or message.get("error"))[:200]
        code = message.get("code")
        category = self.categorize_close(code if isinstance(code, int) else None, text)
        targets = [generation] if generation else list(self._live_generations)
        if not targets:
            targets = [None]
        self._error_reported = True
        for target in targets:
            await self._emit_error(category, text, generation_id=target)
        logger.warning("elevenlabs-v3: error frame (%s): %s", category, text)
        self._reset_contexts()

    async def _keepalive_loop(self) -> None:
        """Per-context keepalive. Only LIVE contexts are addressed: a
        keep_alive aimed at a closing context is the same protocol violation
        as a stray sentence and would close the socket."""
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_SECONDS)
                if self._ws is None or self._ws.state is not State.OPEN:
                    continue
                for generation_id in list(self._contexts):
                    # State is re-checked under the send lock inside
                    # _send_to_context: a barge-in landing between the
                    # iteration and the write would otherwise aim a keepalive
                    # at a closing context and close the socket.
                    await self._send_to_context(generation_id, {"keep_alive": True})
        except (asyncio.CancelledError, ConnectionClosed, ConnectionError):
            pass

    async def _teardown_socket(self) -> None:
        for task in (self._receive_task, self._keepalive_task):
            if task is not None and not task.done():
                task.cancel()
        self._receive_task = None
        self._keepalive_task = None
        self._reset_contexts()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 — best-effort close
                pass
