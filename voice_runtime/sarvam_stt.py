"""Sarvam realtime STT with correct end-of-turn finalization signalling.

Pipecat's ``SpeechTimeoutUserTurnStopStrategy`` closes a user turn only when
BOTH of its timers have elapsed:

- ``user_speech_timeout`` — the pause window a caller is allowed (our policy);
- ``stt_timeout`` — a safety net worth ``ttfs_p99_latency - stop_secs``, meant
  to cover a slow provider. It is short-circuited the moment a transcript
  arrives with ``TranscriptionFrame.finalized = True``, which means "the STT has
  nothing more to send for this utterance".

``SarvamSTTService`` never sets that flag. It flushes its socket on
``VADUserStoppedSpeakingFrame`` and pushes the resulting transcript with
``finalized`` left at its default of ``False``, so the safety net always ran to
completion: with ``SARVAM_TTFS_P99 = 1.17`` and ``stop_secs = 0.2`` that is a
fixed **970 ms** wait after every utterance, which silently dominated the 800 ms
telephony pause window. It also made the policy timeout look inert — lowering
``user_speech_timeout`` from 0.8 to 0.3 moved the measured endpoint by under
2 ms, because it was never the binding constraint.

Marking the flush result finalized is honest rather than optimistic: Sarvam's
streaming protocol only emits a ``data`` message when a segment is complete
(the service itself reports ``is_final=True`` for every one of them), and we
only ever see one in response to a flush we asked for. Partial hypotheses
arrive as a different message type and never reach this path.

Barge-in flushing: the upstream service produces a transcript ONLY at VAD
stop, so while the bot is speaking a continuously-talking caller generates no
words for the word-confirmed barge-in gate to count — the transcript arbiter
never arrives and the bot talks the caller down. While the bot is speaking
AND gated VAD speech is live, this subclass flushes the socket periodically
so partial finals surface mid-utterance and can confirm the interruption.
The brain's segment buffering merges those partials into one turn exactly as
it merges naturally-split finals.
"""

import asyncio
import base64
import logging

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.sarvam.stt import SarvamSTTService
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# How often to force a segment final while the caller talks over the bot.
# Long enough that a flush usually carries ≥2 confident words (the barge-in
# gate's threshold), short enough that a genuine interruption lands with the
# next flush rather than at the end of the caller's sentence.
_BARGE_IN_FLUSH_INTERVAL_S = 0.7

# The upstream service has no reconnect at all: when the Sarvam socket dies
# (server idle-out, provider restart), its receive task ends silently, audio
# keeps flowing into a dead client and the bot never hears the caller again.
# A bounded number of mid-call reconnects covers that; repeated failures give
# up loudly instead of looping.
_MAX_STT_RECONNECTS = 3

# A VAD stop normally causes the upstream adapter to flush Sarvam immediately.
# If no transcript follows, retry once: a lost/early flush should not force a
# phone caller to repeat an otherwise valid short answer.
_MISSING_FINAL_RETRY_S = 0.65

# Pipecat audio frames contain headerless signed 16-bit little-endian PCM.  The
# Sarvam SDK's generated audio-message schema intentionally keeps the legacy
# ``audio/wav`` envelope value even when raw PCM is selected at connection
# time.  The handshake's ``input_audio_codec`` is what tells the server how to
# decode the bytes; sending ``audio/pcm_s16le`` in each message makes the live
# endpoint close an otherwise healthy socket with code 1000.
_RAW_PCM_CODECS = frozenset({"pcm_s16le"})
_SDK_AUDIO_MESSAGE_ENCODING = "audio/wav"


