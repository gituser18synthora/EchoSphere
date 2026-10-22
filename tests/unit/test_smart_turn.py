"""Smart Turn end-of-turn detection: the gate (tenant mode, kill switch,
model-card languages), the shared-session analyzer, the endpoint_by /
probability bookkeeping and the pipeline's stop-strategy list."""
import asyncio

import numpy as np
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.frames.frames import TranscriptionFrame, UserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
from pipecat.metrics.metrics import TurnMetricsData
from pipecat.processors.frame_processor import FrameDirection

import voice_runtime.smart_turn as st
from shared.bot_config import ResolvedBotConfig
from shared.turn_detection import TURN_DETECTION_BOUNDS, TURN_DETECTION_DEFAULTS
from voice_runtime.pipeline import build_user_turn_stop_strategies
from voice_runtime.turn_metrics import TurnLatencyTracker
from voice_runtime.turn_stop import FinalBoundedTurnStopStrategy


# ── gate ─────────────────────────────────────────────────────────────────────

class TestGate:
    def test_mode_parsing(self):
        assert st.smart_turn_mode_from_setting(0) == st.MODE_OFF
        assert st.smart_turn_mode_from_setting(1.0) == st.MODE_AUTO
        assert st.smart_turn_mode_from_setting(2) == st.MODE_ON
        assert st.smart_turn_mode_from_setting("off") == st.MODE_OFF
        assert st.smart_turn_mode_from_setting(" ON ") == st.MODE_ON
        assert st.smart_turn_mode_from_setting("auto") == st.MODE_AUTO
        for junk in (None, "sometimes", 7, True, -1):
            assert st.smart_turn_mode_from_setting(junk) == st.MODE_AUTO, junk

    def test_model_card_languages(self):
        for locale in ("hi-IN", "en-IN", "en-US", "mr-IN", "bn-IN", "hi"):
            assert st.language_supported(locale), locale
        for locale in ("ta-IN", "ml-IN", "te-IN", "kn-IN", "gu-IN", "pa-IN", "", None):
            assert not st.language_supported(locale), locale
        assert len(st.SMART_TURN_LANGUAGES) == 23

    def test_decision_matrix(self):
        assert st.smart_turn_decision(st.MODE_AUTO, "hi-IN", enabled=True) == (True, "auto_supported_language")
        assert st.smart_turn_decision(st.MODE_AUTO, "ta-IN", enabled=True) == (False, "auto_unsupported_language")
        assert st.smart_turn_decision(st.MODE_AUTO, "ml-IN", enabled=True) == (False, "auto_unsupported_language")
        assert st.smart_turn_decision(st.MODE_ON, "ta-IN", enabled=True) == (True, "tenant_on")
        assert st.smart_turn_decision(st.MODE_OFF, "hi-IN", enabled=True) == (False, "tenant_off")
        assert st.smart_turn_decision(st.MODE_ON, "hi-IN", enabled=False) == (False, "kill_switch")

    def test_tenant_field_defaults_to_auto(self):
        for transport in ("browser", "telephony"):
            assert TURN_DETECTION_DEFAULTS[transport]["smart_turn"] == 1.0
        assert TURN_DETECTION_BOUNDS["smart_turn"] == (0.0, 2.0)


# ── analyzer ─────────────────────────────────────────────────────────────────

