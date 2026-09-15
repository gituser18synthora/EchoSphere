"""Complete voiced cues plus ordered 300 ms silence, with interruptible replies."""
import asyncio
import time

import numpy as np
import pytest
from pipecat.frames.frames import CancelFrame, InterruptionFrame, UserStartedSpeakingFrame

from tests.unit.test_latency_filler import (
    DOWN, RATE, _AcknowledgementCueStub, _CueStub, _ShortLibrary,
    filler_audio, make_filler, tts_audio, wait,
)


def smooth_filler(*, clip_ms=280, ack=False, missing=False):
    cues = _AcknowledgementCueStub(clip_ms=clip_ms) if ack else _CueStub(clip_ms=clip_ms, missing=('hmm',) if missing else ())
    return make_filler(
        delay_ms=1500, library=_ShortLibrary(clip_ms=600), cue_library=cues,
        hmm_after_ms=3500, spoken_after_ms=5000, voiced_cue_gap_ms=300,
    )


async def start_cue(filler, *, ack=False):
    await filler.arm(
        turn_id=1, gender='male', cue_after_ms=1500,
        speech_stopped_at=time.monotonic()-1.48,
        acknowledgement={'text': 'जी…', 'context': 'answer'} if ack else None,
    )
    await wait(.065)
    assert filler_audio(filler)


@pytest.mark.parametrize('ack', [False, True])
async def test_started_word_is_complete_and_silence_is_exactly_300_ms(ack):
    filler = smooth_filler(ack=ack)
    await start_cue(filler, ack=ack)
    owner = filler._armed.owner
    reply = tts_audio()
    await asyncio.wait_for(filler.process_frame(reply, DOWN), 1.5)
    pcm = b''.join(f.audio for f in filler_audio(filler))
    word_samples = int(RATE*.280)
    expected_level = 7000 if ack else 4000
    assert np.all(np.frombuffer(pcm[:word_samples*2], dtype='<i2') == expected_level)
    assert pcm[word_samples*2:] == b'\x00\x00' * int(RATE*.300)
    assert filler.pushed[-1][0] is reply
    assert not owner.cancelled, 'Normal response must not clear a buffered cue/gap tail'
    assert not filler._recorder.data('latency_filler_cut')
    assert filler._recorder.data('voiced_cue_reply_handoff')[0]['held_ms'] > 400


async def test_gap_already_elapsed_does_not_add_another_delay():
    filler = smooth_filler()
    await start_cue(filler)
    await wait(.65)
    count = len(filler_audio(filler))
    started = time.monotonic()
    reply = tts_audio()
    await filler.process_frame(reply, DOWN)
    assert time.monotonic()-started < .05
    assert len(filler_audio(filler)) == count
    assert filler.pushed[-1][0] is reply


async def test_reply_arriving_during_gap_waits_only_remaining_silence():
    filler = smooth_filler(clip_ms=200)
    await start_cue(filler)
    await wait(.25)
    started = time.monotonic()
    await filler.process_frame(tts_audio(), DOWN)
    assert .1 < time.monotonic()-started < .3


async def test_fast_reply_skips_entire_cue_and_gap():
    filler = smooth_filler()
    await filler.arm(turn_id=1, gender='male', cue_after_ms=1500)
    started = time.monotonic()
    reply = tts_audio()
    await filler.process_frame(reply, DOWN)
    assert time.monotonic()-started < .05
    assert filler_audio(filler) == []
    assert filler.pushed[-1][0] is reply


async def test_breath_still_cuts_immediately_without_gap():
    filler = smooth_filler(missing=True)
    await start_cue(filler)
    owner = filler._armed.owner
    started = time.monotonic()
    reply = tts_audio()
    await filler.process_frame(reply, DOWN)
    assert time.monotonic()-started < .05
    assert owner.cancelled
    assert filler.pushed[-1][0] is reply
    assert filler._recorder.data('latency_filler_cut')[0]['rung'] == 'breath'
    assert not filler._recorder.data('voiced_cue_reply_handoff')


@pytest.mark.parametrize('phase', ['word', 'gap'])
@pytest.mark.parametrize('event_type', [UserStartedSpeakingFrame, InterruptionFrame, CancelFrame])
async def test_caller_or_teardown_cancels_held_reply_in_word_and_gap(phase, event_type):
    filler = smooth_filler()
    await start_cue(filler)
    owner = filler._armed.owner
    reply = tts_audio()
    sending = asyncio.create_task(filler.process_frame(reply, DOWN))
    await wait(.01 if phase == 'word' else .29)
    await filler.process_frame(event_type(), DOWN)
    await asyncio.wait_for(sending, .2)
    assert owner.cancelled
    assert not any(f is reply for f, _ in filler.pushed)
    count = len(filler_audio(filler))
    await wait(.05)
    assert len(filler_audio(filler)) == count


