"""Caller onset ownership across pauses, rewinds and concurrent generations."""
import asyncio
import time

import pytest
from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame, UserStartedSpeakingFrame, BotStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from tests.unit.test_brain_turn_taking import make_brain
from voice_runtime.transcript_timing import TranscriptSpeechTiming

DOWN = FrameDirection.DOWNSTREAM


def test_vad_confirmation_delay_and_earliest_accepted_segment():
    timing = TranscriptSpeechTiming()
    timing.speech_started(1_800_000_010.2, .2)
    timing.accepted_segment(new_buffer=True)
    timing.speech_started(1_800_000_013.2, .2)
    timing.accepted_segment(new_buffer=False)
    timing.speech_stopped()
    assert timing.take_start() == 1_800_000_010
    timing.accepted_segment(new_buffer=True)
    assert timing.take_start() is None


def test_late_segment_of_an_active_episode_keeps_its_original_onset():
    timing = TranscriptSpeechTiming()
    timing.speech_started(1_800_000_010)
    timing.accepted_segment(new_buffer=True)
    assert timing.take_start() == 1_800_000_010
    timing.accepted_segment(new_buffer=True)
    assert timing.take_start() == 1_800_000_010
    timing.speech_stopped()
    timing.accepted_segment(new_buffer=True)
    assert timing.take_start() == 1_800_000_010
    timing.accepted_segment(new_buffer=True)
    assert timing.take_start() is None


@pytest.mark.asyncio
async def test_claim_identity_keeps_repeated_short_text_onsets_separate_before_record_creation():
    brain = make_brain()
    try:
        brain._open_turn_text = "yes"
        brain._open_turn_audio_start = 1_800_000_010
        first = brain._claim_open_turn()
        brain._open_turn_text = "yes"
        brain._open_turn_audio_start = 1_800_000_030
        second = brain._claim_open_turn()
        await brain._rollback_open_turn(first)
        assert brain._transcript_timing.take_start() == 1_800_000_010
        brain._pending_segments.clear()
        await brain._rollback_open_turn(second)
        assert brain._transcript_timing.take_start() == 1_800_000_030
        assert not brain._claimed_audio_starts
    finally:
        brain._closing = True
        await asyncio.sleep(0)
        await brain.cleanup()


@pytest.mark.asyncio
async def test_actual_brain_turn_records_onset_without_changing_final_timestamp():
    brain = make_brain()
    onset = time.time() - 8
    try:
        await brain.process_frame(VADUserStartedSpeakingFrame(timestamp=onset + .2, start_secs=.2), DOWN)
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        brain._append_segment("I need some help with my booking please")
        await brain.process_frame(VADUserStoppedSpeakingFrame(), DOWN)
        brain._turn_active = False
        before = time.time()
        await brain._consume_pending_turn()
        await brain._generation
        record = next(turn for turn in brain._recorder.turns if turn.role == "user")
        assert record.audio_started_at == pytest.approx(onset)
        assert record.timestamp >= before
        assert record.timestamp - record.audio_started_at >= 8
    finally:
        brain._closing = True
        await asyncio.sleep(0)
        await brain.cleanup()


@pytest.mark.asyncio
async def test_generation_anchor_is_frozen_before_another_speech_episode():
    brain = make_brain()
    captured = []
    async def handler(text):
        captured.append((text, brain._turn_audio_start.get()))
    brain._handle_turn = handler
    try:
        brain._transcript_timing.speech_started(1_800_000_010)
        brain._append_segment("first caller utterance")
        await brain._consume_pending_turn()
        brain._transcript_timing.speech_started(1_800_000_030)
        brain._append_segment("next caller utterance")
        await brain._generation
        assert captured == [("first caller utterance", 1_800_000_010)]
        await brain._consume_pending_turn()
        await brain._generation
        assert captured[-1] == ("next caller utterance", 1_800_000_030)
    finally:
        brain._closing = True
        await asyncio.sleep(0)
        await brain.cleanup()


@pytest.mark.asyncio
async def test_bot_audio_end_event_is_separate_from_transcript_times():
    brain = make_brain()
    try:
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        events = [data for kind, data in brain._recorder.events if kind == "bot_audio_end"]
        assert len(events) == 1
        assert events[0]["audio_end_ts"] <= time.time()
        assert not brain._recorder.turns
    finally:
        brain._closing = True
        await asyncio.sleep(0)
        await brain.cleanup()
