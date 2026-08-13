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
import logging

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.sarvam.stt import SarvamSTTService

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


class EndpointedSarvamSTTService(SarvamSTTService):
    """Sarvam streaming STT that marks its segment finals as finalized.

    Behaviour is otherwise identical to the upstream service, plus the
    barge-in flushing described in the module docstring and a bounded
    mid-call reconnect — all only supply signals/continuity the turn
    controller needs; transcription itself is unchanged.
    """

    def __init__(self, *args, recorder=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._bot_speaking = False
        self._barge_in_flush_task: asyncio.Task | None = None
        self._recorder = recorder
        self._stt_stopping = False
        self._reconnect_attempts = 0

    async def stop(self, frame):
        self._stt_stopping = True
        await super().stop(frame)

    async def cancel(self, frame):
        self._stt_stopping = True
        await super().cancel(frame)

    async def push_frame(
        self, frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ):
        if isinstance(frame, TranscriptionFrame):
            # A delivered transcript proves the connection is healthy again.
            self._reconnect_attempts = 0
            if not frame.finalized:
                # Every Sarvam `data` message is a complete segment
                # transcript, so there is nothing further to wait for.
                frame.finalized = True
        await super().push_frame(frame, direction)

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

    async def process_frame(self, frame, direction: FrameDirection):
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            await self._stop_barge_in_flush()
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if self._bot_speaking:
                self._start_barge_in_flush()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            # The upstream service flushes on this frame itself; the periodic
            # loop has nothing further to do for this utterance.
            await self._stop_barge_in_flush()
        await super().process_frame(frame, direction)

    def _start_barge_in_flush(self) -> None:
        if self._barge_in_flush_task is None or self._barge_in_flush_task.done():
            self._barge_in_flush_task = self.create_task(
                self._barge_in_flush_loop()
            )

    async def _stop_barge_in_flush(self) -> None:
        task, self._barge_in_flush_task = self._barge_in_flush_task, None
        if task is not None and not task.done():
            await self.cancel_task(task)

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
