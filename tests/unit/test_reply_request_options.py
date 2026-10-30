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


# ── what a call actually sends, and what OpenAI reports back ───────────────

def _luna(monkeypatch, llm_settings, model="gpt-6-luna"):
    monkeypatch.setenv("OPENAI_UNIT_TEST_KEY", "sk-unit-test-not-real")
    from shared.bot_config import ResolvedBotConfig
    from shared.providers.factory import clear_provider_cache
    from voice_runtime.pipeline import build_llm_provider

    clear_provider_cache()   # providers are cached per config, process-wide

    config = ResolvedBotConfig(
        tenant_id="tn_x", bot_id="bot_1", bot_name="Test", version="v1", published=True,
        llm={"provider": "openai", "model": model, "settings": llm_settings,
             "api_key_reference": "env:OPENAI_UNIT_TEST_KEY",
             "extra": bot_config._reply_request_options("bot_1", llm_settings)},
    )
    return config, build_llm_provider(config)


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            async def chunks():
                yield SimpleNamespace(service_tier="fast", model="gpt-6-luna-2026-10-01", usage=None,
                                      choices=[SimpleNamespace(delta=SimpleNamespace(content="Ji"))])
                yield SimpleNamespace(service_tier="fast", model="gpt-6-luna-2026-10-01", choices=[],
                                      usage=SimpleNamespace(prompt_tokens=1200, completion_tokens=5,
                                                            prompt_tokens_details=SimpleNamespace(cached_tokens=1024),
                                                            completion_tokens_details=SimpleNamespace(reasoning_tokens=0)))
            return chunks()
        return SimpleNamespace(
            service_tier="fast", model="gpt-6-luna-2026-10-01",
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}", tool_calls=None), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=900, completion_tokens=40,
                                  prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                                  completion_tokens_details=SimpleNamespace(reasoning_tokens=7)),
        )


def test_request_options_report_what_is_sent(monkeypatch):
    _, luna = _luna(monkeypatch, {"reply_service_tier": "fast", "reply_reasoning_effort": "lowest"})
    assert luna.request_options() == {
        "prompt_cache_key": "bot_1", "service_tier": "fast", "reasoning_effort": "none"}
    _, mini = _luna(monkeypatch, {"reply_reasoning_effort": "lowest"}, model="gpt-4o-mini")
    assert mini.request_options() == {"prompt_cache_key": "bot_1"}      # not a reasoning model


def test_a_live_call_defaults_to_the_lowest_effort(monkeypatch):
    # Manappuram (demo 2026-10-26): gpt-6-luna without reply options ran at
    # its default medium effort, first token p90 3.8 s against the brain's
    # 3 s deadline -> six "kuch gadbad ho gayi" replies in four calls.
    _, unset = _luna(monkeypatch, {})
    assert unset.request_options() == {"prompt_cache_key": "bot_1", "reasoning_effort": "none"}
    # The bot's own choice still wins, the tier stays opt-in.
    _, chosen = _luna(monkeypatch, {"reply_reasoning_effort": "medium"})
    assert chosen.request_options() == {"prompt_cache_key": "bot_1", "reasoning_effort": "medium"}
    _, mini = _luna(monkeypatch, {}, model="gpt-4o-mini")
    assert mini.request_options() == {"prompt_cache_key": "bot_1"}
    # The resolved config (and with it post-call analysis) is unchanged.
    config, _ = _luna(monkeypatch, {})
    assert config.llm["extra"] == {"prompt_cache_key": "bot_1"}


async def test_an_unset_effort_reaches_the_stream_request(monkeypatch):
    _, llm = _luna(monkeypatch, {})
    fake = _FakeCompletions()
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    assert [t async for t in llm.stream([{"role": "user", "content": "x"}], system="s")] == ["Ji"]
    assert fake.calls[-1]["reasoning_effort"] == "none"
    assert "service_tier" not in fake.calls[-1]


