"""Interrupted filler ownership must end even after its producer has finished."""

import asyncio
from unittest.mock import patch

import pytest
from pipecat.frames.frames import BotStartedSpeakingFrame, InterruptionFrame, TTSAudioRawFrame, UserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameProcessor

from tests.unit.test_latency_filler import DOWN, UP, _ShortLibrary, filler_audio, make_filler, wait
from voice_runtime.frames import FillerClearFrame


@pytest.mark.parametrize("finished", [False, True])
@pytest.mark.parametrize("reason", ["interruption", "caller_speech", "barge_in", "new_turn"])
async def test_cancellation_retires_output_even_after_production(reason, finished):
    filler = make_filler(delay_ms=10, library=_ShortLibrary(clip_ms=40 if finished else 600))
    await filler.arm(turn_id=1, gender="male")
    await wait(0.08)
    owner = filler_audio(filler)[0].owner
    if finished:
        assert filler._task is None
    if reason in ("interruption", "caller_speech"):
        frame = InterruptionFrame() if reason == "interruption" else UserStartedSpeakingFrame()
        await filler.process_frame(frame, DOWN)
        assert filler.pushed[-1] == (frame, DOWN)
    else:
        await filler.cancel(reason)
    assert owner.cancelled
    assert [f.owner for f, _ in filler.pushed if isinstance(f, FillerClearFrame)] == [owner]
    assert filler._output_owners == []


async def test_interruption_retires_owner_before_processor_queue_can_yield():
    filler = make_filler(delay_ms=10)
    await filler.arm(turn_id=1, gender="male")
    await wait(0.04)
    owner = filler_audio(filler)[0].owner

    async def queue(*args, **kwargs):
        assert owner.cancelled
    try:
        with patch.object(FrameProcessor, "queue_frame", side_effect=queue):
            await filler.queue_frame(InterruptionFrame(), DOWN)
    finally:
        await filler.cancel()


async def test_six_interruptions_allow_fresh_filler_and_do_not_affect_another_call():
    filler = make_filler(delay_ms=10, library=_ShortLibrary(clip_ms=600))
    other = make_filler(delay_ms=10, library=_ShortLibrary(clip_ms=600))
    await other.arm(turn_id=1, gender="female")
    owners = []
    try:
        for turn in range(1, 7):
            await filler.arm(turn_id=turn, gender="male")
            await wait(0.04)
            owner = filler_audio(filler)[-1].owner
            assert owner not in owners and not owner.cancelled
            owners.append(owner)
            await filler.process_frame(InterruptionFrame(), DOWN)
            await filler.process_frame(UserStartedSpeakingFrame(), DOWN)
            assert all(o.cancelled for o in owners)
            assert not filler.armed
        assert not filler_audio(other)[0].owner.cancelled
    finally:
        await filler.cancel()
        await other.cancel()


async def test_interrupted_tts_does_not_leave_next_turn_deferred_forever():
    filler = make_filler(delay_ms=10)
    await filler.process_frame(BotStartedSpeakingFrame(), UP)
    await filler.process_frame(InterruptionFrame(), DOWN)
    await filler.arm(turn_id=2, gender="male")
    try:
        await wait(0.05)
        assert filler_audio(filler)
    finally:
        await filler.cancel()


async def test_rearm_retires_completed_owner_before_new_turn():
    filler = make_filler(delay_ms=10, library=_ShortLibrary(clip_ms=40))
    await filler.arm(turn_id=1, gender="male")
    await wait(0.08)
    old = filler_audio(filler)[0].owner
    await filler.arm(turn_id=1, gender="male")  # same integer still gets a new token
    try:
        assert old.cancelled
        assert filler._armed.owner.token != old.token
    finally:
        await filler.cancel()


async def test_interruption_during_producer_cancellation_still_cancels_response_task():
    filler = make_filler(delay_ms=10)
    cancelling = asyncio.Event()

    async def producer():
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await asyncio.Event().wait()

    filler._task = asyncio.create_task(producer())
    await asyncio.sleep(0)
    handoff = asyncio.create_task(filler.cancel("tts_audio"))
    await cancelling.wait()
    handoff.cancel()  # InterruptionFrame cancels the in-progress handoff
    with pytest.raises(asyncio.CancelledError):
        await handoff


async def test_retirement_before_cut_is_not_reported_as_natural_completion():
    filler = make_filler(delay_ms=10, library=_ShortLibrary(clip_ms=600))
    await filler.arm(turn_id=1, gender="male")
    await wait(0.04)
    owner = filler_audio(filler)[0].owner
    owner.cancel()  # readiness at queue ingress, before the handler runs
    before = len(filler_audio(filler))
    await wait(0.05)
    assert len(filler_audio(filler)) == before
    assert not filler._recorder.data("latency_filler_completed")
    await filler.process_frame(TTSAudioRawFrame(audio=b"\x10\x10" * 320,
                                               sample_rate=16000, num_channels=1), DOWN)
    assert filler._recorder.data("latency_filler_cut")[-1]["reason"] == "tts_audio"
