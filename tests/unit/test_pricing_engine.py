"""Pure-logic tests for the billing engine: component mapping, unit math
and Decimal-safe currency conversion. Database-backed selection rules are
covered in tests/integration/test_usage_tracking.py."""

from decimal import Decimal

from shared.billing.currency import convert_from_usd
from shared.billing.pricing import _UNIT_DIVISORS, quantities_for


class TestQuantitiesFor:
    def test_llm_split_components(self):
        # input_tokens is the provider's GROSS prompt count (includes the
        # cached subset); the cached portion is billed at the cached rate
        # only, so it is netted out of the full-rate input component.
        q = quantities_for("llm", input_tokens=1200, output_tokens=340, cached_tokens=100)
        assert q == {
            "input_tokens": Decimal(1100),
            "output_tokens": Decimal(340),
            "cached_input_tokens": Decimal(100),
        }

    def test_llm_fully_cached_prompt_has_no_full_rate_input(self):
        q = quantities_for("llm", input_tokens=500, output_tokens=10, cached_tokens=500)
        assert q == {
            "output_tokens": Decimal(10),
            "cached_input_tokens": Decimal(500),
        }

    def test_llm_blended_fallback_uses_total(self):
        q = quantities_for("llm", total_tokens=1540)
        assert q == {"tokens": Decimal(1540)}

    def test_embedding_uses_total_tokens(self):
        assert quantities_for("embedding", total_tokens=800) == {"tokens": Decimal(800)}
        assert quantities_for("embedding", input_tokens=800) == {"tokens": Decimal(800)}

    def test_stt_audio_seconds(self):
        q = quantities_for("stt", audio_seconds=42.5)
        assert q == {"audio_seconds": Decimal("42.5")}

    def test_tts_characters_beat_duration(self):
        q = quantities_for("tts", characters=250, audio_seconds=12)
        assert q == {"characters": Decimal(250)}

    def test_tts_duration_when_no_characters(self):
        assert quantities_for("tts", audio_seconds=12) == {"audio_seconds": Decimal(12)}

    def test_telephony_call_seconds(self):
        assert quantities_for("telephony", audio_seconds=61) == {"call_seconds": Decimal(61)}

    def test_request_fallback_when_no_quantities(self):
        assert quantities_for("llm", requests=3) == {"requests": Decimal(3)}

    def test_zero_usage_yields_no_components(self):
        assert quantities_for("stt", audio_seconds=0, requests=0) == {}


class TestUnitDivisors:
    def test_per_1k_tokens(self):
        cost = Decimal(1500) / _UNIT_DIVISORS["per_1k_tokens"] * Decimal("0.0006")
        assert cost == Decimal("0.0009")

    def test_per_1m_tokens(self):
        cost = Decimal(2_000_000) / _UNIT_DIVISORS["per_1m_tokens"] * Decimal("2.50")
        assert cost == Decimal("5.00")

    def test_per_minute_prices_seconds(self):
        # STT seconds against a per-minute rate (whisper-1 style $0.006/min).
        cost = Decimal(90) / _UNIT_DIVISORS["per_minute"] * Decimal("0.006")
        assert cost == Decimal("0.009")

    def test_per_hour_prices_seconds(self):
        # Sarvam STT style: rate quoted per hour, usage measured in seconds.
        cost = Decimal(5400) / _UNIT_DIVISORS["per_hour"] * Decimal("30")
        assert cost == Decimal("45")

    def test_per_1k_characters(self):
        cost = Decimal(2500) / _UNIT_DIVISORS["per_1k_characters"] * Decimal("0.015")
        assert cost == Decimal("0.0375")

    def test_per_1m_characters_not_treated_as_per_character(self):
        # A $50/1M-characters rate must divide by 1,000,000 — never 1.
        cost = Decimal(2_500_000) / _UNIT_DIVISORS["per_1m_characters"] * Decimal("50")
        assert cost == Decimal("125")
        assert _UNIT_DIVISORS["per_1m_characters"] == Decimal(1_000_000)
        assert _UNIT_DIVISORS["per_character"] == Decimal(1)

    def test_every_pricing_unit_has_a_divisor(self):
        from shared.models.billing_models import PRICING_UNITS

        assert set(PRICING_UNITS) == set(_UNIT_DIVISORS)


class TestCostQuantization:
    def test_half_up_rounding_at_micro_dollar_boundary(self):
        from decimal import ROUND_HALF_UP

        from shared.billing.pricing import _COST_QUANT

        assert Decimal("0.0000015").quantize(_COST_QUANT, ROUND_HALF_UP) == Decimal("0.000002")
        assert Decimal("0.0000014").quantize(_COST_QUANT, ROUND_HALF_UP) == Decimal("0.000001")


class TestConvertFromUsd:
    def test_usd_to_inr_example_rate(self):
        assert convert_from_usd(Decimal("12.45"), Decimal("86.50")) == Decimal("1076.925000")

    def test_decimal_precision_preserved(self):
        # 0.1 + 0.2 style float traps must not appear.
        assert convert_from_usd(Decimal("0.000123"), Decimal("86.50")) == Decimal("0.010640")

    def test_accepts_float_amount_without_binary_noise(self):
        assert convert_from_usd(0.1, Decimal("3")) == Decimal("0.300000")
