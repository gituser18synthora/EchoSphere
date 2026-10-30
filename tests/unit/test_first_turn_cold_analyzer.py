"""The first caller turn on a cold Smart Turn analyzer waits for the pending
STT final — bounded — before the turn can end empty.

Hypothesis checked for the lost first utterances of 2026-10-22
(cv_80982e36f4f5 / cv_baeaf87927cb): the turn was NOT dispatched early by the
analyzer strategy or the pause window — the final never came (Sarvam's
first-turn speech requirement, test_sarvam_first_turn_handshake.py). These
tests pin the ordering that evidence relied on, for a first turn whose
analyzer inference is cold and slow:

- the Smart Turn strategy (``wait_for_transcript=True``) never ends the turn
  without text, however long its inference takes, and ends it the moment the
  finalized transcript lands;
- the pause-window strategy waits for the final up to the STT safety net
  (anchored at the VAD stop, not at the end of inference) and only then ends
  the turn empty (``endpoint_by="pause_timeout"``).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import TranscriptionFrame, VADUserStoppedSpeakingFrame
from pipecat.metrics.metrics import TurnMetricsData
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager

from voice_runtime.smart_turn import EndpointReportingSmartTurnStrategy
from voice_runtime.turn_metrics import TurnLatencyTracker
from voice_runtime.turn_stop import FinalBoundedTurnStopStrategy

STOP_SECS = 0.25
STT_P99 = 0.8            # SARVAM_TTFS_P99_S on the live pipeline
PAUSE_WINDOW = 0.45      # user_speech_timeout - stop_secs on telephony


class _ColdAnalyzer:
    """A turn analyzer whose first inference is slow (cold ONNX session)."""

    params = SmartTurnParams(stop_secs=0.7)

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.calls = 0

    def set_sample_rate(self, rate: int) -> None:
        pass

    def update_vad_start_secs(self, secs: float) -> None:
        pass

    def append_audio(self, audio: bytes, is_speech: bool) -> EndOfTurnState:
        return EndOfTurnState.INCOMPLETE

    async def analyze_end_of_turn(self):
        self.calls += 1
        await asyncio.sleep(self.delay_s)
        return EndOfTurnState.COMPLETE, TurnMetricsData(
            processor="BaseSmartTurn", is_complete=True, probability=0.98,
            e2e_processing_time_ms=self.delay_s * 1000.0,
        )

    def clear(self) -> None:
        pass

    async def cleanup(self) -> None:
        pass


async def _ready(strategy, monkeypatch, triggered: list[float], t0: float):
    manager = TaskManager()
    await strategy.setup(FrameProcessorSetup(
        clock=SystemClock(), task_manager=manager, audio_in_sample_rate=8000,
        audio_out_sample_rate=8000, pipeline_worker=None,
    ))

    async def _push(frame, direction=None):
        pass

    async def _trigger(self_inner, **kwargs):
        triggered.append(time.monotonic() - t0)

    monkeypatch.setattr(strategy, "push_frame", _push)
    # Patch the pipecat parent, so our subclasses' endpoint bookkeeping still runs.
    monkeypatch.setattr(type(strategy).__mro__[1], "trigger_user_turn_stopped", _trigger)
    strategy._stt_timeout = STT_P99       # normally from the STT service's metadata frame
    await strategy.handle_user_turn_started()


class TestSmartTurnStrategyOnTheFirstTurn:
    @pytest.mark.asyncio
    async def test_cold_inference_never_ends_the_turn_without_text(self, monkeypatch):
        analyzer = _ColdAnalyzer(delay_s=0.3)
        strategy = EndpointReportingSmartTurnStrategy(
            turn_analyzer=analyzer, wait_for_transcript=True, tracker=TurnLatencyTracker(session_id="s"))
        triggered: list[float] = []
        t0 = time.monotonic()
        await _ready(strategy, monkeypatch, triggered, t0)
        try:
            await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS, timestamp=time.time()))
            assert analyzer.calls == 1 and strategy._turn_complete is True   # COMPLETE after 0.3 s
            assert triggered == []                                           # ... but no text yet
            await asyncio.sleep(0.5)                                          # past the STT safety net
            assert triggered == []                                           # still no empty turn
        finally:
            await strategy.cleanup()

    @pytest.mark.asyncio
    async def test_the_pending_final_ends_the_turn_the_moment_it_lands(self, monkeypatch):
        analyzer = _ColdAnalyzer(delay_s=0.3)
        tracker = TurnLatencyTracker(session_id="s")
        strategy = EndpointReportingSmartTurnStrategy(
            turn_analyzer=analyzer, wait_for_transcript=True, tracker=tracker)
        triggered: list[float] = []
        t0 = time.monotonic()
        await _ready(strategy, monkeypatch, triggered, t0)
        try:
            await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS, timestamp=time.time()))
            await asyncio.sleep(0.1)                                          # final lands 0.4 s after the stop
            await strategy.process_frame(TranscriptionFrame("हाँ बोलिए।", "u", "t", finalized=True))
            assert len(triggered) == 1 and strategy._text == "हाँ बोलिए।"
            assert 0.35 <= triggered[0] <= 0.6
            assert tracker.smart_turn_complete is True and tracker.smart_turn_probability == 0.98
        finally:
            await strategy.cleanup()


class TestPauseWindowStrategyOnTheFirstTurn:
    @pytest.mark.asyncio
    async def test_waits_for_the_final_until_the_stt_bound_then_ends_empty(self, monkeypatch):
        tracker = TurnLatencyTracker(session_id="s")
        strategy = FinalBoundedTurnStopStrategy(
            user_speech_timeout=PAUSE_WINDOW, wait_for_transcript=True, tracker=tracker)
        triggered: list[float] = []
        t0 = time.monotonic()
        await _ready(strategy, monkeypatch, triggered, t0)
        try:
            await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS, timestamp=time.time()))
            await asyncio.sleep(PAUSE_WINDOW + 0.03)
            assert triggered == []                       # pause window over, final still awaited
            await asyncio.sleep(STT_P99 - STOP_SECS - PAUSE_WINDOW + 0.1)
            assert len(triggered) == 1                   # the bounded wait ran out: empty turn
            assert STT_P99 - STOP_SECS - 0.05 <= triggered[0] <= STT_P99 - STOP_SECS + 0.2
            assert strategy._text == "" and tracker.endpoint_by == "pause_timeout"
        finally:
            await strategy.cleanup()

    @pytest.mark.asyncio
    async def test_a_final_inside_the_pause_window_ends_the_turn_on_the_window(self, monkeypatch):
        strategy = FinalBoundedTurnStopStrategy(
            user_speech_timeout=PAUSE_WINDOW, wait_for_transcript=True, tracker=TurnLatencyTracker(session_id="s"))
        triggered: list[float] = []
        t0 = time.monotonic()
        await _ready(strategy, monkeypatch, triggered, t0)
        try:
            await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS, timestamp=time.time()))
            await asyncio.sleep(0.1)
            await strategy.process_frame(TranscriptionFrame("हाँ बोलिए।", "u", "t", finalized=True))
            assert triggered == []                       # the pause window still runs
            await asyncio.sleep(PAUSE_WINDOW)
            assert len(triggered) == 1 and strategy._text == "हाँ बोलिए।"
            assert PAUSE_WINDOW - 0.05 <= triggered[0] <= PAUSE_WINDOW + 0.2
        finally:
            await strategy.cleanup()
