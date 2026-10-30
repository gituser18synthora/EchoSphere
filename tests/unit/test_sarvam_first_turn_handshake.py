"""The first caller turn must not be held to a stricter speech requirement
than every later turn (Sarvam streaming handshake).

Live 2026-10-22, bot_4b8d6fe95cb1, calls cv_80982e36f4f5 and cv_baeaf87927cb:
the caller's first "haan boliye" (~0.5 s, Silero 0.58 s) after the 16 s
greeting produced no transcript at all — Sarvam answered neither the flush at
VAD stop nor the retried flush (``stt_final_missing``), the turn ended empty
and the silence prompt spoke 15 s later. The second attempt transcribed in
100 ms. Reproduced offline on 2026-10-23 by replaying the call's recorded
post-gate audio into ``EndpointedSarvamSTTService`` against the live API:

- exact replay (18.5 s of gate silence, then the utterance): no ``data``;
- the same audio with ``first_turn_min_speech_frames=2``: "हाँ बोलिए।" in 90 ms;
- the same utterance after only 2 s of silence: transcribed;
- the second utterance of the call: transcribed (as live).

Sarvam's streaming docs: ``first_turn_min_speech_frames`` defaults to 8
(one frame = 512 samples = 64 ms at 8 kHz → 512 ms of consecutive speech
frames above ``positive_speech_threshold``) while ``min_speech_frames``
defaults to 2. Unless the bot sets it, the pipeline now sends the regular
segment requirement for the first turn as well.
"""

from __future__ import annotations

import asyncio

import pytest

from shared.bot_config import ResolvedBotConfig
from voice_runtime import pipeline as pipeline_mod
from voice_runtime.pipeline import build_stt_service
from voice_runtime.sarvam_stt import EndpointedSarvamSTTService


def _config(settings: dict | None = None) -> ResolvedBotConfig:
    return ResolvedBotConfig(
        tenant_id="t", bot_id="b", bot_name="Test", version="1", published=True,
        language="hi-IN", languages=["hi-IN", "en-IN"],
        stt={
            "provider": "sarvam", "model": "saaras:v3", "language": "",
            "api_key_reference": "env:TEST_SARVAM_API_KEY",
            "settings": settings if settings is not None else {},
        },
    )


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
    settings = pipeline_mod.get_settings()
    monkeypatch.setattr(settings, "sarvam_realtime_stt", False)


def _build(config: ResolvedBotConfig) -> EndpointedSarvamSTTService:
    service = build_stt_service(config, sample_rate=8000, use_provider_vad=False)
    assert isinstance(service, EndpointedSarvamSTTService)
    return service


class TestFirstTurnRequirement:
    def test_defaults_to_the_regular_segment_requirement(self, key):
        service = _build(_config({}))
        assert service._settings.first_turn_min_speech_frames == pipeline_mod.SARVAM_MIN_SPEECH_FRAMES_DEFAULT == 2

    def test_follows_a_configured_min_speech_frames(self, key):
        # The live bot's settings shape: min_speech_frames set, first turn not.
        service = _build(_config({
            "mode": "transcribe", "vad_signals": True, "auto_detect_language": True,
            "min_speech_frames": 3, "interrupt_min_speech_frames": 2,
            "positive_speech_threshold": 0.7, "negative_speech_threshold": 0.45,
        }))
        assert service._settings.first_turn_min_speech_frames == 3
        assert service._settings.min_speech_frames == 3

    def test_an_explicit_bot_setting_wins(self, key):
        service = _build(_config({"min_speech_frames": 2, "first_turn_min_speech_frames": 5}))
        assert service._settings.first_turn_min_speech_frames == 5

    @pytest.mark.asyncio
    async def test_the_value_reaches_the_sarvam_handshake(self, key, monkeypatch):
        service = _build(_config({"min_speech_frames": 2}))
        captured: list[dict] = []

        class _Ctx:
            async def __aenter__(self_inner):
                raise RuntimeError("stop at the handshake")

            async def __aexit__(self_inner, *exc):
                return False

        class _Streaming:
            def connect(self, **kwargs):
                captured.append(kwargs)
                return _Ctx()

        service._sarvam_client.speech_to_text_streaming._client = _Streaming()
        errors: list[str] = []

        async def _push_error(error_msg=None, exception=None, **kwargs):
            errors.append(str(error_msg))

        monkeypatch.setattr(service, "push_error", _push_error)
        await service._connect()
        [kwargs] = captured
        assert kwargs["first_turn_min_speech_frames"] == "2"
        assert kwargs["min_speech_frames"] == "2"
        assert kwargs["flush_signal"] == "true" and kwargs["vad_signals"] == "false"
        assert kwargs["input_audio_codec"] == "pcm_s16le"
        assert kwargs["model"] == "saaras:v3" and kwargs["language_code"] == "unknown"
        assert errors and "stop at the handshake" in errors[0]
