"""A confirmed interrupted sentence keeps its final after turn-close.

cv_f5293164ece8: "अरे, आपको बार-बार।" was accepted during a confirmed
barge-in. The turn closed before "कॉल करते हैं।" arrived. Its audio still
overlapped the bot, so the tail was held as unconfirmed and later discarded
when the bot repeated its question. Exercise that frame ordering, with the
background guard still rejecting speech outside the proven caller episode.
"""

import asyncio
import time

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from tests.unit.test_brain_turn_taking import stub_turn_handler
from tests.unit.test_held_segment_guard import kinds, make_brain, say
from tests.unit.test_workflow_closing import cleanup


DOWN = FrameDirection.DOWNSTREAM
FIRST = "अरे, आपको बार-बार।"
TAIL = "कॉल करते हैं।"


@pytest.fixture
async def caller_turn():
    brain, gate, _ = make_brain()
    handled, started = stub_turn_handler(brain)
    yield brain, gate, handled, started
    brain._cancel_held_continuation_timer()
    await brain._stop_backchannel_monitor()
    await cleanup(brain)


async def interrupted_fragment_then_close(brain, gate, *, initially_held=False):
    await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
    await brain.process_frame(VADUserStartedSpeakingFrame(), DOWN)
    if initially_held:
        await say(brain, gate, FIRST, during_bot=True)
        assert len(brain._held_multiword) == 1
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
    else:
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await say(brain, gate, FIRST, during_bot=True)
    await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
    # The zero stop window makes the first flush explicitly earlier than
    # this episode's final. Smart Turn must wait for the trailing final.
    await brain.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0), DOWN)
    brain._latency.note_smart_turn(
        probability=.985, is_complete=True, inference_ms=68,
    )
    brain._latency.mark_endpoint_by("smart_turn")
    await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
    assert brain._pending_segments == [FIRST]
    assert brain._smart_turn_closed


@pytest.mark.parametrize("initially_held", [False, True])
async def test_confirmed_interrupt_tail_after_turn_close_is_dispatched_as_one_sentence(
    caller_turn, initially_held,
):
    brain, gate, handled, started = caller_turn
    await interrupted_fragment_then_close(brain, gate, initially_held=initially_held)
    # Gate evidence describes capture time, before the interrupted bot stopped.
    await say(brain, gate, TAIL, during_bot=True)
    await asyncio.wait_for(started.wait(), 1)
    assert handled == [f"{FIRST} {TAIL}"]
    assert brain._held_multiword == []
    # Starting the reply cannot discard a caller fragment that was held wrong.
    await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
    discarded = [data for kind, data in brain._recorder.events
                 if kind == "stt_held_segment_discarded"]
    assert not discarded


@pytest.mark.parametrize("boundary", [
    "new_vad_episode", "new_user_turn", "new_gate_segment", "new_reply", "expired_final",
    "new_provider_connection", "new_provider_turn",
])
async def test_overlap_cannot_cross_a_new_speech_reply_or_provider_boundary(caller_turn, boundary):
    brain, gate, handled, _ = caller_turn
    await interrupted_fragment_then_close(brain, gate)
    if boundary == "new_vad_episode":
        await brain.process_frame(VADUserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(VADUserStoppedSpeakingFrame(), DOWN)
    elif boundary == "new_user_turn":
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
    elif boundary == "new_gate_segment":
        gate.segments_started += 1
    elif boundary == "new_reply":
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
    elif boundary == "expired_final":
        brain._last_turn_closed_at = time.monotonic() - 2
    elif boundary == "new_provider_connection":
        brain._last_buffered_final["connection"] = "previous-connection"
    elif boundary == "new_provider_turn":
        brain._last_buffered_final.update(connection="same-connection", turn_index=1)
    gate.snapshot = {
        "snr_db": 30, "speech_dbfs": -30, "during_bot_audio": True,
        "segment_ms": 1200, "live": False,
    }
    tail = TranscriptionFrame(text=TAIL, user_id="u", timestamp="t")
    if boundary == "new_provider_connection":
        tail.result = {"request_id": "new-connection"}
    elif boundary == "new_provider_turn":
        tail.result = {"request_id": "same-connection", "turn_index": 2}
    await brain.process_frame(tail, DOWN)
    assert brain._pending_segments == [FIRST]
    assert [entry["text"] for entry in brain._held_multiword] == [TAIL]
    assert handled == []


async def test_unconfirmed_acknowledgement_cannot_admit_overlapping_background_sentence(caller_turn):
    brain, gate, handled, _ = caller_turn
    await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
    await say(brain, gate, "हाँ जी", during_bot=True, segment_ms=400)
    await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
    await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
    await say(brain, gate, "हाँ technician भेज दीजिए", during_bot=True)
    assert brain._pending_segments == ["हाँ जी"]
    assert [entry["text"] for entry in brain._held_multiword] == ["हाँ technician भेज दीजिए"]
    assert "stt_segment_held_multiword" in kinds(brain)
    assert handled == []
