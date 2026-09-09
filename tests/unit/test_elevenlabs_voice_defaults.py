"""ElevenLabs voice-setting defaults and REST output rate.

Contract: the catalog's ElevenLabs defaults match the ElevenLabs API defaults
(stability 0.5, similarity_boost 0.75 — stability 0 with similarity 1.0 is
what made every Voice-tab preview of an ElevenLabs voice sound erratic), the
migration moves only rows still carrying the untouched seed pair, and the REST
adapter synthesizes at the consumer's pipeline rate when ElevenLabs serves it.
"""

import importlib.util
import json
from pathlib import Path

import httpx

from backend.seeds.provider_catalog_seed import (
    _ELEVEN_DEFAULT_VOICE_SETTINGS,
    _ELEVENLABS_TTS_SCHEMA,
    _ELEVENLABS_V3_TTS_SCHEMA,
)
from shared.providers.base import ProviderConfig
from shared.providers.tts.elevenlabs import ElevenLabsTTS

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "backend/alembic/versions/b5d7f9a1c3e5_elevenlabs_voice_setting_defaults.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("m_b5d7f9a1c3e5", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSeedDefaults:
    def test_flash_schema_defaults_follow_the_elevenlabs_api(self):
        assert _ELEVENLABS_TTS_SCHEMA["stability"]["default"] == 0.5
        assert _ELEVENLABS_TTS_SCHEMA["similarity_boost"]["default"] == 0.75
        assert _ELEVENLABS_TTS_SCHEMA["style"]["default"] == 0.0
        assert _ELEVENLABS_TTS_SCHEMA["use_speaker_boost"]["default"] is True
        assert _ELEVENLABS_TTS_SCHEMA["speed"]["default"] == 1.0

    def test_v3_schema_keeps_natural_and_takes_the_similarity_default(self):
        assert _ELEVENLABS_V3_TTS_SCHEMA["stability"]["default"] == 0.5
        assert _ELEVENLABS_V3_TTS_SCHEMA["similarity_boost"]["default"] == 0.75

    def test_seeded_voice_profiles_carry_the_same_defaults(self):
        assert _ELEVEN_DEFAULT_VOICE_SETTINGS["stability"] == 0.5
        assert _ELEVEN_DEFAULT_VOICE_SETTINGS["similarity_boost"] == 0.75


class TestMigrationTransforms:
    def test_number_schema_defaults_move_and_enum_stability_is_left_alone(self):
        m = _migration()
        flash = {
            "stability": {"type": "number", "default": 0.0},
            "similarity_boost": {"type": "number", "default": 1.0},
            "style": {"type": "number", "default": 0.0},
        }
        out = m.fixed_schema(flash, stability=0.5, similarity=0.75,
                             from_stability=0.0, from_similarity=1.0)
        assert out["stability"]["default"] == 0.5
        assert out["similarity_boost"]["default"] == 0.75
        assert out["style"]["default"] == 0.0
        assert flash["stability"]["default"] == 0.0  # input untouched
        v3 = {
            "stability": {"type": "enum", "values": [0.0, 0.5, 1.0], "default": 0.5},
            "similarity_boost": {"type": "number", "default": 1.0},
        }
        out = m.fixed_schema(v3, stability=0.5, similarity=0.75,
                             from_stability=0.0, from_similarity=1.0)
        assert out["stability"]["default"] == 0.5
        assert out["similarity_boost"]["default"] == 0.75

    def test_already_tuned_schema_is_not_rewritten(self):
        m = _migration()
        tuned = {"stability": {"type": "number", "default": 0.4},
                 "similarity_boost": {"type": "number", "default": 0.8}}
        assert m.fixed_schema(tuned, stability=0.5, similarity=0.75,
                              from_stability=0.0, from_similarity=1.0) is None
        assert m.fixed_schema(None, stability=0.5, similarity=0.75,
                              from_stability=0.0, from_similarity=1.0) is None

    def test_profile_seed_pair_moves_but_operator_choices_stay(self):
        m = _migration()
        seed = {"stability": 0, "similarity_boost": 1, "style": 0.0,
                "use_speaker_boost": True, "speed": 1.0}
        out = m.fixed_settings(seed, stability=0.5, similarity=0.75,
                               from_stability=0.0, from_similarity=1.0)
        assert out == {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0,
                       "use_speaker_boost": True, "speed": 1.0}
        for tuned in (
            {"stability": 0.0, "similarity_boost": 0.8},   # similarity tuned
            {"stability": 0.3, "similarity_boost": 1.0},   # stability tuned
            {"style": 0.0},                                # pair missing
            None,
        ):
            assert m.fixed_settings(tuned, stability=0.5, similarity=0.75,
                                    from_stability=0.0, from_similarity=1.0) is None

    def test_downgrade_is_the_exact_inverse(self):
        m = _migration()
        seed = {"stability": 0.0, "similarity_boost": 1.0}
        up = m.fixed_settings(seed, stability=m.NEW_STABILITY, similarity=m.NEW_SIMILARITY,
                              from_stability=m.OLD_STABILITY, from_similarity=m.OLD_SIMILARITY)
        down = m.fixed_settings(up, stability=m.OLD_STABILITY, similarity=m.OLD_SIMILARITY,
                                from_stability=m.NEW_STABILITY, from_similarity=m.NEW_SIMILARITY)
        assert down == seed
        assert m.down_revision == "f3a5c7e9b1d4"