class TestSharedAnalyzer:
    def test_instances_share_one_onnx_session(self):
        a = st.SharedSmartTurnAnalyzer(sample_rate=16000)
        b = st.SharedSmartTurnAnalyzer(sample_rate=8000)
        assert a._session is b._session is st.shared_session()

    async def test_analysis_runs_off_the_loop_and_reports_a_probability(self):
        analyzer = st.SharedSmartTurnAnalyzer(params=st.SmartTurnParams(stop_secs=1.0))
        analyzer.set_sample_rate(16000)   # the stop strategy does this at setup()
        rng = np.random.default_rng(7)
        speech = (rng.standard_normal(16000) * 3000).astype(np.int16).tobytes()  # 1 s "speech"
        silence = np.zeros(1600, dtype=np.int16).tobytes()                       # 100 ms
        state = EndOfTurnState.INCOMPLETE
        for i in range(0, len(speech), 640):
            state = analyzer.append_audio(speech[i:i + 640], True)
        assert state == EndOfTurnState.INCOMPLETE
        for _ in range(3):
            state = analyzer.append_audio(silence, False)
        assert state == EndOfTurnState.INCOMPLETE          # under the 1.0 s silence cut-off
        state, metrics = await analyzer.analyze_end_of_turn()
        assert isinstance(metrics, TurnMetricsData)
        assert 0.0 <= metrics.probability <= 1.0
        assert metrics.is_complete == (metrics.probability > 0.5)
        assert state == (EndOfTurnState.COMPLETE if metrics.is_complete else EndOfTurnState.INCOMPLETE)
        assert analyzer._executor is not None             # inference ran in the analyzer's thread pool
        await analyzer.cleanup()

    def test_silence_cut_off_equals_the_pause_window(self):
        strategy = st.build_smart_turn_strategy(
            mode=st.MODE_ON, language="ta-IN", enabled=True, user_speech_timeout=0.9,
            wait_for_transcript=True)
        assert strategy is not None
        assert strategy._turn_analyzer.params.stop_secs == 0.9
        assert strategy.wait_for_transcript is True


# ── endpoint bookkeeping ─────────────────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.events = []

    def add_event(self, kind, **data):
        self.events.append((kind, data))


class TestEndpointBookkeeping:
    def test_tracker_first_decision_wins_and_resets_per_turn(self):
        tracker = TurnLatencyTracker(session_id="s")
        tracker.mark_speech_started()
        tracker.mark_endpoint_by("smart_turn")
        tracker.mark_endpoint_by("pause_timeout")
        tracker.note_smart_turn(probability=0.91, is_complete=True, inference_ms=42.0)
        record = tracker.structured()
        assert record["endpoint_by"] == "smart_turn"
        assert record["smart_turn_probability"] == 0.91 and record["smart_turn_complete"] is True
        assert record["smart_turn_inference_ms"] == 42.0
        tracker.mark_speech_started()
        assert tracker.structured()["endpoint_by"] == "unknown"
        assert tracker.structured()["smart_turn_probability"] is None

    async def test_smart_turn_strategy_reports_prediction_and_endpoint(self, monkeypatch):
        tracker = TurnLatencyTracker(session_id="s")
        strategy = st.build_smart_turn_strategy(
            mode=st.MODE_ON, language="hi-IN", enabled=True, user_speech_timeout=0.7,
            wait_for_transcript=True, tracker=tracker)
        pushed = []

        async def _push(frame, direction=None):
            pushed.append(frame)

        triggered = []

        async def _trigger(self, **kwargs):
            triggered.append(kwargs)

        monkeypatch.setattr(strategy, "push_frame", _push)
        monkeypatch.setattr(st.TurnAnalyzerUserTurnStopStrategy, "trigger_user_turn_stopped", _trigger)
        await strategy._handle_prediction_result(TurnMetricsData(
            processor="BaseSmartTurn", is_complete=True, probability=0.83, e2e_processing_time_ms=31.0))
        assert tracker.smart_turn_probability == 0.83 and tracker.smart_turn_complete is True
        assert pushed and pushed[0].__class__.__name__ == "MetricsFrame"
        await strategy.trigger_user_turn_stopped()
        assert tracker.endpoint_by == "smart_turn" and triggered == [{}]

    async def test_pause_window_strategy_reports_itself(self, monkeypatch):
        tracker = TurnLatencyTracker(session_id="s")
        strategy = FinalBoundedTurnStopStrategy(user_speech_timeout=0.5, wait_for_transcript=True,
                                                tracker=tracker)

        async def _trigger(self, **kwargs):
            pass

        monkeypatch.setattr(FinalBoundedTurnStopStrategy.__mro__[1], "trigger_user_turn_stopped", _trigger)
        await strategy.trigger_user_turn_stopped()
        assert tracker.endpoint_by == "pause_timeout"

    def test_gate_event_is_recorded_either_way(self):
        recorder = _Recorder()
        assert st.build_smart_turn_strategy(
            mode=st.MODE_AUTO, language="ml-IN", enabled=True, user_speech_timeout=0.7,
            wait_for_transcript=True, recorder=recorder) is None
        kind, data = recorder.events[-1]
        assert kind == "smart_turn_gate" and data["enabled"] is False
        assert data["reason"] == "auto_unsupported_language" and data["mode"] == "auto"


