"""Sarvam realtime STT behind ``SARVAM_REALTIME_STT`` (default OFF).

Covers: the flag defaults, model mapping onto the realtime endpoint, the
``keyterms`` handshake parameter (saaras:v4 only — the realtime endpoint
closes the socket with code 4000 otherwise, probed 2026-10-06), endpointing /
stream_type normalisation, the call-event evidence, and that the legacy
streaming adapter stays the default.
"""

from __future__ import annotations

import json

import pytest
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

from shared.bot_config import ResolvedBotConfig
from shared.config import Settings, get_settings
from voice_runtime import sarvam_realtime_stt as rt
from voice_runtime import sarvam_stt
from voice_runtime.brain import interim_is_stability_reference
from voice_runtime.pipeline import build_stt_service


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def add_event(self, kind: str, **data) -> None:
        self.events.append((kind, data))

    def of(self, kind: str) -> list[dict]:
        return [data for k, data in self.events if k == kind]


def _config(model: str, settings: dict | None = None, *, language: str = "") -> ResolvedBotConfig:
    return ResolvedBotConfig(
        tenant_id="t", bot_id="b", bot_name="Test", version="1", published=True,
        language="hi-IN", languages=["hi-IN", "en-IN"],
        stt={
            "provider": "sarvam", "model": model, "language": language,
            "api_key_reference": "env:TEST_SARVAM_API_KEY",
            "settings": settings or {},
        },
    )


