"""Held-segment guard: a multi-word final that lands after its own VAD burst
closed is reclaimed by the barge-in that its transcript confirms moments later.

LOCAL e2e vs_G6U5hNZ-6VB-dsSS__jg-iZ_ (2026-11-07, Haier, guard on): the caller
interrupted the intro with "नहीं, मना नहीं किया था। बोला था कि नेक्स्ट डे बुला
लेंगे।". The first flush final arrived 400 ms after the VAD stop (so it was held
with no episode), the turn controller then confirmed the barge-in from those
five words, and the hold was discarded as superseded_by_barge_in: only the
second half of the sentence reached the workflow. With no new physical speech
episode in between and the confirmation inside _HELD_RECLAIM_LATE_FINAL_S, the
held text is the confirmed utterance and is merged into the turn.
"""

import asyncio
import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from tests.unit.test_brain_turn_taking import stub_turn_handler
from tests.unit.test_held_segment_guard import kinds, make_brain, say

DOWN = FrameDirection.DOWNSTREAM


async def _live_pattern(brain, gate):
    await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
    await brain.process_frame(VADUserStartedSpeakingFrame(), DOWN)
    await brain.process_frame(VADUserStoppedSpeakingFrame(), DOWN)          # burst closed before the final
    await say(brain, gate, "नहीं, मना नहीं किया था।", during_bot=True)       # final lands late → held, no episode
    assert len(brain._held_multiword) == 1 and brain._held_multiword[0]["episode"] is None
    assert "stt_segment_held_multiword" in kinds(brain)


class TestLateFinalReclaim:
    async def test_transcript_confirmed_barge_in_reclaims_the_late_final(self):
        brain, gate, _ = make_brain()
        handled, started = stub_turn_handler(brain)
        await _live_pattern(brain, gate)
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)            # confirmed from those words
        assert brain._held_multiword == [] and brain._pending_segments == ["नहीं, मना नहीं किया था।"]
        merged = [d for k, d in brain._recorder.events if k == "stt_held_segment_merged"]
        assert merged and merged[-1]["via"] == "confirmed_barge_in_same_utterance"
        assert "stt_held_segment_discarded" not in kinds(brain)
        await brain.process_frame(VADUserStartedSpeakingFrame(), DOWN)         # the caller goes on
        await say(brain, gate, "बोला था कि नेक्स्ट डे बुला लेंगे।", during_bot=True)
        await brain.process_frame(VADUserStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await asyncio.wait_for(started.wait(), 1)
        assert handled == ["नहीं, मना नहीं किया था। बोला था कि नेक्स्ट डे बुला लेंगे।"]

    async def test_old_late_final_is_still_discarded(self):
        brain, gate, _ = make_brain()
        stub_turn_handler(brain)
        await _live_pattern(brain, gate)
        brain._held_multiword[0]["at"] = time.monotonic() - 1.5                 # held well before this interruption
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        assert brain._pending_segments == []
        discarded = [d for k, d in brain._recorder.events if k == "stt_held_segment_discarded"]
        assert discarded and discarded[-1]["reason"] == "superseded_by_barge_in"

    async def test_new_burst_in_between_still_discards(self):
        brain, gate, _ = make_brain()
        stub_turn_handler(brain)
        await _live_pattern(brain, gate)
        await brain.process_frame(VADUserStartedSpeakingFrame(), DOWN)         # a NEW physical episode
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        assert brain._pending_segments == []
        discarded = [d for k, d in brain._recorder.events if k == "stt_held_segment_discarded"]
        assert discarded and discarded[-1]["reason"] == "superseded_by_barge_in"