async def test_the_bot_settings_reach_every_request(monkeypatch):
    _, llm = _luna(monkeypatch, {"reply_service_tier": "fast", "reply_reasoning_effort": "lowest"})
    fake = _FakeCompletions()
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    result = await llm.generate([{"role": "user", "content": "x"}], system="s", temperature=0.0, max_tokens=400)
    assert (result.cached_tokens, result.reasoning_tokens, result.service_tier) == (0, 7, "fast")
    assert [t async for t in llm.stream([{"role": "user", "content": "x"}], system="s")] == ["Ji"]
    for call in fake.calls:
        assert (call["service_tier"], call["reasoning_effort"], call["prompt_cache_key"]) == ("fast", "none", "bot_1")
        assert "max_completion_tokens" in call and "max_tokens" not in call
    # What OpenAI reported back for the completed stream.
    assert llm.last_response_meta == {"service_tier": "fast", "cached_tokens": 1024,
                                      "model": "gpt-6-luna-2026-10-01"}


def test_the_call_document_and_the_journal_carry_them(monkeypatch, caplog):
    import logging

    from voice_runtime.brain import ConversationBrain
    from voice_runtime.recording import SessionRecorder

    config, llm = _luna(monkeypatch, {"reply_service_tier": "fast", "reply_reasoning_effort": "lowest"})
    recorder = SessionRecorder("vs_test", config)
    with caplog.at_level(logging.INFO, logger="voice_runtime.brain"):
        ConversationBrain._record_llm_request_options(config, llm, recorder)
    # Per CALL (the provider instance is shared by a bot's calls).
    ConversationBrain._note_llm_call(SimpleNamespace(_recorder=recorder), tier="fast")
    models = recorder.configured_models()
    assert models["llm_options"] == {"prompt_cache_key": "bot_1", "service_tier": "fast",
                                     "reasoning_effort": "none"}
    assert models["llm_service_tiers"] == {"fast": 1}
    assert '"reasoning_effort": "none"' in caplog.text and "openai/gpt-6-luna" in caplog.text
    # A stub engine without the report leaves the document as it was.
    bare = SessionRecorder("vs_bare", config)
    ConversationBrain._record_llm_request_options(config, SimpleNamespace(), bare)
    assert "llm_options" not in bare.configured_models()


async def test_the_extractor_reports_the_tier_of_each_reader():
    from shared.orchestration.mdnd_slots import extract_mdnd_slots
    from shared.providers.base import LLMResult

    class _Readers:
        supports_json_output = True

        def __init__(self):
            self.calls = 0

        async def generate(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 4:
                raise RuntimeError("one reader failed")
            return LLMResult(text='{"patch": {}, "evidence": {}}', input_tokens=1200,
                             output_tokens=20, service_tier="fast", cached_tokens=1024,
                             reasoning_tokens=0)

    result = await extract_mdnd_slots(
        _Readers(), text="haan maine call kiya tha", slots={},
        pending_question="Kya aapne customer ko call kiya tha?", pending_variable="m_called_customer",
    )
    assert result.requests == 4
    assert result.service_tiers == {"fast": 3}      # the failed reader reports nothing
    # Cached prompt tokens of each completed reader reach the call's usage.
    assert (result.input_tokens, result.cached_tokens) == (3600, 3072)


def test_the_brain_tallies_the_tiers_of_its_call():
    from voice_runtime.brain import ConversationBrain

    recorder = SimpleNamespace(llm_service_tiers={})
    fake = SimpleNamespace(_recorder=recorder)
    ConversationBrain._note_llm_call(fake, tier="fast", requests=3)
    ConversationBrain._note_llm_call(fake, tier=None)
    ConversationBrain._note_llm_call(fake, tier="default")
    assert recorder.llm_service_tiers == {"fast": 3, "unreported": 1, "default": 1}


def test_non_streamed_calls_fold_cached_and_reasoning_tokens():
    """Rewrites, the memory greeting, the classifier and the extractor never
    added their cached prompt tokens to the call (only streamed replies did):
    MDND's reply path read cached=0 whatever OpenAI served from the cache."""
    from voice_runtime.brain import ConversationBrain

    recorder = SimpleNamespace(usage={"llm_cached_tokens": 100, "llm_reasoning_tokens": 0},
                               llm_service_tiers={})
    fake = SimpleNamespace(_recorder=recorder)
    ConversationBrain._note_llm_call(fake, tier="fast", cached=1024, reasoning=12)
    ConversationBrain._note_llm_call(fake, tier="fast")     # a stream: its site folded them
    assert recorder.usage == {"llm_cached_tokens": 1124, "llm_reasoning_tokens": 12}
    assert recorder.llm_service_tiers == {"fast": 2}