@pytest.fixture
def realtime_on(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
    settings = get_settings()
    monkeypatch.setattr(settings, "sarvam_realtime_stt", True)
    monkeypatch.setattr(settings, "sarvam_realtime_endpointing", "manual")
    monkeypatch.setattr(settings, "sarvam_realtime_stream_type", "balanced")
    return settings


class TestFlag:
    def test_defaults_are_off_manual_balanced(self):
        fresh = Settings(_env_file=None)
        assert fresh.sarvam_realtime_stt is False
        assert fresh.sarvam_realtime_endpointing == "manual"
        assert fresh.sarvam_realtime_stream_type == "balanced"

    async def test_flag_off_keeps_the_streaming_adapter(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
        monkeypatch.setattr(get_settings(), "sarvam_realtime_stt", False)
        service = build_stt_service(_config("saaras:v4", {"keyterms": ["Zepto"]}))
        try:
            assert isinstance(service, sarvam_stt.EndpointedSarvamSTTService)
            assert not isinstance(service, rt.RealtimeSarvamSTTService)
        finally:
            await service.cleanup()


class TestModelMapping:
    @pytest.mark.parametrize("configured, expected", [
        ("saaras:v4", "saaras:v4"),
        ("saaras:v3", "saaras:v3-realtime"),
        ("saaras:v3-realtime", "saaras:v3-realtime"),
        (" SAARAS:V4 ", "saaras:v4"),
        ("saarika:v2.5", None),
        ("saaras:v4-realtime", None),  # the endpoint rejects this spelling
        ("", None),
        (None, None),
    ])
    def test_realtime_model_for(self, configured, expected):
        assert rt.realtime_model_for(configured) == expected

    def test_normalisers_fall_back_quietly(self):
        assert rt.normalize_endpointing("vad") == "vad"
        assert rt.normalize_endpointing(" Manual ") == "manual"
        assert rt.normalize_endpointing("server") == "manual"
        assert rt.normalize_endpointing(None) == "manual"
        assert rt.normalize_stream_type("fast") == "fast"
        assert rt.normalize_stream_type("simulated") == "balanced"
        assert rt.normalize_stream_type("") == "balanced"


class TestBuild:
    async def test_v4_realtime_sends_keyterms_on_the_query_string(self, realtime_on):
        recorder = _Recorder()
        service = build_stt_service(
            _config("saaras:v4", {"mode": "transcribe", "keyterms": [" Zepto", "New Delhi", "Zepto", ""]}),
            recorder=recorder,
        )
        try:
            assert isinstance(service, rt.RealtimeSarvamSTTService)
            assert service._settings.model == "saaras:v4"
            assert service.endpointing == "manual"
            assert service.stream_type == "balanced"
            assert service.keyterms == ["Zepto", "New Delhi"]
            params = service._query_params()
            assert json.loads(params["keyterms"]) == ["Zepto", "New Delhi"]
            assert params["model"] == "saaras:v4"
            assert params["endpointing"] == "manual"
            assert params["stream_type"] == "balanced"
            assert params["encoding"] == "linear16"
            assert params["mode"] == "transcribe"
            # Two configured languages → auto-detect, never pipecat's en-IN default.
            assert params["language_code"] == "auto"
            assert "keyterms" in service._build_ws_url()
            [event] = recorder.of("stt_keyterms")
            assert event["sent"] is True and event["count"] == 2
            [rt_event] = recorder.of("stt_realtime")
            assert rt_event == {
                "model": "saaras:v4", "configured_model": "saaras:v4",
                "endpointing": "manual", "stream_type": "balanced",
            }
        finally:
            await service.cleanup()

    async def test_pinned_language_reaches_the_query_string(self, realtime_on):
        service = build_stt_service(_config("saaras:v4", language="hi-IN"))
        try:
            assert service._query_params()["language_code"] == "hi-IN"
        finally:
            await service.cleanup()

    async def test_v3_maps_to_v3_realtime_and_never_sends_keyterms(self, realtime_on):
        recorder = _Recorder()
        service = build_stt_service(
            _config("saaras:v3", {"keyterms": ["Zepto"]}), recorder=recorder,
        )
        try:
            assert isinstance(service, rt.RealtimeSarvamSTTService)
            assert service._settings.model == "saaras:v3-realtime"
            assert service.keyterms == []
            assert "keyterms" not in service._query_params()
            [event] = recorder.of("stt_keyterms")
            assert event["sent"] is False and event["reason"] == "unsupported_model"
            assert recorder.of("stt_realtime")[0]["configured_model"] == "saaras:v3"
        finally:
            await service.cleanup()

    async def test_adapter_itself_refuses_keyterms_for_v3_realtime(self, caplog):
        # Last gate before the wire: a direct constructor with the wrong model
        # must not produce the parameter the endpoint would reject.
        service = rt.RealtimeSarvamSTTService(
            api_key="k",
            settings=rt.RealtimeSarvamSTTService.Settings(model="saaras:v3-realtime", language_code="auto"),
            keyterms=["Zepto"],
        )
        try:
            assert service.keyterms == []
            assert "keyterms" not in service._query_params()
        finally:
            await service.cleanup()

    async def test_vad_endpointing_and_fast_cadence_from_settings(self, realtime_on, monkeypatch):
        monkeypatch.setattr(realtime_on, "sarvam_realtime_endpointing", "VAD")
        monkeypatch.setattr(realtime_on, "sarvam_realtime_stream_type", "fast")
        service = build_stt_service(_config("saaras:v4"))
        try:
            assert service.endpointing == "vad"
            assert service.stream_type == "fast"
            params = service._query_params()
            assert params["endpointing"] == "vad" and params["stream_type"] == "fast"
            # Server-side boundaries → the user aggregator must defer to them.
            frame = service.service_metadata_frame()
            assert isinstance(frame.user_turn_strategies, ExternalUserTurnStrategies)
        finally:
            await service.cleanup()

    async def test_manual_endpointing_leaves_our_turn_strategies_in_place(self, realtime_on):
        service = build_stt_service(_config("saaras:v4"))
        try:
            assert service.service_metadata_frame().user_turn_strategies is None
        finally:
            await service.cleanup()

    async def test_unserved_model_falls_back_to_the_streaming_adapter(self, realtime_on, monkeypatch):
        # Pipecat 1.12 dropped saarika from the streaming table too, so use a
        # realtime-only spelling that the legacy adapter can still construct.
        monkeypatch.setattr(rt, "REALTIME_MODEL_FOR", {})
        service = build_stt_service(_config("saaras:v4", {"keyterms": ["Zepto"]}))
        try:
            assert isinstance(service, sarvam_stt.EndpointedSarvamSTTService)
            assert service.keyterms == ["Zepto"]
        finally:
            await service.cleanup()


def _capture_sends(service) -> list[dict]:
    sent: list[dict] = []

    async def _send_json(payload):
        sent.append(payload)
        return True

    service._send_json = _send_json  # type: ignore[method-assign]
    return sent


class TestAlwaysOpenUtterance:
    async def test_session_begin_opens_the_first_utterance(self, realtime_on):
        service = build_stt_service(_config("saaras:v4"))
        sent = _capture_sends(service)
        try:
            await service._handle_message({"event": "session.begin", "request_id": "r"})
            assert sent == [{"event": "speech_start"}]
            assert service._utterance_open is True
        finally:
            await service.cleanup()

    async def test_vad_stop_ends_then_reopens(self, realtime_on):
        service = build_stt_service(_config("saaras:v4"))
        sent = _capture_sends(service)

        async def _flush():
            sent.append({"event": "<flush>"})

        service._flush_audio_buffer = _flush  # type: ignore[method-assign]
        try:
            await service._handle_message({"event": "session.begin"})
            await service.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            assert sent == [
                {"event": "speech_start"},
                {"event": "<flush>"}, {"event": "speech_end"},   # upstream boundary
                {"event": "speech_start"},                       # immediately re-armed
            ]
            assert service._reopen_count == 2
        finally:
            await service.cleanup()

    async def test_vad_endpointing_never_sends_client_boundaries(self, realtime_on, monkeypatch):
        monkeypatch.setattr(realtime_on, "sarvam_realtime_endpointing", "vad")
        service = build_stt_service(_config("saaras:v4"))
        sent = _capture_sends(service)
        try:
            await service._handle_message({"event": "session.begin"})
            await service.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            assert sent == []
        finally:
            await service.cleanup()

    async def test_partials_are_tagged_display_only(self, realtime_on):
        service = build_stt_service(_config("saaras:v4"))
        pushed: list = []

        async def _push(self_, frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

        monkeypatch_target = type(service).__mro__[1]  # SarvamRealtimeSTTService
        original = monkeypatch_target.push_frame
        try:
            monkeypatch_target.push_frame = _push  # type: ignore[method-assign]
            await service.push_frame(InterimTranscriptionFrame("Zepto se", "u", "now"))
            await service.push_frame(TranscriptionFrame("ज़ेप्टो से", "u", "now"))
            interim, final = pushed
            assert interim_is_stability_reference(interim) is False
            assert interim_is_stability_reference(final) is True
        finally:
            monkeypatch_target.push_frame = original  # type: ignore[method-assign]
            await service.cleanup()


class TestInterimsOnPipecat113:
    """Phase 2: the realtime socket on pipecat 1.13 — partials reach the
    pipeline as display-only interims, finals stay ``finalized``, and the
    language setting no longer goes through the deprecated ``language_code``."""

    async def _run_messages(self, service, messages):
        pushed: list = []

        async def _push(self_, frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

        parent = type(service).__mro__[1]  # SarvamRealtimeSTTService
        original = parent.push_frame
        try:
            parent.push_frame = _push  # type: ignore[method-assign]
            for message in messages:
                await service._handle_message(message)
        finally:
            parent.push_frame = original  # type: ignore[method-assign]
            await service.cleanup()
        return pushed

    async def test_partials_are_forwarded_and_finals_stay_finalized(self, realtime_on):
        service = build_stt_service(_config("saaras:v4"))
        pushed = await self._run_messages(service, [
            {"event": "transcript.partial", "text": "Zepto se", "language": "hi-IN"},
            {"event": "transcript.partial", "text": "Zepto se mera", "language": "hi-IN"},
            {"event": "transcript.partial", "text": ""},                      # blank: dropped
            {"event": "transcript.final", "text": "Zepto se mera order", "language": "hi-IN",
             "request_id": "r1"},
        ])
        interims = [f for f in pushed if isinstance(f, InterimTranscriptionFrame)]
        finals = [f for f in pushed if isinstance(f, TranscriptionFrame)]
        assert [f.text for f in interims] == ["Zepto se", "Zepto se mera"]
        assert all(interim_is_stability_reference(f) is False for f in interims)
        assert [f.text for f in finals] == ["Zepto se mera order"]
        assert finals[0].finalized is True
        assert interim_is_stability_reference(finals[0]) is True

    async def test_language_setting_without_the_deprecated_language_code(self, realtime_on, recwarn):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            auto = build_stt_service(_config("saaras:v4"))
            pinned = build_stt_service(_config("saaras:v4", language="hi-IN"))
        try:
            assert auto._query_params()["language_code"] == "auto"
            assert pinned._query_params()["language_code"] == "hi-IN"
            assert "keyterms" not in auto._query_params()
        finally:
            await auto.cleanup()
            await pinned.cleanup()

    async def test_keyterms_ride_the_realtime_handshake_when_on(self, realtime_on):
        service = build_stt_service(_config("saaras:v4", {"keyterms": ["Zepto", "MDND"]}))
        try:
            assert json.loads(service._query_params()["keyterms"]) == ["Zepto", "MDND"]
            assert service.keyterms == ["Zepto", "MDND"]
        finally:
            await service.cleanup()


class TestEvidence:
    async def test_session_events_are_recorded(self, realtime_on):
        recorder = _Recorder()
        service = build_stt_service(_config("saaras:v4", {"keyterms": ["Zepto"]}), recorder=recorder)
        _capture_sends(service)
        try:
            await service._handle_message({"event": "session.begin", "request_id": "req-1"})
            await service._handle_message({"event": "config.updated"})
            await service._handle_message({"event": "session.end", "audio_duration_s": 2.5})
            [begin] = recorder.of("stt_realtime_session")
            assert begin == {
                "request_id": "req-1", "model": "saaras:v4", "endpointing": "manual",
                "stream_type": "balanced", "keyterms": 1, "always_open": True,
            }
            assert recorder.of("stt_realtime_session_end") == [{"audio_duration_s": 2.5}]
        finally:
            await service.cleanup()

    async def test_error_events_are_recorded_and_still_reported(self, realtime_on):
        recorder = _Recorder()
        service = build_stt_service(_config("saaras:v4"), recorder=recorder)
        pushed: list[str] = []

        async def _push_error(error_msg="", **kwargs):
            pushed.append(error_msg)

        service.push_error = _push_error  # type: ignore[method-assign]
        try:
            await service._handle_message(
                {"event": "error", "code": "bad_audio", "is_fatal": True, "message": "nope"}
            )
            assert recorder.of("stt_realtime_error") == [
                {"code": "bad_audio", "fatal": True, "message": "nope"}
            ]
            assert pushed and "Sarvam realtime STT error" in pushed[0]
        finally:
            await service.cleanup()
