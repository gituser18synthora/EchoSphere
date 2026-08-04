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
"""

import logging

from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.sarvam.stt import SarvamSTTService

logger = logging.getLogger(__name__)


class EndpointedSarvamSTTService(SarvamSTTService):
    """Sarvam streaming STT that marks its segment finals as finalized.

    Behaviour is otherwise identical to the upstream service — this only
    supplies the metadata the turn-stop strategy needs to skip its
    provider-latency safety net once the transcript has actually arrived.
    """

    async def push_frame(
        self, frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ):
        if isinstance(frame, TranscriptionFrame) and not frame.finalized:
            # Every Sarvam `data` message is a complete segment transcript, so
            # there is nothing further to wait for on this segment.
            frame.finalized = True
        await super().push_frame(frame, direction)
