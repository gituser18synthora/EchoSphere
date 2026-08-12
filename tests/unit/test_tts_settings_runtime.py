"""Persisted TTS settings → provider wire payload, on real calls.

The bot's saved tts_settings must reach the provider request unchanged for the
engine they were configured against, and must NOT ride along to an engine that
never accepts them (a per-language override or the fallback engine can run a
different provider/model whose parameters have different names and ranges).

Complements tests/integration/test_tts_voice_settings_api.py, which covers the
API and preview layers.
"""

import json

import pytest

from shared.providers.tts.delivery import (
    apply_delivery_params,
    speed_range,
    strip_speed_params,
)
from shared.providers.tts.streaming import TTSStreamSettings
from voice_runtime.tts_router import StreamingTTSRouter

SARVAM_V3 = {"provider": "sarvam", "model": "bulbul:v3", "voice": "shubh",
             "api_key_reference": "env:SARVAM_API_KEY"}


def build_router(tts_config: dict, **kwargs) -> StreamingTTSRouter:
    defaults = dict(language="hi-IN", speed=1.0, pause_ms=0, energy=50, sample_rate=16000)
    defaults.update(kwargs)
    return StreamingTTSRouter(tts_config=tts_config, **defaults)


@pytest.fixture(autouse=True)
def provider_keys(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "sk-sarvam-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-eleven-test")


class TestSavedSettingsReachTheProvider:
    def test_default_engine_receives_persisted_settings(self):
        """Saved UI settings → runtime config → provider stream settings."""
        router = build_router({
            **SARVAM_V3,
            "settings": {"temperature": 0.35, "min_buffer_size": 55},
        })
        settings = router._stream_settings(router._default_engine, "hi-IN")
        assert settings.params["temperature"] == pytest.approx(0.35)
        assert settings.params["min_buffer_size"] == 55

    def test_canonical_speed_becomes_the_provider_speed_parameter(self):
        """A saved speaking speed of 0.9 must be the pace a real call sends."""
        router = build_router({**SARVAM_V3, "settings": {}}, speed=0.9)
        settings = router._stream_settings(router._default_engine, "hi-IN")
        assert settings.params["pace"] == pytest.approx(0.9)

    def test_bot_without_settings_sends_no_provider_parameters(self):
        """Backward compatibility: an existing bot with no tts_settings gets
        provider defaults, with only Delivery-derived values present."""
        router = build_router({**SARVAM_V3, "settings": {}}, speed=1.0, energy=50)
        settings = router._stream_settings(router._default_engine, "hi-IN")
        # Energy 50 is the neutral band — nothing native is injected.
        assert set(settings.params) == {"pace"}

    def test_stale_speed_in_stored_settings_never_shadows_the_canonical_speed(self):
        router = build_router(
            {**SARVAM_V3, "settings": {"pace": 2.0, "speed": 0.6}}, speed=1.1,
        )
        settings = router._stream_settings(router._default_engine, "hi-IN")
        assert settings.params["pace"] == pytest.approx(1.1)
        assert "speed" not in settings.params


class TestSettingsDoNotLeakAcrossEngines:
    """The saved settings describe the DEFAULT engine's provider/model and were
    validated against that model's schema alone."""

    def test_other_provider_language_override_does_not_inherit_base_settings(self):
        router = build_router({
            **SARVAM_V3,
            "settings": {"temperature": 0.35, "min_buffer_size": 55},
            "language_map": {
                "en-IN": {
                    "provider": "elevenlabs", "model": "eleven_flash_v2_5",
                    "voice": "monika", "params": {"stability": 0.4},
                    "api_key_reference": "env:ELEVENLABS_API_KEY",
                },
            },
        })
        settings = router._stream_settings(router._engine_for_language("en-IN"), "en-IN")
        # Only the override's own validated params, plus Delivery speed.
        assert settings.params["stability"] == pytest.approx(0.4)
        assert "temperature" not in settings.params
        assert "min_buffer_size" not in settings.params

    def test_same_provider_different_model_does_not_inherit_base_settings(self):
        """bulbul:v2 accepts neither temperature nor dict_id, so a v3 default
        engine's settings must not follow an override onto v2."""
        router = build_router({
            **SARVAM_V3,
            "settings": {"temperature": 0.35, "dict_id": "collections"},
            "language_map": {
                "en-IN": {
                    "provider": "sarvam", "model": "bulbul:v2", "voice": "anushka",
                    "params": {"pitch": 0.2}, "api_key_reference": "env:SARVAM_API_KEY",
                },
            },
        })
        settings = router._stream_settings(router._engine_for_language("en-IN"), "en-IN")
        assert settings.params["pitch"] == pytest.approx(0.2)
        assert "temperature" not in settings.params
        assert "dict_id" not in settings.params

    def test_same_provider_and_model_override_still_inherits_base_settings(self):
        """A voice-only override runs the SAME model, so the bot's tuned
        settings must still apply — this is not a leak."""
        router = build_router({
            **SARVAM_V3,
            "settings": {"temperature": 0.35},
            "language_map": {
                "en-IN": {
                    "provider": "sarvam", "model": "bulbul:v3", "voice": "aayan",
                    "params": {}, "api_key_reference": "env:SARVAM_API_KEY",
                },
            },
        })
        settings = router._stream_settings(router._engine_for_language("en-IN"), "en-IN")
        assert settings.params["temperature"] == pytest.approx(0.35)

    def test_language_override_params_win_over_base_settings(self):
        router = build_router({
            **SARVAM_V3,
            "settings": {"temperature": 0.35},
            "language_map": {
                "en-IN": {
                    "provider": "sarvam", "model": "bulbul:v3", "voice": "aayan",
                    "params": {"temperature": 0.8},
                    "api_key_reference": "env:SARVAM_API_KEY",
                },
            },
        })
        settings = router._stream_settings(router._engine_for_language("en-IN"), "en-IN")
        assert settings.params["temperature"] == pytest.approx(0.8)


