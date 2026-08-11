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


class EndpointedSarvamSTTService(SarvamSTTService):
    """Sarvam streaming STT that marks its segment finals as finalized.

    Behaviour is otherwise identical to the upstream service, plus the
    barge-in flushing described in the module docstring — both only supply
    signals the turn controller needs; transcription itself is unchanged.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._bot_speaking = False
        self._barge_in_flush_task: asyncio.Task | None = None

    async def push_frame(
        self, frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ):
        if isinstance(frame, TranscriptionFrame) and not frame.finalized:
            # Every Sarvam `data` message is a complete segment transcript, so
            # there is nothing further to wait for on this segment.
            frame.finalized = True
        await super().push_frame(frame, direction)

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
                return
            try:
                await client.flush()
            except Exception:  # noqa: BLE001 — a failed flush must never kill STT
                logger.debug("barge-in flush failed", exc_info=True)
                return
