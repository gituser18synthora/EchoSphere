"""Sarvam STT configuration and VAD ownership regression tests."""

from shared.bot_config import ResolvedBotConfig
from voice_runtime.pipeline import build_stt_service


def _config(settings: dict, language: str = "") -> ResolvedBotConfig:
    return ResolvedBotConfig(
        tenant_id="t",
        bot_id="b",
        bot_name="Test",
        version="1",
        published=True,
        language="hi-IN",
        languages=["hi-IN", "en-IN"],
        stt={
            "provider": "sarvam",
            "model": "saaras:v3",
            "language": language,
            "api_key_reference": "env:TEST_SARVAM_API_KEY",
            "settings": settings,
        },
    )


async def test_all_saved_sarvam_vad_controls_reach_service(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    service = build_stt_service(
        _config({
            "vad_signals": True,
            "high_vad_sensitivity": True,
            "positive_speech_threshold": 0.75,
            "negative_speech_threshold": 0.25,
            "min_speech_frames": 5,
            "first_turn_min_speech_frames": 3,
            "negative_frames_count": 6,
            "negative_frames_window": 10,
            "start_speech_volume_threshold": 0.2,
            "interrupt_min_speech_frames": 8,
            "pre_speech_pad_frames": 4,
            "num_initial_ignored_frames": 2,
        }, language="hi-IN"),
        use_provider_vad=True,
    )

    settings = service._settings
    assert settings.language == "hi-IN"
    assert settings.vad_signals is True
    assert settings.high_vad_sensitivity is True
    assert settings.positive_speech_threshold == 0.75
    assert settings.negative_speech_threshold == 0.25
    assert settings.min_speech_frames == 5
    assert settings.first_turn_min_speech_frames == 3
    assert settings.negative_frames_count == 6
    assert settings.negative_frames_window == 10
    assert settings.start_speech_volume_threshold == 0.2
    assert settings.interrupt_min_speech_frames == 8
    assert settings.pre_speech_pad_frames == 4
    assert settings.num_initial_ignored_frames == 2
    await service.cleanup()


async def test_local_vad_overrides_saved_provider_vad(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    service = build_stt_service(
        _config({"vad_signals": True}),
        use_provider_vad=False,
    )
    assert service._settings.vad_signals is False
    await service.cleanup()


async def test_empty_language_keeps_multilingual_auto_detection(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    service = build_stt_service(
        _config({"vad_signals": True}, language=""),
        use_provider_vad=False,
    )
    assert service._settings.language is None
    await service.cleanup()
