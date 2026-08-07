"""Orchestration (Goal Engine) model selection during bot-config resolution.

The per-turn decision call is a small, hard-deadline JSON task; reasoning-class
conversation models (gpt-5 family, o-series) routinely blow that deadline. The
rules pinned here:

- a configured ``llm_settings.orchestration_provider/_model`` always wins;
- a reasoning-class CONVERSATION model never decides by default — the
  platform's non-reasoning default (openai/gpt-4o-mini here) takes the
  decision call while the conversation model keeps the spoken replies;
- if the platform default is itself reasoning-class, the pinned fast engine
  (openai/gpt-4o-mini) is used;
- a disallowed/unknown orchestration candidate degrades to the next candidate,
  and with none left the engine falls back to the conversation LLM — the call
  never drops;
- non-reasoning conversation models keep deciding themselves (no extra
  engine, no behavior change).
"""

import uuid

import pytest

from shared.bot_config import _is_reasoning_class, _load_config_sync
from shared.config import get_settings
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import BotLanguage, VoiceBot, VoiceBotSetting

pytestmark = pytest.mark.integration

_SUFFIX = uuid.uuid4().hex[:8]


@pytest.fixture()
def bot_factory():
    session = get_sessionmaker()()
    created: list[tuple[str, str]] = []

    def make(llm_model: str, llm_settings: dict | None = None) -> str:
        bot = VoiceBot(
            id=new_id("bot"), tenant_id="tn-001",
            name=f"OrchSel {_SUFFIX} {len(created)}",
            status="draft", version="v0.1.0", health="neutral",
        )
        session.add(bot)
        session.flush()
        session.add(BotLanguage(bot_id=bot.id, language_code="hi-IN"))
        vbs = VoiceBotSetting(
            id=new_id("vbs"), bot_id=bot.id, tenant_id="tn-001",
            stt_provider="mock", stt_model="mock",
            tts_provider="mock", tts_model="mock",
            llm_provider="openai", llm_model=llm_model,
            llm_settings=llm_settings or {},
        )
        session.add(vbs)
        session.commit()
        created.append((bot.id, vbs.id))
        return bot.id

    yield make
    for bot_id, vbs_id in created:
        session.query(BotLanguage).filter(BotLanguage.bot_id == bot_id).delete()
        session.query(VoiceBotSetting).filter(VoiceBotSetting.id == vbs_id).delete()
        session.query(VoiceBot).filter(VoiceBot.id == bot_id).delete()
    session.commit()
    session.close()


class TestReasoningClassDetection:
    def test_reasoning_families_are_detected(self):
        for model in ("gpt-5-mini", "gpt-5", "gpt-5.1", "gpt-5.6-sol",
                      "o1-preview", "o3-mini", "o4-mini"):
            assert _is_reasoning_class(model), model

    def test_non_reasoning_models_are_not(self):
        for model in ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "mock", "", None):
            assert not _is_reasoning_class(model), model


class TestOrchestrationSelection:
    def test_gpt5_mini_conversation_decides_on_gpt4o_mini(self, bot_factory):
        """THE production case: bot on gpt-5-mini, nothing configured."""
        bot_id = bot_factory("gpt-5-mini")
        config = _load_config_sync(bot_id, require_published=False)
        # The conversation model is untouched …
        assert config.llm["model"] == "gpt-5-mini"
        # … and the decision call runs on the allowed non-reasoning default.
        orchestration = config.llm["orchestration"]
        assert orchestration is not None
        assert (orchestration["provider"], orchestration["model"]) == (
            "openai", "gpt-4o-mini",
        )
        assert not _is_reasoning_class(orchestration["model"])

    def test_pinned_fast_engine_when_platform_default_is_reasoning(
        self, bot_factory, monkeypatch,
    ):
        settings = get_settings()
        monkeypatch.setattr(settings, "llm_model", "gpt-5-mini")
        bot_id = bot_factory("gpt-5-mini")
        config = _load_config_sync(bot_id, require_published=False)
        orchestration = config.llm["orchestration"]
        assert orchestration is not None
        assert (orchestration["provider"], orchestration["model"]) == (
            "openai", "gpt-4o-mini",
        )

    def test_configured_orchestration_engine_wins(self, bot_factory):
        bot_id = bot_factory("gpt-5-mini", {
            "orchestration_provider": "openai",
            "orchestration_model": "gpt-4.1-nano",
        })
        config = _load_config_sync(bot_id, require_published=False)
        orchestration = config.llm["orchestration"]
        assert orchestration is not None
        assert orchestration["model"] == "gpt-4.1-nano"

    def test_disallowed_configured_engine_degrades_to_fast_default(
        self, bot_factory,
    ):
        """A candidate governance refuses falls through to the next one —
        never onto the reasoning conversation model, never a dropped call."""
        bot_id = bot_factory("gpt-5-mini", {
            "orchestration_provider": "openai",
            "orchestration_model": "model-that-does-not-exist",
        })
        config = _load_config_sync(bot_id, require_published=False)
        orchestration = config.llm["orchestration"]
        assert orchestration is not None
        assert orchestration["model"] == "gpt-4o-mini"

    def test_no_candidate_allowed_degrades_to_conversation_llm(
        self, bot_factory, monkeypatch,
    ):
        """Everything disallowed → no orchestration entry; the runtime's
        GoalEngine then uses the conversation LLM (degraded, still live)."""
        import shared.bot_config as bot_config_module

        monkeypatch.setattr(
            bot_config_module, "_FAST_ORCHESTRATION_ENGINE",
            ("openai", "model-that-does-not-exist"),
        )
        settings = get_settings()
        monkeypatch.setattr(settings, "llm_model", "gpt-5-mini")
        bot_id = bot_factory("gpt-5-mini", {
            "orchestration_provider": "openai",
            "orchestration_model": "another-missing-model",
        })
        config = _load_config_sync(bot_id, require_published=False)
        assert config.llm["orchestration"] is None
        assert config.llm["model"] == "gpt-5-mini"  # the call still resolves

    def test_non_reasoning_conversation_model_needs_no_engine(self, bot_factory):
        bot_id = bot_factory("gpt-4o-mini")
        config = _load_config_sync(bot_id, require_published=False)
        assert config.llm["orchestration"] is None


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