# ── pipeline stop-strategy list ──────────────────────────────────────────────

def _config(language="hi-IN"):
    return ResolvedBotConfig(
        tenant_id="tn", bot_id="bot", bot_name="T", version="v1", published=True,
        language=language, languages=[language], stt={"provider": "sarvam"},
        system_prompt="x",
    )


class TestPipelineStopStrategies:
    def test_auto_adds_smart_turn_before_the_pause_window_for_hindi(self):
        turn = dict(TURN_DETECTION_DEFAULTS["telephony"])
        recorder = _Recorder()
        stops = build_user_turn_stop_strategies(
            turn, _config("hi-IN"), wait_for_final=True, tracker=TurnLatencyTracker(session_id="s"),
            recorder=recorder, smart_turn_enabled=True)
        assert [type(s).__name__ for s in stops] == [
            "EndpointReportingSmartTurnStrategy", "FinalBoundedTurnStopStrategy"]
        assert stops[0]._turn_analyzer.params.stop_secs == turn["user_speech_timeout"]
        assert recorder.events[-1][1]["reason"] == "auto_supported_language"

    def test_auto_keeps_only_the_pause_window_for_tamil_and_malayalam(self):
        for language in ("ta-IN", "ml-IN"):
            stops = build_user_turn_stop_strategies(
                dict(TURN_DETECTION_DEFAULTS["telephony"]), _config(language), wait_for_final=True,
                smart_turn_enabled=True)
            assert [type(s).__name__ for s in stops] == ["FinalBoundedTurnStopStrategy"], language

    def test_tenant_off_and_kill_switch(self):
        turn = {**TURN_DETECTION_DEFAULTS["browser"], "smart_turn": 0.0}
        assert len(build_user_turn_stop_strategies(turn, _config(), wait_for_final=True,
                                                   smart_turn_enabled=True)) == 1
        turn = {**TURN_DETECTION_DEFAULTS["browser"], "smart_turn": 2.0}
        assert len(build_user_turn_stop_strategies(turn, _config("ta-IN"), wait_for_final=True,
                                                   smart_turn_enabled=True)) == 2
        assert len(build_user_turn_stop_strategies(turn, _config(), wait_for_final=True,
                                                   smart_turn_enabled=False)) == 1

    def test_pause_window_timer_unchanged(self):
        turn = dict(TURN_DETECTION_DEFAULTS["telephony"])
        stops = build_user_turn_stop_strategies(turn, _config("ta-IN"), wait_for_final=False,
                                                smart_turn_enabled=True)
        final = stops[-1]
        assert isinstance(final, FinalBoundedTurnStopStrategy)
        assert final._user_speech_timeout == max(0.2, turn["user_speech_timeout"] - turn["stop_secs"])
        assert final.wait_for_transcript is False


# ── the brain names the lexical shortcut ─────────────────────────────────────

class TestLexicalShortcutIsRecorded:
    async def test_complete_short_reply_during_open_turn(self):
        from tests.unit.test_brain_turn_taking import make_brain, settle, transcript

        brain = make_brain()
        handled = []

        async def _handle(text):
            handled.append(text)

        brain._handle_turn = _handle
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("हाँ"), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(brain._short_reply_endpoint + 0.15)
        await settle()
        assert handled == ["हाँ"]
        assert brain._latency.endpoint_by == "lexical_shortcut"
