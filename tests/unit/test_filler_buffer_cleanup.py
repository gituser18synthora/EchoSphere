"""Owner-selective cleanup at the real Pipecat transport buffering boundary.

Sender tests freeze playback so queued PCM can be inspected sample for sample.
They deliberately retain unrelated audio and owners with equal turn numbers.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import EndFrame, InterruptionFrame, OutputAudioRawFrame, TTSAudioRawFrame
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.utils.frame_queue import FrameQueue

from voice_runtime.filler_transport import FillerOutputTransportMixin
from voice_runtime.frames import FillerAudioOwner, FillerAudioRawFrame


class BufferedOutput(FillerOutputTransportMixin, BaseOutputTransport):
    pass


def pcm(value, ms=20, rate=16000):
    return value.to_bytes(2, "little", signed=True) * (rate * ms // 1000)


def owned(owner, *, value=7000, ms=20, rate=16000):
    return FillerAudioRawFrame(
        audio=pcm(value, ms, rate), sample_rate=rate, num_channels=1,
        owner=owner,
    )


def response(*, value=12000, ms=40):
    return TTSAudioRawFrame(audio=pcm(value, ms), sample_rate=16000, num_channels=1)


def make_sender(chunk_ms):
    params = TransportParams(
        audio_out_enabled=True, audio_out_sample_rate=16000,
        audio_out_10ms_chunks=chunk_ms // 10, audio_out_end_silence_secs=0,
    )
    transport = BufferedOutput(params)
    sender = transport.MediaSender(
        transport, destination=None, sample_rate=16000,
        audio_chunk_size=len(pcm(0, chunk_ms)), params=params,
    )
    sender._audio_queue = FrameQueue()
    transport._media_senders[None] = sender
    return transport, sender


def queued(sender):
    result = []
    while not sender._audio_queue.empty():
        result.append(sender._audio_queue.get_nowait())
        sender._audio_queue.task_done()
    return result


@pytest.mark.parametrize("chunk_ms", [20, 40])
async def test_clear_discards_owned_queued_pcm_and_preserves_reply_and_other_owner(chunk_ms):
    _, sender = make_sender(chunk_ms)
    cancelled = FillerAudioOwner(turn_id=1)
    current = FillerAudioOwner(turn_id=1)
    await sender.handle_audio_frame(owned(cancelled, ms=80))
    await sender.handle_audio_frame(response(ms=80))
    unrelated = OutputAudioRawFrame(audio=pcm(3000, 80), sample_rate=16000, num_channels=1)
    await sender.handle_audio_frame(unrelated)
    await sender.handle_audio_frame(owned(current, value=5000, ms=80))
    cancelled.cancel()
    await sender.clear_filler(cancelled)
    frames = queued(sender)
    assert b"".join(frame.audio for frame in frames) == pcm(12000, 80) + pcm(3000, 80) + pcm(5000, 80)
    assert all(not isinstance(frame, FillerAudioRawFrame) or frame.owner is current for frame in frames)
    assert not current.cancelled


async def test_partial_filler_cannot_be_prepended_to_first_real_tts_chunk():
    _, sender = make_sender(40)
    owner = FillerAudioOwner(turn_id=3)
    await sender.handle_audio_frame(owned(owner, ms=20))
    owner.cancel()
    await sender.clear_filler(owner)
    await sender.handle_audio_frame(response(ms=40))
    frames = queued(sender)
    assert len(frames) == 1
    assert isinstance(frames[0], TTSAudioRawFrame)
    assert frames[0].audio == pcm(12000, 40)


async def test_partial_unrelated_audio_survives_owner_cleanup():
    _, sender = make_sender(40)
    owner = FillerAudioOwner(turn_id=3)
    await sender.handle_audio_frame(OutputAudioRawFrame(
        audio=pcm(3000, 20), sample_rate=16000, num_channels=1,
    ))
    await sender.handle_audio_frame(owned(owner, ms=20))
    owner.cancel()
    await sender.clear_filler(owner)
    await sender.handle_audio_frame(response(ms=20))
    assert b"".join(frame.audio for frame in queued(sender)) == pcm(3000, 20) + pcm(12000, 20)


@pytest.mark.parametrize("chunk_ms", [20, 40])
async def test_retired_frame_arriving_after_clear_is_rejected(chunk_ms):
    _, sender = make_sender(chunk_ms)
    owner = FillerAudioOwner(turn_id=4)
    owner.cancel()
    await sender.clear_filler(owner)
    await sender.handle_audio_frame(owned(owner, ms=80))
    await sender.handle_audio_frame(response(ms=80))
    assert b"".join(frame.audio for frame in queued(sender)) == pcm(12000, 80)


@pytest.mark.parametrize("chunk_ms", [20, 40])
async def test_six_turns_cleanup_never_leaks_previous_owner(chunk_ms):
    _, sender = make_sender(chunk_ms)
    previous = None
    for turn in range(1, 7):
        owner = FillerAudioOwner(turn_id=turn)
        if previous:
            await sender.handle_audio_frame(owned(previous, ms=80))
        await sender.handle_audio_frame(owned(owner, ms=60))
        owner.cancel()
        await sender.clear_filler(owner)
        await sender.handle_audio_frame(response(value=12000 + turn, ms=80))
        frames = queued(sender)
        assert b"".join(frame.audio for frame in frames) == pcm(12000 + turn, 80)
        assert all(isinstance(frame, TTSAudioRawFrame) for frame in frames)
        previous = owner


async def test_cancelling_one_call_does_not_touch_same_turn_in_other_call():
    _, call_a = make_sender(40)
    _, call_b = make_sender(40)
    owner_a = FillerAudioOwner(turn_id=1)
    owner_b = FillerAudioOwner(turn_id=1)
    await call_a.handle_audio_frame(owned(owner_a, ms=80))
    await call_b.handle_audio_frame(owned(owner_b, value=5000, ms=80))
    owner_a.cancel()
    await call_a.clear_filler(owner_a)
    assert queued(call_a) == []
    frames = queued(call_b)
    assert b"".join(frame.audio for frame in frames) == pcm(5000, 80)
    assert all(frame.owner is owner_b for frame in frames)
    assert not owner_b.cancelled


async def test_resampling_preserves_filler_identity_and_cannot_taint_reply():
    _, sender = make_sender(40)
    owner = FillerAudioOwner(turn_id=8)
    await sender.handle_audio_frame(owned(owner, ms=60, rate=48000))
    owner.cancel()
    await sender.clear_filler(owner)
    await sender.handle_audio_frame(response(ms=80))
    frames = queued(sender)
    assert b"".join(frame.audio for frame in frames) == pcm(12000, 80)
    assert all(isinstance(frame, TTSAudioRawFrame) for frame in frames)


async def test_cleanup_still_occurs_after_producer_has_finished():
    from voice_runtime.frames import FillerClearFrame
    from tests.unit.test_latency_filler import make_filler, _ShortLibrary, wait, DOWN
    filler = make_filler(delay_ms=10, library=_ShortLibrary(clip_ms=40))
    await filler.arm(turn_id=7, gender="male")
    await wait(0.08)
    assert filler._task is None and not filler.armed
    sent = [frame for frame, _ in filler.pushed if isinstance(frame, FillerAudioRawFrame)]
    assert sent and not sent[0].owner.cancelled
    reply = response()
    await filler.process_frame(reply, DOWN)
    clears = [frame for frame, _ in filler.pushed if isinstance(frame, FillerClearFrame)]
    assert len(clears) == 1 and clears[0].owner is sent[0].owner
    assert sent[0].owner.cancelled and filler.pushed[-1][0] is reply


@pytest.mark.parametrize("uninterruptible", [False, True])
async def test_interruption_clears_partial_pcm_before_bot_speaking(monkeypatch, uninterruptible):
    _, sender = make_sender(40)
    # Freeze task creation, not buffering or the actual interruption handler.
    for name in ("_cancel_clock_task", "_cancel_video_task", "_cancel_audio_task"):
        monkeypatch.setattr(sender, name, AsyncMock())
    for name in ("_create_clock_task", "_create_video_task", "_create_audio_task"):
        monkeypatch.setattr(sender, name, lambda: None)
    await sender.handle_audio_frame(response(value=3000, ms=10))
    end = EndFrame()
    if uninterruptible:
        sender._audio_queue.put_nowait(end)
    assert not sender._bot_speaking and sender._audio_buffer
    await sender.handle_interruptions(InterruptionFrame())
    assert sender._audio_buffer == b""
    await sender.handle_audio_frame(response(ms=40))
    frames = queued(sender)
    if uninterruptible:
        assert frames.pop(0) is end
    assert b"".join(frame.audio for frame in frames) == pcm(12000, 40)
