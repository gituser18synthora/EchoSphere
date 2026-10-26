"""Per-bot request options for the conversation model (llm_settings).

``reply_service_tier`` / ``reply_reasoning_effort`` let a bot run its reply
model on OpenAI's fast tier at the lowest reasoning effort — needed for
gpt-6-luna, whose default (medium) effort is far too slow for a live call.
They apply to every call on the conversation model (replies, grounded
rewrites, the workflow fact extractor, intent classification); post-call
analysis drops the tier.
"""

from types import SimpleNamespace

import pytest

import shared.bot_config as bot_config
from backend.core.provider_catalog import validate_llm_settings
from shared.providers.base import ProviderConfig
from shared.providers.llm.openai_llm import OpenAILLM

SCHEMA = {"temperature": {"type": "number", "min": 0, "max": 2}}


def test_default_is_the_cache_key_only():
    assert bot_config._reply_request_options("bot_1", {}) == {"prompt_cache_key": "bot_1"}


def test_tier_and_effort_are_passed_through():
    assert bot_config._reply_request_options(
        "bot_1", {"reply_service_tier": " fast ", "reply_reasoning_effort": "Lowest"},
    ) == {"prompt_cache_key": "bot_1", "service_tier": "fast", "reasoning_effort": "lowest"}


def test_the_openai_provider_applies_them(monkeypatch):
    monkeypatch.setenv("OPENAI_UNIT_TEST_KEY", "sk-unit-test-not-real")
    provider = OpenAILLM(ProviderConfig(
        provider="openai", model="gpt-6-luna", api_key_reference="env:OPENAI_UNIT_TEST_KEY",
        timeout_seconds=5,
        extra=bot_config._reply_request_options(
            "bot_1", {"reply_service_tier": "fast", "reply_reasoning_effort": "lowest"}),
    ))
    assert provider._request_extra == {"prompt_cache_key": "bot_1", "service_tier": "fast"}
    assert provider._reasoning_effort == "lowest"


@pytest.mark.parametrize("settings", [
    {"reply_service_tier": "fast", "reply_reasoning_effort": "lowest"},
    {"reply_service_tier": "default"},
    {"reply_reasoning_effort": "none", "temperature": 0.3},
])
def test_valid_settings_pass(settings):
    assert validate_llm_settings(SCHEMA, settings) == []


@pytest.mark.parametrize("settings,fragment", [
    ({"reply_service_tier": "turbo"}, "reply_service_tier' must be one of"),
    ({"reply_reasoning_effort": "max"}, "reply_reasoning_effort' must be one of"),
    ({"reply_service_tier": 1}, "must be a string"),
])
def test_invalid_settings_are_rejected(settings, fragment):
    errors = validate_llm_settings(SCHEMA, settings)
    assert errors and fragment in errors[0]


def test_post_call_analysis_drops_the_reply_tier(monkeypatch):
    from shared.post_call import analyzer
    import shared.providers.factory as factory

    seen = []
    monkeypatch.setattr(factory, "get_llm_provider", lambda conf: seen.append(conf) or conf)
    analyzer.build_analysis_llm(SimpleNamespace(llm={
        "provider": "openai", "model": "gpt-6-luna", "api_key_reference": "env:K",
        "extra": {"prompt_cache_key": "bot_1", "service_tier": "fast", "reasoning_effort": "lowest"},
        "orchestration": {"provider": "openai", "model": "gpt-6-luna", "source": "platform"},
    }))
    assert seen[-1].model == "gpt-6-luna"
    assert seen[-1].extra == {"prompt_cache_key": "bot_1", "reasoning_effort": "lowest"}
