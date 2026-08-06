"""Word-confirmed barge-in — the user-turn start policy for noisy callers.

With the stock ``VADUserTurnStartStrategy``, ANY audio that survives the
caller audio gate and reads as speech to Silero starts a user turn, and a turn
started while the bot is speaking is an interruption: the reply's audio is
cancelled mid-word. That is the right behaviour for a genuine barge-in, but
background conversation is real speech too — an energy gate cannot separate a
colleague talking near the caller's microphone from the caller, so every blip
of ambient chatter chopped the bot off (observed in browser testing as
"choppy, breaking audio": 2–4 ``barge_in`` cancellations per short call, the
greeting cut twice within 3 seconds).

This strategy makes the *transcript* the arbiter while the bot is speaking:

- bot quiet → VAD starts the turn, exactly as before (latency unchanged);
- bot speaking → VAD activity is noted but does NOT start a turn; the turn
  (and the interruption it implies) fires only when the STT transcribes at
  least ``min_words`` words. Ambient noise rarely survives STT as multiple
  confident words, while a real "एक मिनट रुकिए" does.

The price is honest and bounded: a genuine barge-in now lands when its
transcript does (VAD stop → STT flush → final, roughly a second) instead of
at first sound. A caller who keeps talking still gets the interruption —
their words arrive and cancel the reply — and a one-word backchannel ("हाँ",
"hmm") no longer silences the bot, which is what a human speaker would do.

Known limitation: the word gate counts words in the raw transcript — the
brain's transcript quality gate (script/language checks) runs downstream and
cannot veto the interruption. A multi-word hallucination can therefore still
cancel a reply; with the pre-gate suppressing most noise before STT, that is
rare, and it is exactly as bad as EVERY noise event was without the gate.
"""

import logging

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.base_user_turn_start_strategy import (
    BaseUserTurnStartStrategy,
)

logger = logging.getLogger(__name__)


class WordConfirmedBargeInStrategy(BaseUserTurnStartStrategy):
    """VAD-started turns while the bot is quiet; word-confirmed while it speaks.

    Args:
        min_words: Transcribed words required for a turn start (= interruption)
            while the bot is speaking. Interim transcripts count, so providers
            that stream partials interrupt earlier; Sarvam only emits finals.
    """

    def __init__(self, *, min_words: int = 2, **kwargs):
        super().__init__(**kwargs)
        self._min_words = max(1, int(min_words))
        self._bot_speaking = False

    async def reset(self):
        """Reset on turn start. ``_bot_speaking`` deliberately survives:
        it mirrors frame-derived transport state, not per-turn state — a
        barge-in turn starts precisely while the bot is still speaking."""
        await super().reset()

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        """Start the user turn per the policy above.

        Args:
            frame: The frame to be analyzed.

        Returns:
            STOP when the user turn started, CONTINUE otherwise.
        """
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if not self._bot_speaking:
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            if self._bot_speaking:
                words = len((frame.text or "").split())
                if words >= self._min_words:
                    logger.info(
                        "barge-in confirmed by transcript (%d words >= %d)",
                        words,
                        self._min_words,
                    )
                    await self.trigger_user_turn_started()
                    return ProcessFrameResult.STOP

        return ProcessFrameResult.CONTINUE
