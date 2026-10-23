"""Fields the Phase 3 metrics read (scratchpad/phase0_metrics.py).

- ``turn_timing.tts_model``: provider/model that produced the turn's first
  audio (tts_ttfb per TTS model, mixed v3/v4-flash calls included);
- the call document's ``models``: the configured STT/TTS/LLM/decision engines.
"""

from shared.providers.tts.streaming import TTSStreamEvent
from tests.unit.test_costing_capture import make_config
from tests.unit.test_tts_router_flush_finals import KEY, FakeProvider, make_router
from voice_runtime.recording import SessionRecorder
from voice_runtime.turn_metrics import TurnLatencyTracker
from voice_runtime.tts_router import _Generation


def test_first_audio_engine_is_recorded_once_per_turn():
    tracker = TurnLatencyTracker()
    tracker.note_tts_engine("sarvam", "bulbul:v4-flash")      # no request yet: ignored
    assert tracker.structured()["tts_model"] is None
    tracker.mark_dispatched()
    tracker.mark_tts_request()
    tracker.note_tts_engine("sarvam", "bulbul:v4-flash")
    tracker.note_tts_engine("sarvam", "bulbul:v3")             # later sentence: first stands
    assert tracker.structured()["tts_model"] == "sarvam/bulbul:v4-flash"
    tracker.mark_dispatched()                                  # re-dispatch clears it
    assert tracker.structured()["tts_model"] is None


async def test_router_stamps_the_engine_with_the_first_byte():
    router = make_router()
    tracker = TurnLatencyTracker()
    tracker.mark_dispatched()
    tracker.mark_tts_request()
    router._latency = tracker
    engine = {"provider": "sarvam", "model": "bulbul:v4-flash", "voice": "shubh"}
    router._generations["ctx1"] = _Generation(engine=engine, provider=FakeProvider())
    await router._dispatch_event(KEY, TTSStreamEvent(
        kind="audio", generation_id="ctx1", audio=b"\x01\x02" * 8,
    ))
    assert tracker.structured()["tts_model"] == "sarvam/bulbul:v4-flash"


def test_call_document_names_the_configured_engines():
    config = make_config(
        stt={"provider": "sarvam", "model": "saaras:v4"},
        tts={"provider": "sarvam", "model": "bulbul:v4-flash"},
        llm={"provider": "openai", "model": "gpt-4o-mini",
             "orchestration": {"provider": "openai", "model": "gpt-6-luna"}},
    )
    assert SessionRecorder("vs_x", config).configured_models() == {
        "stt": "sarvam/saaras:v4", "tts": "sarvam/bulbul:v4-flash",
        "llm": "openai/gpt-4o-mini", "decision": "openai/gpt-6-luna",
    }
    plain = make_config(tts={})
    models = SessionRecorder("vs_y", plain).configured_models()
    assert models["tts"] is None and models["decision"] == "openai/gpt-4o-mini"
