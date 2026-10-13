"""pipecat 1.12 follow-ups: no deprecated APIs on our side of the contract.

- processors read the pipeline's sample rates from ``FrameProcessorSetup``
  (``StartFrame.audio_*_sample_rate`` are deprecated, removed in 2.0);
- the barge-in start strategy implements ``handle_user_turn_started`` rather
  than the deprecated ``reset``;
- the latency observer turns pipecat's ``LatencyBreakdown`` into the
  ``turn_latency_breakdown`` event stored on the call;
- the app runs its pipeline through ``WorkerRunner.add_workers`` + ``run()``.
"""

import re
import warnings
from pathlib import Path

import pytest
from pipecat.clocks.system_clock import SystemClock
from pipecat.observers.user_bot_latency_observer import (
    LatencyBreakdown,
    LatencyContribution,
    LatencyOwnerKind,
    MeasuredFrom,
    TTFBBreakdownMetrics,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.turns.user_start.base_user_turn_start_strategy import BaseUserTurnStartStrategy
from pipecat.utils.asyncio.task_manager import TaskManager

from voice_runtime.audio_gate import CallerAudioGate
from voice_runtime.barge_in import WordConfirmedBargeInStrategy
from voice_runtime.latency_breakdown import (
    EVENT_KIND,
    TurnLatencyBreakdownObserver,
    breakdown_event,
)
from voice_runtime.playback_duck import PlaybackDuck

ROOT = Path(__file__).resolve().parents[2]


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def add_event(self, kind: str, **data) -> None:
        self.events.append((kind, data))


def _setup(*, audio_in: int = 8000, audio_out: int = 16000) -> FrameProcessorSetup:
    import asyncio

    tm = TaskManager()
    return FrameProcessorSetup(
        clock=SystemClock(), task_manager=tm, pipeline_worker=None,
        audio_in_sample_rate=audio_in, audio_out_sample_rate=audio_out,
    )


class TestSampleRatesFromSetup:
    async def test_playback_duck_takes_output_rate_from_setup(self):
        duck = PlaybackDuck()
        assert duck._sample_rate == 0
        await duck.setup(_setup(audio_out=24000))
        assert duck._sample_rate == 24000

    async def test_caller_audio_gate_takes_input_rate_from_setup(self):
        gate = CallerAudioGate()
        await gate.setup(_setup(audio_in=8000))
        assert gate._sample_rate == 8000

    def test_no_processor_reads_deprecated_startframe_rates(self):
        pattern = re.compile(r"frame\.audio_(in|out)_sample_rate|\"audio_(in|out)_sample_rate\"")
        offenders = [
            str(p.relative_to(ROOT))
            for p in (ROOT / "voice_runtime").glob("*.py")
            if pattern.search(p.read_text())
        ]
        assert offenders == [], offenders


class TestBargeInStrategyHooks:
    def test_uses_handle_user_turn_started_not_reset(self):
        assert WordConfirmedBargeInStrategy.reset is BaseUserTurnStartStrategy.reset
        assert (
            WordConfirmedBargeInStrategy.handle_user_turn_started
            is not BaseUserTurnStartStrategy.handle_user_turn_started
        )

    def test_subclass_definition_emits_no_deprecation(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)

            class _Probe(WordConfirmedBargeInStrategy):  # noqa: D401 — definition is the test
                pass

        assert not [w for w in caught if "deprecated" in str(w.message).lower()]


class TestLatencyBreakdownEvent:
    def _breakdown(self) -> LatencyBreakdown:
        return LatencyBreakdown(
            ttfb=[TTFBBreakdownMetrics(processor="StreamingTTSRouter#0", model="bulbul:v3",
                                       start_time=100.0, duration_secs=0.31)],
            contributions=[
                LatencyContribution(key="vad_silence", label="VAD silence", owner="config:stop_secs",
                                    owner_kind=LatencyOwnerKind.SETTING, start_time=100.0, duration_secs=0.2),
                LatencyContribution(key="llm", label="LLM", owner="OpenAILLMService#0",
                                    owner_kind=LatencyOwnerKind.SERVICE, start_time=100.2, duration_secs=1.4),
            ],
            measured_from=MeasuredFrom.USER_SILENCE,
            total_secs=1.6,
            user_turn_start_time=98.0,
            user_turn_secs=1.7,
        )

    def test_payload_is_json_safe_and_in_milliseconds(self):
        payload = breakdown_event(self._breakdown())
        assert payload["measured_from"] == "user_silence"
        assert payload["total_ms"] == 1600.0 and payload["user_turn_ms"] == 1700.0
        assert payload["slowest"] == "llm"
        assert [c["key"] for c in payload["contributions"]] == ["vad_silence", "llm"]
        assert payload["contributions"][1]["owner_kind"] == "service"
        assert payload["ttfb"] == [{"processor": "StreamingTTSRouter#0", "model": "bulbul:v3", "ms": 310.0}]
        import json
        json.dumps(payload)  # must serialise as-is for Mongo

    async def test_observer_records_the_event_on_the_call(self):
        recorder = _Recorder()
        observer = TurnLatencyBreakdownObserver(recorder)
        await observer._call_event_handler("on_latency_breakdown", self._breakdown())
        # pipecat runs async event handlers as tasks; let them finish.
        import asyncio
        await asyncio.gather(*(task for _name, task in list(observer._event_tasks)))
        [(kind, data)] = recorder.events
        assert kind == EVENT_KIND
        assert data["total_ms"] == 1600.0 and data["slowest"] == "llm"

    async def test_recorder_failure_never_propagates(self):
        class _Broken:
            def add_event(self, *a, **k):
                raise RuntimeError("mongo down")

        observer = TurnLatencyBreakdownObserver(_Broken())
        await observer._call_event_handler("on_latency_breakdown", self._breakdown())
        import asyncio
        await asyncio.gather(*(task for _name, task in list(observer._event_tasks)))  # no raise


class TestRunnerUsage:
    def test_app_uses_worker_runner_add_workers(self):
        src = (ROOT / "voice_runtime" / "app.py").read_text()
        assert "from pipecat.pipeline.runner import PipelineRunner" not in src
        assert "PipelineRunner(" not in src
        assert "runner.add_workers(worker)" in src and "await runner.run()" in src
        gateway = (ROOT / "voice_runtime" / "gateway.py").read_text()
        assert "from pipecat.pipeline.runner import PipelineRunner" not in gateway

    def test_pipeline_attaches_the_breakdown_observer(self):
        src = (ROOT / "voice_runtime" / "pipeline.py").read_text()
        assert "observers=[TurnLatencyBreakdownObserver(recorder)]" in src