class _CodecAwareStreamingClient:
    """Inject the raw input codec into the Sarvam WebSocket handshake.

    Pipecat 1.5/1.6 stores ``input_audio_codec`` but does not forward it to the
    SDK's ``connect`` call.  Keeping this as a tiny proxy avoids copying the
    much larger upstream reconnect/connect implementation and stays harmless
    once a newer Pipecat starts forwarding the value itself.
    """

    def __init__(self, client, codec: str) -> None:
        self._client = client
        self._codec = codec

    def connect(self, **kwargs):
        kwargs.setdefault("input_audio_codec", self._codec)
        return self._client.connect(**kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


class EndpointedSarvamSTTService(SarvamSTTService):
    """Sarvam streaming STT that marks its segment finals as finalized.

    Behaviour is otherwise identical to the upstream service, plus the
    barge-in flushing described in the module docstring and a bounded
    mid-call reconnect — all only supply signals/continuity the turn
    controller needs; transcription itself is unchanged.
    """

    def __init__(self, *args, recorder=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if self._input_audio_codec in _RAW_PCM_CODECS:
            endpoint_name = (
                "speech_to_text_translate_streaming"
                if self._config.use_translate_endpoint
                else "speech_to_text_streaming"
            )
            streaming_client = getattr(self._sarvam_client, endpoint_name)
            setattr(
                self._sarvam_client,
                f"_{endpoint_name}",
                _CodecAwareStreamingClient(streaming_client, self._input_audio_codec),
            )
        self._bot_speaking = False
        self._barge_in_flush_task: asyncio.Task | None = None
        self._missing_final_task: asyncio.Task | None = None
        self._utterance_generation = 0
        self._transcript_generation = -1
        self._recorder = recorder
        self._stt_stopping = False
        self._reconnect_attempts = 0
        self._socket_send_failed = False

    async def stop(self, frame):
        self._stt_stopping = True
        await self._stop_missing_final_retry()
        await super().stop(frame)

    async def cancel(self, frame):
        self._stt_stopping = True
        await self._stop_missing_final_retry()
        await super().cancel(frame)

    async def push_frame(
        self, frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ):
        if isinstance(frame, TranscriptionFrame):
            # A delivered transcript proves the connection is healthy again.
            self._reconnect_attempts = 0
            self._socket_send_failed = False
            self._transcript_generation = getattr(self, "_utterance_generation", 0)
            await self._stop_missing_final_retry()
            if not frame.finalized:
                # Every Sarvam `data` message is a complete segment
                # transcript, so there is nothing further to wait for.
                frame.finalized = True
        await super().push_frame(frame, direction)

    async def run_stt(self, audio: bytes):
        """Send raw PCM using the codec negotiated in the WS handshake.

        The SDK message envelope still requires ``audio/wav``; the connection
        level ``input_audio_codec=pcm_s16le`` controls actual decoding.
        """
        if self._input_audio_codec not in _RAW_PCM_CODECS:
            async for frame in super().run_stt(audio):
                yield frame
            return
        if not self._socket_client or self._socket_send_failed:
            yield None
            return
        try:
            await self._send_stream_audio(audio)
        except ConnectionClosed as exc:
            # The receive task owns bounded reconnect.  Stop feeding the dead
            # client meanwhile, otherwise every ~20-30 ms audio chunk emits an
            # ErrorFrame until that task observes the close.
            self._note_closed_socket(exc)
        except Exception as exc:  # noqa: BLE001 — surface provider/socket errors
            yield ErrorFrame(
                error=f"Error sending raw PCM audio to Sarvam: {exc}",
                exception=exc,
            )
        yield None

    async def _send_keepalive(self, silence: bytes):
        if self._input_audio_codec in _RAW_PCM_CODECS:
            if self._socket_send_failed:
                return
            try:
                await self._send_stream_audio(silence)
            except ConnectionClosed as exc:
                self._note_closed_socket(exc)
            return
        await super()._send_keepalive(silence)

    async def _send_stream_audio(self, audio: bytes) -> None:
        client = self._socket_client
        if client is None:
            return
        await client.transcribe(
            audio=base64.b64encode(audio).decode("ascii"),
            encoding=_SDK_AUDIO_MESSAGE_ENCODING,
            sample_rate=self.sample_rate,
        )

    def _note_closed_socket(self, exc: ConnectionClosed) -> None:
        if self._socket_send_failed:
            return
        self._socket_send_failed = True
        logger.warning(
            "sarvam-stt: socket closed while sending audio; waiting for "
            "bounded reconnect (%s)", exc,
        )
        if self._recorder is not None:
            self._recorder.add_event(
                "stt_socket_send_closed", detail=str(exc)[:200]
            )

    async def _receive_task_handler(self):
        await super()._receive_task_handler()
        # Reaching here means start_listening() ended: the socket is dead.
        # (A worker-initiated disconnect cancels this task instead, so the
        # code below never runs on normal teardown.)
        if self._stt_stopping or self._socket_client is None:
            return
        self._reconnect_attempts += 1
        if self._reconnect_attempts > _MAX_STT_RECONNECTS:
            await self.push_error(
                error_msg="Sarvam STT socket closed repeatedly; transcription stopped"
            )
            if self._recorder is not None:
                self._recorder.add_event(
                    "stt_reconnect_gave_up", attempts=self._reconnect_attempts
                )
            return
        logger.warning(
            "sarvam-stt: streaming socket ended mid-call — reconnecting "
            "(attempt %d/%d)", self._reconnect_attempts, _MAX_STT_RECONNECTS,
        )
        if self._recorder is not None:
            self._recorder.add_event(
                "stt_reconnecting", attempt=self._reconnect_attempts
            )
        # Drop the dead client first so run_stt stops writing into it.
        socket_client = self._socket_client
        websocket_context = self._websocket_context
        self._socket_client = None
        self._websocket_context = None
        if websocket_context is not None and socket_client is not None:
            try:
                await websocket_context.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 — the socket is already dead
                logger.debug("sarvam-stt: dead socket close failed", exc_info=True)
        await self._cancel_keepalive_task()
        await asyncio.sleep(min(0.5 * self._reconnect_attempts, 2.0))
        if self._stt_stopping:
            return
        # _connect() spawns a fresh receive task only when no live one is
        # registered — deregister ourselves before calling it.
        if self._receive_task is asyncio.current_task():
            self._receive_task = None
        await self._connect()
        if self._socket_client is not None:
            self._socket_send_failed = False

    async def process_frame(self, frame, direction: FrameDirection):
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            await self._stop_barge_in_flush()
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._utterance_generation += 1
            await self._stop_missing_final_retry()
            if self._bot_speaking:
                self._start_barge_in_flush()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            # The upstream service flushes on this frame itself; the periodic
            # loop has nothing further to do for this utterance.
            await self._stop_barge_in_flush()
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._start_missing_final_retry()

    def _start_barge_in_flush(self) -> None:
        if self._barge_in_flush_task is None or self._barge_in_flush_task.done():
            self._barge_in_flush_task = self.create_task(
                self._barge_in_flush_loop()
            )

    async def _stop_barge_in_flush(self) -> None:
        task, self._barge_in_flush_task = self._barge_in_flush_task, None
        if task is not None and not task.done():
            await self.cancel_task(task)

    def _start_missing_final_retry(self) -> None:
        generation = self._utterance_generation
        if self._transcript_generation == generation:
            return
        if self._missing_final_task is None or self._missing_final_task.done():
            self._missing_final_task = self.create_task(
                self._missing_final_retry(generation)
            )

    async def _stop_missing_final_retry(self) -> None:
        task = getattr(self, "_missing_final_task", None)
        self._missing_final_task = None
        if task is not None and not task.done():
            await self.cancel_task(task)

    async def _missing_final_retry(self, generation: int) -> None:
        await asyncio.sleep(_MISSING_FINAL_RETRY_S)
        if (
            generation != self._utterance_generation
            or generation == self._transcript_generation
            or self._stt_stopping
        ):
            return
        client = self._socket_client
        if client is None:
            return
        logger.warning(
            "sarvam-stt: no transcript after VAD stop; retrying flush once"
        )
        if self._recorder is not None:
            self._recorder.add_event(
                "stt_missing_final_retry", generation=generation
            )
        try:
            await client.flush()
        except Exception:  # noqa: BLE001 — reconnect path owns socket recovery
            logger.debug("sarvam-stt: missing-final retry failed", exc_info=True)

    async def _barge_in_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(_BARGE_IN_FLUSH_INTERVAL_S)
            client = getattr(self, "_socket_client", None)
            if client is None:
                # Mid-reconnect: the next tick may find a live client again.
                # The loop is cancelled at bot-stop/turn-stop either way.
                continue
            try:
                await client.flush()
            except Exception:  # noqa: BLE001 — a failed flush must never kill STT
                # One failed flush (socket died between ticks) must not
                # disable transcript-confirmed barge-in for the whole call.
                logger.debug("barge-in flush failed", exc_info=True)
