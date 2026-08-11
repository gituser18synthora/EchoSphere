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

This strategy makes the *transcript* the arbiter while the bot is speaking,
with a sustained-speech fallback for providers that cannot produce one
mid-utterance:

- bot quiet → VAD starts the turn, exactly as before (latency unchanged);
- bot speaking → VAD activity is noted but does NOT start a turn; the turn
  (and the interruption it implies) fires when EITHER
  (a) the STT transcribes at least ``min_words`` words — ambient noise rarely
  survives STT as multiple confident words, while a real "एक मिनट रुकिए"
  does; OR
  (b) VAD speech has been SUSTAINED for ``vad_fallback_secs`` — the
  transcript arbiter assumes interim transcripts exist, but Sarvam's
  streaming STT only produces a final when its socket is flushed at VAD
  stop, so a caller who keeps talking would otherwise never be able to
  interrupt at all. Post-gate noise (already 200 ms sustained and above the
  echo margin) very rarely also sustains Silero speech for a full second.
- bot stops speaking while gated VAD speech is live → the turn opens
  immediately: the word gate's rationale (protecting audible speech from
  being chopped) no longer applies, and without this the caller's opening
  words waited on the transcript for nothing.

A one-word backchannel ("हाँ", "hmm") still never silences the bot: it
neither reaches ``min_words`` nor sustains VAD speech for the fallback
window.
"""

import logging
import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.base_user_turn_start_strategy import (
    BaseUserTurnStartStrategy,
)

logger = logging.getLogger(__name__)


class WordConfirmedBargeInStrategy(BaseUserTurnStartStrategy):
    """VAD-started turns while the bot is quiet; word- or duration-confirmed
    while it speaks.

    Args:
        min_words: Transcribed words required for a turn start (= interruption)
            while the bot is speaking. Interim transcripts count, so providers
            that stream partials interrupt earlier; Sarvam only emits finals.
        vad_fallback_secs: Sustained gated VAD speech that confirms a barge-in
            without any transcript. 0 disables the fallback (transcript-only
            confirmation, the pre-2026-08-11 behaviour).
    """

    def __init__(
        self, *, min_words: int = 2, vad_fallback_secs: float = 1.0, **kwargs
    ):
        super().__init__(**kwargs)
        self._min_words = max(1, int(min_words))
        self._vad_fallback_secs = max(0.0, float(vad_fallback_secs))
        self._bot_speaking = False
        self._vad_speech_since: float | None = None

    async def reset(self):
        """Reset on turn start. ``_bot_speaking`` deliberately survives:
        it mirrors frame-derived transport state, not per-turn state — a
        barge-in turn starts precisely while the bot is still speaking."""
        self._vad_speech_since = None
        await super().reset()

    async def _confirm(self, why: str) -> ProcessFrameResult:
        self._vad_speech_since = None
        logger.info("barge-in confirmed (%s)", why)
        await self.trigger_user_turn_started()
        return ProcessFrameResult.STOP

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
            if self._vad_speech_since is not None:
                # The caller began speaking over the bot's final syllables and
                # the gate held the turn; with the bot now quiet there is
                # nothing left to protect — open the turn immediately instead
                # of waiting for a transcript.
                return await self._confirm("bot stopped during gated speech")
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if not self._bot_speaking:
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            if self._vad_speech_since is None:
                self._vad_speech_since = time.monotonic()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            # The speech was not sustained through the fallback window; a new
            # VAD start begins a fresh window.
            self._vad_speech_since = None
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            if self._bot_speaking:
                words = len((frame.text or "").split())
                if words >= self._min_words:
                    return await self._confirm(
                        f"transcript ({words} words >= {self._min_words})"
                    )

        if (
            self._bot_speaking
            and self._vad_fallback_secs > 0
            and self._vad_speech_since is not None
            and time.monotonic() - self._vad_speech_since
            >= self._vad_fallback_secs
        ):
            # Checked on every frame (audio arrives every ~20 ms), so the
            # fallback fires within a frame of its deadline without a timer
            # task to manage.
            return await self._confirm(
                f"sustained VAD speech >= {self._vad_fallback_secs:.1f}s"
            )

        return ProcessFrameResult.CONTINUE
