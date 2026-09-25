"""Turn end that waits for the STT final — but only as long as the STT's own
measured final latency says it is worth waiting.

Pipecat's ``SpeechTimeoutUserTurnStopStrategy`` runs two timers after the VAD
stop (the tenant's pause window and an STT safety net worth
``ttfs_p99_latency - stop_secs``) and, with ``wait_for_transcript=True``, never
closes the turn until a transcript has arrived. That last rule is what made the
flag unusable here: a VAD blip that the STT never transcribes (noise, a cough,
a dropped one-word ack) left the turn open until the processor's 5 s fallback,
which also blocked the next barge-in.

With ``wait_for_transcript=False`` the turn closed on the pause window alone.
On telephony that window is 0.4 s after the VAD stop while 19 % of Sarvam finals
arrive later (p50 0.26 s, p90 0.45 s, worst 1.5 s; local measurement over 216
turns, 2026-09-24): the turn closed with nothing buffered, a late-merge's
re-queued text was dispatched on its own, and the final that followed ran as a
second turn.

This subclass keeps the transcript gate but lets the STT safety net end it: once
the pause window AND the bounded STT wait have both elapsed, the turn closes
whether or not a transcript exists. A final that arrives earlier still
short-circuits the wait (it is marked ``finalized`` by the Sarvam adapter), so
the normal turn keeps the pause-window latency; only a late final is waited for,
and a lost one costs ``ttfs_p99_latency - stop_secs`` at most.
"""

from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)


class FinalBoundedTurnStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    """``SpeechTimeoutUserTurnStopStrategy`` whose transcript requirement lapses
    when the STT safety net has run out."""

    async def _maybe_trigger_user_turn_stopped(self):
        if self._vad_user_speaking:
            return
        if (
            self._wait_for_transcript
            and not self._text
            and not (self._user_speech_wait_done and self._stt_wait_done)
        ):
            # Still inside the bounded wait for the final.
            return
        if self._user_speech_wait_done and self._stt_wait_done:
            await self.trigger_user_turn_stopped()


__all__ = ["FinalBoundedTurnStopStrategy"]