class TestAdapterDropsUnsupportedParameters:
    """Last line of defence: even if an unsupported parameter reached an
    adapter, it must never be put on the wire."""

    def _sarvam_config(self, model: str, params: dict) -> dict:
        from shared.providers.tts.sarvam_ws import SarvamWebSocketTTSProvider

        client = SarvamWebSocketTTSProvider(TTSStreamSettings(
            provider="sarvam", model=model, voice="shubh", language="hi-IN",
            sample_rate=16000, codec="linear16", params=params, api_key="sk-test",
        ))
        return client._build_config()

    def test_v3_config_omits_v2_only_pitch_and_loudness(self):
        config = self._sarvam_config(
            "bulbul:v3", {"pace": 1.1, "pitch": 0.4, "loudness": 1.5, "temperature": 0.5},
        )
        assert config["pace"] == pytest.approx(1.1)
        assert config["temperature"] == pytest.approx(0.5)
        assert "pitch" not in config
        assert "loudness" not in config

    def test_v2_config_includes_pitch_and_loudness_but_not_temperature(self):
        config = self._sarvam_config(
            "bulbul:v2", {"pace": 1.1, "pitch": 0.4, "loudness": 1.5, "temperature": 0.5},
        )
        assert config["pitch"] == pytest.approx(0.4)
        assert config["loudness"] == pytest.approx(1.5)
        assert "temperature" not in config

    def test_elevenlabs_context_message_carries_only_supported_voice_settings(self):
        from shared.providers.tts.elevenlabs_ws import ElevenLabsWebSocketTTSProvider

        client = ElevenLabsWebSocketTTSProvider(TTSStreamSettings(
            provider="elevenlabs", model="eleven_flash_v2_5", voice="monika",
            language="en-IN", sample_rate=16000, codec="pcm", api_key="sk-test",
            params={
                "stability": 0.4, "similarity_boost": 0.8, "style": 0.1,
                "use_speaker_boost": True, "speed": 1.05,
                # Sarvam leftovers must never appear in voice_settings.
                "temperature": 0.5, "pitch": 0.3,
            },
        ))
        message = json.loads(json.dumps(client._context_init("ctx")))
        voice_settings = message["voice_settings"]
        assert voice_settings == {
            "stability": 0.4, "similarity_boost": 0.8, "style": 0.1,
            "use_speaker_boost": True, "speed": 1.05,
        }


class TestDeliveryHelpers:
    def test_speed_range_is_model_specific(self):
        assert speed_range("sarvam", "bulbul:v2") == (0.3, 3.0)
        assert speed_range("sarvam", "bulbul:v3") == (0.5, 2.0)
        assert speed_range("elevenlabs", "eleven_flash_v2_5") == (0.7, 1.2)
        # Eleven v3 documents no speed control at all.
        assert speed_range("elevenlabs", "eleven_v3") is None

    def test_strip_speed_params_leaves_everything_else(self):
        assert strip_speed_params({"pace": 1.2, "speed": 0.9, "pitch": 0.3}) == {"pitch": 0.3}
        assert strip_speed_params(None) == {}

    def test_speed_is_clamped_into_the_selected_models_range(self):
        """A canonical 2.0 is legal platform-wide but out of range for
        ElevenLabs, whose documented maximum is 1.2."""
        params = apply_delivery_params("elevenlabs", "eleven_flash_v2_5", {}, speed=2.0)
        assert params["speed"] == pytest.approx(1.2)
        assert apply_delivery_params(
            "sarvam", "bulbul:v3", {}, speed=2.0
        )["pace"] == pytest.approx(2.0)

    def test_speed_is_not_sent_to_a_model_without_a_speed_control(self):
        params = apply_delivery_params("elevenlabs", "eleven_v3", {"stability": 0.5}, speed=1.2)
        assert "speed" not in params
        assert params["stability"] == pytest.approx(0.5)