def _adapter(extra: dict, captured: list) -> ElevenLabsTTS:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"\x01\x00" * 480)

    # Built without touching Settings/secret resolution.
    adapter = ElevenLabsTTS.__new__(ElevenLabsTTS)
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"xi-api-key": "sk-unit-test"}
    )
    adapter._model = "eleven_v3"
    adapter._voice = "voice-xyz"
    adapter._timeout = 5.0
    params = dict(extra)
    requested = params.pop("output_sample_rate", None)
    adapter._params = params
    from shared.providers.tts import elevenlabs as module
    adapter.output_sample_rate = (
        int(requested) if requested in module._SUPPORTED_PCM_RATES else module._PCM_RATE
    )
    return adapter


class TestRestOutputRate:
    async def test_pipeline_rate_is_requested_when_elevenlabs_serves_it(self):
        captured: list = []
        adapter = _adapter({"stability": 0.5, "output_sample_rate": 24000}, captured)
        result = await adapter.synthesize("Namaste", language="hi-IN")
        await adapter.aclose()
        assert captured[0].url.params["output_format"] == "pcm_24000"
        assert result.sample_rate == 24000
        # The rate hint never reaches voice_settings on the wire.
        assert "output_sample_rate" not in json.loads(captured[0].content).get("voice_settings", {})

    async def test_unsupported_rate_falls_back_to_16k(self):
        captured: list = []
        adapter = _adapter({"output_sample_rate": 44100}, captured)
        result = await adapter.synthesize("Namaste", language="hi-IN")
        await adapter.aclose()
        assert captured[0].url.params["output_format"] == "pcm_16000"
        assert result.sample_rate == 16000

    def test_constructor_parses_the_rate_hint(self, monkeypatch):
        from shared.providers.tts import elevenlabs as module

        class _Settings:
            tts_api_key_reference = "env:X"

            @staticmethod
            def resolve_secret(_ref):
                return "sk-unit-test"

        monkeypatch.setattr(module, "get_settings", lambda: _Settings())
        adapter = ElevenLabsTTS(ProviderConfig(
            provider="elevenlabs", model="eleven_v3", voice="v",
            extra={"stability": 0.5, "output_sample_rate": "22050"},
        ))
        assert adapter.output_sample_rate == 22050
        assert "output_sample_rate" not in adapter._params
        default = ElevenLabsTTS(ProviderConfig(provider="elevenlabs", model="eleven_v3", voice="v"))
        assert default.output_sample_rate == 16000
