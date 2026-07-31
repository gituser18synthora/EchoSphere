"""Delivery tuning helpers: canonical speed/energy mapping, silence, prompts.

These helpers are the single source of truth shared by the live runtime
(StreamingTTSRouter / EchoTTSService) and the backend voice preview — the
precedence rules asserted here are therefore the platform's behavior:

- Canonical Delivery speed OVERRIDES legacy per-provider pace/speed params.
- Energy only fills provider fields the operator left unset, and only fields
  the selected model documents (Sarvam bulbul:v3 never receives the v2-only
  pitch/loudness controls; eleven_v3 never receives speed).
- Unrelated provider settings pass through untouched.
"""

from shared.audio.pcm import silence_pcm
from shared.bot_config import ResolvedBotConfig
from shared.orchestration.delivery import (
    delivery_instructions,
    empathy_instruction,
    energy_instruction,
)
from shared.providers.tts.delivery import (
    apply_delivery_params,
    clamp_level,
    clamp_speed,
    energy_params,
    provider_speed,
    speed_param_name,
)

class TestSilencePcm:
    def test_byte_length_formula(self):
        # sample_rate * pause_ms / 1000 * 2 bytes (mono s16le)
        assert len(silence_pcm(24000, 350)) == 16800
        assert len(silence_pcm(16000, 350)) == 11200
        assert len(silence_pcm(8000, 500)) == 8000

    def test_always_sample_aligned_and_silent(self):
        blob = silence_pcm(22050, 333)
        assert len(blob) % 2 == 0
        assert blob == b"\x00" * len(blob)

    def test_zero_and_negative_inputs_produce_no_audio(self):
        assert silence_pcm(16000, 0) == b""
        assert silence_pcm(16000, -100) == b""
        assert silence_pcm(0, 350) == b""


class TestClamps:
    def test_speed_clamps_to_platform_range(self):
        assert clamp_speed(0.1) == 0.5
        assert clamp_speed(5) == 2.0
        assert clamp_speed(1.15) == 1.15
        assert clamp_speed(None) == 1.0
        assert clamp_speed("bogus") == 1.0

    def test_level_clamps_to_0_100(self):
        assert clamp_level(-5) == 0
        assert clamp_level(150) == 100
        assert clamp_level(70) == 70
        assert clamp_level(None) == 50


class TestCanonicalSpeedMapping:
    def test_sarvam_speed_maps_to_pace(self):
        params = apply_delivery_params("sarvam", "bulbul:v3", {}, speed=1.2)
        assert params == {"pace": 1.2}

    def test_elevenlabs_speed_maps_to_speed(self):
        params = apply_delivery_params("elevenlabs", "eleven_flash_v2_5", {}, speed=1.1)
        assert params == {"speed": 1.1}

    def test_canonical_speed_overrides_legacy_values(self):
        # Legacy duplicates saved in tts_settings must lose to Delivery tuning
        # for BOTH namings, regardless of provider.
        params = apply_delivery_params(
            "sarvam", "bulbul:v3", {"pace": 0.6, "speed": 0.6}, speed=1.4
        )
        assert params["pace"] == 1.4
        assert "speed" not in params
        params = apply_delivery_params(
            "elevenlabs", "eleven_flash_v2_5", {"speed": 0.8, "pace": 0.8}, speed=1.1
        )
        assert params["speed"] == 1.1
        assert "pace" not in params

    def test_eleven_v3_never_receives_speed(self):
        params = apply_delivery_params("elevenlabs", "eleven_v3", {"speed": 0.9}, speed=1.3)
        assert "speed" not in params and "pace" not in params

    def test_model_range_clamping(self):
        assert provider_speed("elevenlabs", "eleven_flash_v2_5", 2.0) == 1.2
        assert provider_speed("elevenlabs", "eleven_flash_v2_5", 0.5) == 0.7
        assert provider_speed("sarvam", "bulbul:v3", 2.0) == 2.0
        assert provider_speed("sarvam", "bulbul:v2", 0.5) == 0.5

    def test_unknown_provider_has_no_speed_param(self):
        assert speed_param_name("openai") is None
        params = apply_delivery_params("openai", "tts-1", {"voice": "alloy"}, speed=1.2)
        assert params == {"voice": "alloy"}

    def test_unrelated_settings_pass_through(self):
        base = {"min_buffer_size": 40, "temperature": 0.6}
        params = apply_delivery_params("sarvam", "bulbul:v3", base, speed=1.0)
        assert params["min_buffer_size"] == 40 and params["temperature"] == 0.6
        assert base == {"min_buffer_size": 40, "temperature": 0.6}  # not mutated

    def test_no_speed_argument_preserves_legacy_params(self):
        # Back-compat: callers that do not resolve a canonical speed leave
        # stored parameters exactly as they were.
        params = apply_delivery_params("sarvam", "bulbul:v3", {"pace": 0.8})
        assert params["pace"] == 0.8