async def test_rearm_drops_old_held_reply_without_affecting_next_turn():
    filler = smooth_filler()
    await start_cue(filler)
    old_owner = filler._armed.owner
    old_reply = tts_audio()
    sending = asyncio.create_task(filler.process_frame(old_reply, DOWN))
    await wait(.01)
    await filler.arm(turn_id=2, gender='female', cue_after_ms=1500)
    await asyncio.wait_for(sending, .2)
    assert old_owner.cancelled
    assert not any(f is old_reply for f, _ in filler.pushed)
    new_reply = tts_audio()
    await filler.process_frame(new_reply, DOWN)
    assert filler.pushed[-1][0] is new_reply


async def test_only_first_reply_packet_waits_and_packet_order_is_preserved():
    filler = smooth_filler()
    await start_cue(filler)
    first, second = tts_audio(), tts_audio()
    await filler.process_frame(first, DOWN)
    started = time.monotonic()
    await filler.process_frame(second, DOWN)
    assert time.monotonic()-started < .05
    assert [f for f, _ in filler.pushed if f is first or f is second] == [first, second]


@pytest.mark.parametrize('transport', ['browser', 'fork', 'vaani'])
@pytest.mark.parametrize('interrupt_phase', [None, 'word', 'gap'])
async def test_socket_playback_preserves_word_gap_and_interrupt_recovery(transport, interrupt_phase):
    import base64
    import json
    from pipecat.frames.frames import EndFrame, TTSAudioRawFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
    from pipecat.workers.runner import WorkerRunner
    from tests.unit.test_filler_interruption_pipeline import InterruptSocket, until
    from tests.unit.test_filler_websocket_cleanup import ObservedOutput
    from voice_runtime.latency_filler import LatencyFillerProcessor
    from voice_runtime.serializer import RawPCMSerializer
    from voice_runtime.telephony import FreeSwitchAudioForkSerializer, VaaniFrameSerializer

    class TracedSocket(InterruptSocket):
        async def send(self, payload):
            count = len(self.packets)
            await super().send(payload)
            if len(self.packets) == count:
                return
            if isinstance(payload, bytes):
                audio = payload
            else:
                m = json.loads(payload)
                encoded = (m.get('audio') or m.get('data', {}).get('audioContent')
                           or m.get('media', {}).get('payload'))
                audio = base64.b64decode(encoded)
            values = list(memoryview(audio).cast('h'))
            packet = self.packets[-1]
            packet['word_samples'] = values.count(4000)
            packet['silent_samples'] = values.count(0)
            word_indices = [i for i,v in enumerate(values) if v == 4000]
            packet['word_end'] = packet['play']+(word_indices[-1]+1)/8000 if word_indices else None

    client = TracedSocket(browser=transport == 'browser')
    serializer = {
        'browser': RawPCMSerializer,
        'fork': FreeSwitchAudioForkSerializer,
        'vaani': lambda: VaaniFrameSerializer(stream_sid='cue-gap-test'),
    }[transport]()
    output = ObservedOutput(client, client, FastAPIWebsocketParams(
        audio_out_enabled=True, audio_out_sample_rate=8000,
        audio_out_10ms_chunks=4, audio_out_end_silence_secs=0, serializer=serializer,
    ))
    filler = LatencyFillerProcessor(
        delay_ms=1500, sample_rate=8000, library=_ShortLibrary(),
        cue_library=_CueStub(clip_ms=410), hmm_after_ms=3500,
        voiced_cue_gap_ms=300, emit_flush_marker=transport != 'browser',
    )
    worker = PipelineWorker(Pipeline([filler, output]), params=PipelineParams(
        audio_in_sample_rate=8000, audio_out_sample_rate=8000,
    ), enable_rtvi=False, idle_timeout_secs=None)

    def reply():
        return TTSAudioRawFrame(audio=(12000).to_bytes(2, 'little')*2240,
                               sample_rate=8000, num_channels=1)

    async def feed():
        await until(lambda: bool(output._media_senders))
        await filler.arm(turn_id=1, gender='male', cue_after_ms=1500,
                         speech_stopped_at=time.monotonic()-1.48)
        owner = filler._armed.owner
        await until(lambda: any(p['word_samples'] for p in client.packets))
        await filler.queue_frame(reply())
        if interrupt_phase:
            await asyncio.sleep(.04 if interrupt_phase == 'word' else .46)
            await worker.queue_frame(InterruptionFrame())
            await worker.queue_frame(UserStartedSpeakingFrame())
            await until(lambda: len(client.interrupts) == 1)
            await asyncio.sleep(.2)
            assert owner.cancelled
            assert not any(p['reply'] for p in client.packets)
            # A new turn must recover without releasing the discarded reply.
            await filler.arm(turn_id=2, gender='female', cue_after_ms=1500)
            await filler.queue_frame(reply())
            await until(lambda: sum(p['reply_duration_ms'] for p in client.packets) == 280)
        else:
            await until(lambda: sum(p['reply_duration_ms'] for p in client.packets) == 280)
            assert sum(p['word_samples'] for p in client.packets) == 3280  # complete 410 ms
            # Telephony rounds the final 10 ms to a 20 ms wire packet.
            silence = sum(p['silent_samples'] for p in client.packets if not p['reply'])
            assert 2400 <= silence <= (2400 if transport == 'browser' else 2480)
            end = max(p['word_end'] for p in client.packets if p['word_end'] is not None)
            start = next(p['play'] for p in client.packets if p['reply'])
            assert .295 <= start-end < .4, (transport, start-end)
            assert not any(p['discarded_ms'] for p in client.packets)
        assert not any(p['mixed'] for p in client.packets)
        await worker.queue_frame(EndFrame())

    feeder = asyncio.create_task(feed())
    runner = asyncio.create_task(WorkerRunner(handle_sigint=False).run(worker))
    try:
        await asyncio.wait_for(asyncio.gather(feeder, runner), 8)
    finally:
        for task in (feeder, runner):
            if not task.done():
                task.cancel()
        await asyncio.gather(feeder, runner, return_exceptions=True)


