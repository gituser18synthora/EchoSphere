"""Transport delivery, cancellation and transcript persistence regressions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import InterruptionFrame, TTSAudioRawFrame, TTSStoppedFrame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection

from voice_runtime.frames import TranscriptPlayoutFrame
from voice_runtime.recording import SessionRecorder, TurnRecord
from voice_runtime.transcript_playout import PlayoutLedger, TranscriptPlayoutObserver
from tests.unit.test_tts_router_flush_finals import FakeProvider, FakeRecorder, KEY, ENGINE, make_router
from voice_runtime.tts_router import _Generation


def recorder(enabled):
    config = SimpleNamespace(language="hi-IN")
    rec = SessionRecorder("vs_playout_test", config)
    rec.transcript_playout_only = enabled
    rec.add_turn(TurnRecord(role="bot", text="पहली बात। दूसरी बात।"))
    return rec


def test_default_transcript_and_generated_text_are_unchanged():
    rec = recorder(False)
    assert rec.transcript_rows()[0]["text"] == "पहली बात। दूसरी बात।"
    assert "delivery" not in rec.transcript_rows()[0]


def test_partial_sentence_is_not_guessed_from_a_time_or_character_ratio():
    rec = recorder(True)
    rec.playout_ledger.note(TranscriptPlayoutFrame(context_id="c", text="पहली बात।"))
    rec.playout_ledger.interrupt()
    rows = rec.transcript_rows()
    assert rows[0]["text"] == "पहली बात।"
    assert rows[0]["delivery"]["partial_sentence_omitted"]
    assert "दूसरी" not in str(rows)
    assert rec.transcript_rows() == rows  # stable across retry/finalize reads


def test_no_delivery_proof_means_no_invented_bot_words():
    rec = recorder(True)
    assert rec.transcript_rows()[0]["text"] == ""
    assert rec.transcript_rows()[0]["delivery"]["status"] == "unverified"


def test_late_completion_of_cancelled_context_cannot_restore_unheard_text():
    ledger = PlayoutLedger()
    ledger.note(TranscriptPlayoutFrame(context_id="c"))
    ledger.interrupt()
    ledger.note(TranscriptPlayoutFrame(context_id="c", text="Unheard words.", complete=True))
    assert ledger.for_turn("Unheard words.")[0] == ""


async def test_output_observer_requires_successfully_delivered_audio_bytes():
    ledger = PlayoutLedger()
    observer = TranscriptPlayoutObserver(ledger)
    async def push(*args): pass
    observer.push_frame = push
    down = FrameDirection.DOWNSTREAM
    await observer.process_frame(TranscriptPlayoutFrame(context_id="c"), down)
    # Provider completion alone is not proof: audio may have failed or been cut.
    await observer.process_frame(TranscriptPlayoutFrame(context_id="c", text="Words.", complete=True, audio_bytes=320), down)
    assert ledger.for_turn("Words.")[0] == ""
    await observer.process_frame(TTSAudioRawFrame(b"\x01\x00" * 160, 8000, 1, context_id="c"), down)
    await observer.process_frame(TranscriptPlayoutFrame(context_id="c", text="Words.", complete=True, audio_bytes=320), down)
    spoken, evidence = ledger.for_turn("Words.")
    assert spoken == "Words." and evidence["status"] == "transport_delivered_complete"
    assert evidence["far_end_playback_verified"] is False


@pytest.mark.parametrize("enabled", [False, True])
async def test_router_emits_delivery_boundary_only_when_opted_in(enabled):
    rec = FakeRecorder()
    rec.transcript_playout_only = enabled
    router = make_router(recorder=rec)
    provider = FakeProvider()
    state = _Generation(engine=ENGINE, provider=provider, texts=["First sentence."])
    state.audio_bytes = 320
    state.audio_chunks = 1
    router._generations["ctx"] = state
    await router._finalize_generation("ctx", failed=False)
    proof = [f for _, f in router.appended if isinstance(f, TranscriptPlayoutFrame)]
    assert bool(proof) is enabled
    if enabled:
        assert proof[0].text == "First sentence." and proof[0].audio_bytes == 320
        frames = [f for _, f in router.appended]
        assert next(i for i, f in enumerate(frames) if isinstance(f, TTSStoppedFrame)) < frames.index(proof[0])


async def test_pause_mode_commits_only_a_completed_provider_sentence():
    rec = FakeRecorder(); rec.transcript_playout_only = True
    router = make_router(recorder=rec, pause_ms=100)
    provider = FakeProvider()
    state = _Generation(engine=ENGINE, provider=provider, texts=["First sentence.", "Unheard next sentence."])
    state.seq = 1; state.active = "ctx~1"; state.active_got_audio = True; state.audio_bytes = 320
    router._generations["ctx"] = state
    await router._on_sentence_final("ctx", "ctx~1", state)
    proof = [f for _, f in router.appended if isinstance(f, TranscriptPlayoutFrame)]
    assert [f.text for f in proof] == ["First sentence."]


async def test_contextless_resampled_transport_chunks_and_filler_are_handled():
    ledger = PlayoutLedger()
    observer = TranscriptPlayoutObserver(ledger)
    async def push(*args): pass
    observer.push_frame = push
    down = FrameDirection.DOWNSTREAM
    await observer.process_frame(TranscriptPlayoutFrame(context_id="c"), down)
    proof = TranscriptPlayoutFrame(context_id="c", text="Delivered.", complete=True,
                                   audio_bytes=320, sample_rate=8000)
    # Generic output/filler cannot certify a bot sentence.
    await observer.process_frame(OutputAudioRawFrame(b"\x01\x00" * 160, 8000, 1), down)
    await observer.process_frame(proof, down)
    assert ledger.for_turn("Delivered.")[0] == ""
    # The real transport drops IDs and can resample 8 kHz to 16 kHz.
    await observer.process_frame(TTSAudioRawFrame(b"\x01\x00" * 320, 16000, 1), down)
    await observer.process_frame(proof, down)
    assert ledger.for_turn("Delivered.")[0] == "Delivered."


async def test_later_cumulative_proof_recovers_a_boundary_held_in_partial_chunk():
    ledger = PlayoutLedger()
    observer = TranscriptPlayoutObserver(ledger)
    async def push(*args): pass
    observer.push_frame = push
    down = FrameDirection.DOWNSTREAM
    await observer.process_frame(TranscriptPlayoutFrame(context_id="c"), down)
    await observer.process_frame(TranscriptPlayoutFrame(context_id="c", text="First.", audio_bytes=100), down)
    assert ledger.contexts["c"]["text"] == ""
    await observer.process_frame(TTSAudioRawFrame(b"\x01\x00" * 160, 8000, 1), down)
    await observer.process_frame(TranscriptPlayoutFrame(context_id="c", text="First. Second.",
                                                      complete=True, audio_bytes=300), down)
    assert ledger.for_turn("First. Second.")[0] == "First. Second."


async def test_cancelled_context_does_not_borrow_next_context_audio():
    ledger = PlayoutLedger()
    observer = TranscriptPlayoutObserver(ledger)
    async def push(*args): pass
    observer.push_frame = push
    # This unit drives the observer directly without Pipecat task setup;
    # cancellation queue handling is exercised in the real-worker test below.
    observer._start_interruption = AsyncMock()
    down = FrameDirection.DOWNSTREAM
    await observer.process_frame(TranscriptPlayoutFrame(context_id="old"), down)
    await observer.process_frame(InterruptionFrame(), down)
    await observer.process_frame(TranscriptPlayoutFrame(context_id="new"), down)
    await observer.process_frame(TTSAudioRawFrame(b"\x01\x00" * 160, 8000, 1), down)
    await observer.process_frame(TranscriptPlayoutFrame(context_id="old", text="Old.", complete=True, audio_bytes=320), down)
    await observer.process_frame(TranscriptPlayoutFrame(context_id="new", text="New.", complete=True, audio_bytes=320), down)
    assert ledger.for_turn("Old.")[0] == ""
    assert ledger.for_turn("New.")[0] == "New."


@pytest.mark.parametrize("cut", [False, True])
async def test_proof_through_real_transport_chunking_and_interruption(cut):
    import asyncio
    from pipecat.frames.frames import EndFrame, TTSStartedFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
    from tests.unit.test_filler_websocket_cleanup import ObservedOutput, SocketPlayback, _run_worker
    from tests.unit.test_filler_interruption_pipeline import until
    from voice_runtime.serializer import RawPCMSerializer

    ledger = PlayoutLedger()
    observer = TranscriptPlayoutObserver(ledger)
    client = SocketPlayback(browser=True)
    output = ObservedOutput(client, client, FastAPIWebsocketParams(
        audio_out_enabled=True, audio_out_sample_rate=8000,
        audio_out_10ms_chunks=4, audio_out_end_silence_secs=0,
        serializer=RawPCMSerializer(),
    ))
    worker = PipelineWorker(Pipeline([output, observer]), params=PipelineParams(
        audio_in_sample_rate=8000, audio_out_sample_rate=8000,
    ), enable_rtvi=False, idle_timeout_secs=None)

    async def feed():
        await until(lambda: bool(output._media_senders))
        await output.queue_frame(TTSStartedFrame(context_id="ctx"))
        await output.queue_frame(TranscriptPlayoutFrame(context_id="ctx"))
        # Shorter than the 40 ms transport chunk: retained until stop.
        await output.queue_frame(TTSAudioRawFrame(b"\xe0\x2e" * 100, 8000, 1, context_id="ctx"))
        await until(lambda: "ctx" in ledger.contexts)
        if cut:
            await worker.queue_frame(InterruptionFrame())
            await until(lambda: ledger.contexts["ctx"]["interrupted"])
        else:
            await output.queue_frame(TTSStoppedFrame(context_id="ctx"))
            await output.queue_frame(TranscriptPlayoutFrame(context_id="ctx", text="Delivered.",
                                    complete=True, audio_bytes=200, sample_rate=8000))
            await until(lambda: ledger.contexts["ctx"]["complete"])
        await worker.queue_frame(EndFrame())

    feeder = asyncio.create_task(feed())
    runner = asyncio.create_task(_run_worker(worker))
    try:
        await asyncio.wait_for(asyncio.gather(feeder, runner), timeout=8)
    finally:
        for task in (feeder, runner):
            if not task.done():
                task.cancel()
        await asyncio.gather(feeder, runner, return_exceptions=True)
    assert ledger.for_turn("Delivered.")[0] == ("" if cut else "Delivered.")
    assert bool(client.packets) is (not cut)