class TestEnergyMapping:
    def test_neutral_band_sends_nothing(self):
        assert energy_params("elevenlabs", "eleven_flash_v2_5", 50) == {}
        assert energy_params("sarvam", "bulbul:v2", 50) == {}
        assert energy_params("sarvam", "bulbul:v3", 50) == {}

    def test_high_energy_elevenlabs_style(self):
        assert energy_params("elevenlabs", "eleven_flash_v2_5", 90) == {"style": 0.4}
        assert energy_params("elevenlabs", "eleven_v3", 70) == {"style": 0.2}

    def test_sarvam_v2_gets_pitch_and_loudness(self):
        mapped = energy_params("sarvam", "bulbul:v2", 90)
        assert mapped == {"pitch": 0.1, "loudness": 1.3}
        low = energy_params("sarvam", "bulbul:v2", 10)
        assert low == {"pitch": -0.1, "loudness": 0.85}

    def test_sarvam_v3_never_gets_v2_only_fields(self):
        # bulbul:v3 does not document pitch/loudness — nothing native is sent
        # for ANY energy level (the LLM instruction is the fallback).
        for level in (0, 25, 75, 100):
            assert energy_params("sarvam", "bulbul:v3", level) == {}

    def test_explicit_operator_settings_win_over_energy(self):
        params = apply_delivery_params(
            "elevenlabs", "eleven_flash_v2_5", {"style": 0.05}, energy=95
        )
        assert params["style"] == 0.05
        params = apply_delivery_params(
            "sarvam", "bulbul:v2", {"pitch": 0.0}, energy=95
        )
        assert params["pitch"] == 0.0            # operator's explicit choice
        assert params["loudness"] == 1.3         # unset field is filled

    def test_unknown_provider_gets_no_native_energy(self):
        assert energy_params("openai", "tts-1", 90) == {}


class TestDeliveryInstructions:
    def test_empathy_bands_are_deterministic(self):
        assert "neutral" in empathy_instruction(0)
        assert "neutral" in empathy_instruction(20)
        assert "professional warmth" in empathy_instruction(21)
        assert "balanced, warm and natural" in empathy_instruction(50)
        assert "acknowledge the caller's concern" in empathy_instruction(61)
        assert "compassionate" in empathy_instruction(100)
        assert empathy_instruction(50) == empathy_instruction(50)

    def test_energy_bands_are_deterministic(self):
        assert "calm, restrained" in energy_instruction(0)
        assert "measured and relaxed" in energy_instruction(30)
        assert "natural and balanced" in energy_instruction(50)
        assert "upbeat" in energy_instruction(70)
        assert "never shouting" in energy_instruction(95)

    def test_out_of_range_levels_clamp(self):
        assert empathy_instruction(-10) == empathy_instruction(0)
        assert energy_instruction(400) == energy_instruction(100)

    def test_combined_section_hides_numbers_and_guards_content(self):
        section = delivery_instructions(80, 20)
        assert "# Delivery style" in section
        assert "80" not in section and "20" not in section
        # Guardrails: no longer replies, no fabricated feelings, rules intact.
        assert "Never make replies longer" in section
        assert "never fabricate feelings" in section
        assert "safety" in section


class TestResolvedConfigCompatibility:
    def test_old_cached_json_resolves_with_defaults(self):
        raw = (
            '{"tenant_id": "tn", "bot_id": "b1", "bot_name": "Old",'
            ' "version": "v1", "published": true, "speed": 1.2}'
        )
        config = ResolvedBotConfig.from_json(raw)
        assert config.speed == 1.2
        assert config.pause_ms == 350
        assert config.empathy == 50
        assert config.energy == 50

    def test_new_fields_round_trip(self):
        config = ResolvedBotConfig(
            tenant_id="tn", bot_id="b1", bot_name="New", version="v1",
            published=True, pause_ms=500, empathy=80, energy=20,
        )
        clone = ResolvedBotConfig.from_json(config.to_json())
        assert (clone.pause_ms, clone.empathy, clone.energy) == (500, 80, 20)
