"""Parameter-schema validation, language mapping helpers, sentence buffering."""

import pytest

from backend.core.provider_catalog import validate_params
from shared.providers.languages import (
    matches_model_language,
    to_platform_language,
    to_provider_language,
)
from voice_runtime.aggregator import VoiceSentenceAggregator


class TestValidateParams:
    SCHEMA = {
        "pace": {"type": "number", "min": 0.5, "max": 2.0, "default": 1.0},
        "min_buffer_size": {"type": "integer", "min": 10, "max": 500, "default": 40},
        "auto_mode": {"type": "boolean", "default": True},
        "mode": {"type": "enum", "values": ["transcribe", "verbatim"], "default": "transcribe"},
        "dict_id": {"type": "string", "optional": True, "max_length": 10},
        "chunk_length_schedule": {"type": "int_list", "min": 50, "max": 500, "max_items": 3},
        "streaming": {"type": "boolean", "default": True, "fixed": True},
    }

    def check(self, params):
        return validate_params(self.SCHEMA, params, prefix="TTS")

    def test_valid_params_pass(self):
        assert self.check({
            "pace": 1.2, "min_buffer_size": 40, "auto_mode": False,
            "mode": "verbatim", "dict_id": "abc", "chunk_length_schedule": [100, 200],
        }) == []

    def test_unknown_parameter_rejected(self):
        assert "unknown parameter" in self.check({"bogus": 1})[0]

    def test_number_range(self):
        assert self.check({"pace": 5.0}) and not self.check({"pace": 2.0})
        assert self.check({"pace": "fast"})

    def test_integer_range_and_type(self):
        assert self.check({"min_buffer_size": 5})
        assert self.check({"min_buffer_size": 1.5})
        assert self.check({"min_buffer_size": True})  # bools are not integers

    def test_boolean(self):
        assert self.check({"auto_mode": "yes"}) and not self.check({"auto_mode": True})

    def test_enum(self):
        assert self.check({"mode": "translate"}) and not self.check({"mode": "verbatim"})

    def test_string_max_length(self):
        assert self.check({"dict_id": "x" * 11}) and not self.check({"dict_id": "x" * 10})

    def test_int_list(self):
        assert not self.check({"chunk_length_schedule": [50, 500]})
        assert self.check({"chunk_length_schedule": [10]})          # below min
        assert self.check({"chunk_length_schedule": [50, 60, 70, 80]})  # too many
        assert self.check({"chunk_length_schedule": "50,60"})       # not a list

    def test_fixed_field_cannot_change(self):
        assert self.check({"streaming": False}) and not self.check({"streaming": True})

    def test_none_values_ignored(self):
        assert self.check({"pace": None, "dict_id": None}) == []


class TestLanguageMapping:
    def test_sarvam_odia_alias_round_trip(self):
        assert to_provider_language("sarvam", "or-IN") == "od-IN"
        assert to_platform_language("sarvam", "od-IN") == "or-IN"

    def test_constrained_to_model_languages(self):
        sarvam_langs = ["hi-IN", "en-IN", "od-IN"]
        assert to_provider_language("sarvam", "or-IN", sarvam_langs) == "od-IN"
        assert to_provider_language("sarvam", "hi-IN", sarvam_langs) == "hi-IN"
        assert to_provider_language("sarvam", "ta-IN", sarvam_langs) is None

    def test_elevenlabs_iso_prefix_match(self):
        eleven = ["en", "hi", "ta"]
        assert to_provider_language("elevenlabs", "hi-IN", eleven) == "hi"
        assert to_provider_language("elevenlabs", "en-US", eleven) == "en"
        assert to_provider_language("elevenlabs", "fr-FR", eleven) is None

    def test_matches_model_language(self):
        assert matches_model_language("elevenlabs", "en-GB", ["en"])
        assert not matches_model_language("sarvam", "fr-FR", ["hi-IN"])
        # Empty list = language-agnostic (LLMs, mock).
        assert matches_model_language("openai", "anything", [])
        assert matches_model_language("mock", "hi-IN", None)


class TestSentenceAggregator:
    async def collect(self, aggregator, pieces):
        out = []
        for piece in pieces:
            async for aggregation in aggregator.aggregate(piece):
                out.append(aggregation.text)
        rest = await aggregator.flush()
        if rest:
            out.append(rest.text)
        return out

    async def test_sentence_punctuation_flush(self):
        aggregator = VoiceSentenceAggregator()
        out = await self.collect(
            aggregator, ["This is one sentence. ", "And here is another one."]
        )
        assert out == ["This is one sentence.", "And here is another one."]

    async def test_short_sentence_merged_forward(self):
        aggregator = VoiceSentenceAggregator(min_flush_chars=8)
        out = await self.collect(aggregator, ["Hi. ", "Nice to meet you today."])
        assert out == ["Hi. Nice to meet you today."]

    async def test_max_buffer_force_flush_preserves_order(self):
        aggregator = VoiceSentenceAggregator(max_buffer_chars=40)
        out = await self.collect(aggregator, ["alpha " * 15])
        assert len(out) >= 2
        assert " ".join(out).split() == ["alpha"] * 15

    async def test_interruption_clears_buffers(self):
        aggregator = VoiceSentenceAggregator(min_flush_chars=100)
        async for _ in aggregator.aggregate("Hi."):
            pass
        await aggregator.handle_interruption()
        assert await aggregator.flush() is None
