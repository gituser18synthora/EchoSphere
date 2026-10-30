"""Sarvam STT configuration and VAD ownership regression tests."""

from shared.bot_config import ResolvedBotConfig
from voice_runtime.pipeline import build_stt_service


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def add_event(self, kind: str, **data) -> None:
        self.events.append((kind, data))


def _config(
    settings: dict, language: str = "", languages: list[str] | None = None,
) -> ResolvedBotConfig:
    return ResolvedBotConfig(
        tenant_id="t",
        bot_id="b",
        bot_name="Test",
        version="1",
        published=True,
        language="hi-IN",
        languages=["hi-IN", "en-IN"] if languages is None else languages,
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


async def test_multilingual_bot_auto_detects_on_telephony_too(monkeypatch):
    """The legacy telephony pin no longer applies to a multilingual bot: the
    derived default is auto-detect, so the brain can see a language change."""
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    recorder = _Recorder()
    service = build_stt_service(
        _config({"vad_signals": True}, language=""),
        use_provider_vad=False,
        prefer_primary_language=True,  # accepted, ignored
        recorder=recorder,
    )
    assert service._settings.language is None
    assert service._input_audio_codec == "pcm_s16le"
    kinds = dict(recorder.events)
    assert kinds["stt_language_mode"] == {
        "mode": "auto", "language": None, "auto_detect_language": True,
        "source": "derived", "configured_languages": ["hi-IN", "en-IN"],
        "default_language": "hi-IN",
    }
    await service.cleanup()


async def test_single_language_bot_pins_to_its_language_on_every_transport(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    for prefer in (True, False):
        recorder = _Recorder()
        service = build_stt_service(
            _config({"vad_signals": True}, language="", languages=["hi-IN"]),
            use_provider_vad=False,
            prefer_primary_language=prefer,
            recorder=recorder,
        )
        assert service._settings.language == "hi-IN"
        assert dict(recorder.events)["stt_language_mode"]["mode"] == "pinned"
        assert dict(recorder.events)["stt_language_mode"]["source"] == "derived"
        await service.cleanup()


async def test_explicit_off_pins_a_multilingual_bot(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    recorder = _Recorder()
    service = build_stt_service(
        _config({"auto_detect_language": False}, language=""),
        use_provider_vad=False,
        recorder=recorder,
    )
    assert service._settings.language == "hi-IN"
    event = dict(recorder.events)["stt_language_mode"]
    assert event["mode"] == "pinned" and event["source"] == "explicit"
    assert event["auto_detect_language"] is False
    await service.cleanup()


async def test_explicit_on_auto_detects_a_single_language_bot(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    service = build_stt_service(
        _config({"auto_detect_language": True}, language="", languages=["hi-IN"]),
        use_provider_vad=False,
        prefer_primary_language=True,
    )
    assert service._settings.language is None
    await service.cleanup()


async def test_explicit_stt_language_pins_even_when_auto_detect_is_on(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    recorder = _Recorder()
    service = build_stt_service(
        _config({"auto_detect_language": True}, language="en-IN"),
        use_provider_vad=False,
        recorder=recorder,
    )
    assert service._settings.language == "en-IN"
    assert dict(recorder.events)["stt_language_mode"]["mode"] == "pinned"
    await service.cleanup()


async def test_wav_setting_is_normalized_to_raw_pcm(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    service = build_stt_service(
        _config({"input_encoding": "wav"}),
        use_provider_vad=False,
    )
    assert service._input_audio_codec == "pcm_s16le"
    await service.cleanup()


async def test_resolver_provenance_is_reported_when_present(monkeypatch):
    """resolve_bot_config stamps the effective boolean into the settings and
    keeps where it came from beside it; the event must say "derived"."""
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    recorder = _Recorder()
    config = _config({"auto_detect_language": True}, language="")
    config.stt["auto_detect_language"] = {
        "value": None, "effective": True, "source": "derived",
        "derivedDefault": True, "languages": ["hi-IN", "en-IN"],
    }
    service = build_stt_service(config, use_provider_vad=False, recorder=recorder)
    assert service._settings.language is None
    event = dict(recorder.events)["stt_language_mode"]
    assert event["source"] == "derived" and event["auto_detect_language"] is True
    await service.cleanup()


async def test_malayalam_default_with_auto_detect_is_not_pinned(monkeypatch):
    """Invariant: an effective auto-detect ON never pins because the bot's
    default language happens to be Malayalam."""
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    recorder = _Recorder()
    config = _config({"vad_signals": True}, language="", languages=["en-IN", "hi-IN", "ml-IN"])
    config.language = "ml-IN"
    service = build_stt_service(config, use_provider_vad=False, prefer_primary_language=True, recorder=recorder)
    assert service._settings.language is None
    event = dict(recorder.events)["stt_language_mode"]
    assert event["mode"] == "auto" and event["default_language"] == "ml-IN"
    await service.cleanup()


async def test_explicit_malayalam_stt_language_pins_to_ml_in(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    service = build_stt_service(
        _config({"auto_detect_language": True}, language="ml-IN", languages=["en-IN", "ml-IN"]),
        use_provider_vad=False,
    )
    assert service._settings.language == "ml-IN"
    await service.cleanup()


async def test_explicit_off_pins_a_malayalam_default_bot(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
    config = _config({"auto_detect_language": False}, language="", languages=["en-IN", "hi-IN", "ml-IN"])
    config.language = "ml-IN"
    service = build_stt_service(config, use_provider_vad=False)
    assert service._settings.language == "ml-IN"
    await service.cleanup()