@pytest.mark.parametrize('boundary', ['wait_finished', 'before_forward'])
async def test_interrupt_at_handoff_boundary_cannot_release_stale_reply(boundary):
    filler = smooth_filler(clip_ms=80)
    await start_cue(filler)
    reached, release = asyncio.Event(), asyncio.Event()
    if boundary == 'wait_finished':
        original = filler._finish_voiced_handoff
        async def finishing(voiced):
            result = await original(voiced)
            reached.set()
            await release.wait()
            return result
        filler._finish_voiced_handoff = finishing
    else:
        original = filler._cut
        async def cutting(reason, **kwargs):
            await original(reason, **kwargs)
            if reason == 'tts_audio':
                reached.set()
                await release.wait()
        filler._cut = cutting
    reply = tts_audio()
    sending = asyncio.create_task(filler.process_frame(reply, DOWN))
    await asyncio.wait_for(reached.wait(), 1)
    await filler.process_frame(UserStartedSpeakingFrame(), DOWN)
    release.set()
    await asyncio.wait_for(sending, .2)
    assert not any(f is reply for f, _ in filler.pushed)


async def test_echo_shield_ends_with_word_not_with_silent_gap():
    filler = smooth_filler(clip_ms=200)
    changes = []
    filler.cue_window_hook = changes.append
    await start_cue(filler)
    assert changes == [True]
    await wait(.2)
    assert changes == [True, False]
    assert not filler._voiced_handoff.finished.is_set()
    await filler.cancel()
    assert changes == [True, False]


async def test_clip_failure_releases_waiter_instead_of_hanging_response():
    filler = smooth_filler()
    async def fail(armed):
        await wait(.15)
        raise RuntimeError('simulated filler output failure')
    filler._stream = fail
    await filler.arm(turn_id=1, gender='male', cue_after_ms=1500,
                     speech_stopped_at=time.monotonic()-1.48)
    await wait(.06)
    reply = tts_audio()
    await asyncio.wait_for(filler.process_frame(reply, DOWN), .5)
    assert filler.pushed[-1][0] is reply
    assert filler._recorder.data('voiced_cue_reply_handoff')[0]['cue_failed']


async def test_next_pcm_ingress_during_handoff_does_not_retire_completed_cue():
    from unittest.mock import AsyncMock, patch
    from pipecat.processors.frame_processor import FrameProcessor
    filler = smooth_filler(clip_ms=80)
    await start_cue(filler)
    owner = filler._armed.owner
    first, second = tts_audio(), tts_audio()
    original = filler._cut
    async def cut_with_next_packet(reason, **kwargs):
        await original(reason, **kwargs)
        if kwargs.get('preserve') is not None:
            with patch.object(FrameProcessor, 'queue_frame', new=AsyncMock()):
                await filler.queue_frame(second, DOWN)
            assert not owner.cancelled
    filler._cut = cut_with_next_packet
    await filler.process_frame(first, DOWN)
    await filler.process_frame(second, DOWN)
    assert [f for f, _ in filler.pushed if f is first or f is second] == [first, second]
