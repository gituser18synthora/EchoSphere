"""Goal Engine decision engine refresh (Phase 3, Change 3).

Benchmark (116 labelled live turns x 2, 2026-10-23): gpt-6-luna on OpenAI's
fast tier decides better than the per-bot models it replaces (routing 69 % vs
54 %, harmful mislabels 5 vs 36) at the same latency, and is OpenAI's named
replacement for gpt-4o-mini / gpt-4.1 / gpt-4.1-mini. These tests pin:

- the OpenAI provider's reasoning-era parameters (lowest effort, temperature
  at effort none) and its decision-only request options;
- the dedicated decision engine resolution (bot override, platform default,
  governance fallbacks) and its decision-only options;
- response_text dropped from the decision for workflow bots (auto) and kept
  for collections / prompt-only bots;
- decision events carrying model, tier and tokens; decision calls billed
  under the decision model; post-call analysis keeping its previous engine;
- the gpt-6-luna catalog migration.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import shared.bot_config as bot_config
from shared.bot_config import ResolvedBotConfig
from shared.orchestration.goal_engine import BotGoalPolicy, GoalEngine, default_timeout_seconds
from shared.providers.base import ProviderConfig
from shared.providers.llm.openai_llm import (
    OpenAILLM,
    _chat_generation_params,
    lowest_reasoning_effort,
)
from tests.unit.test_agentic_orchestration import (
    GRACE,
    _AgenticLLMStub,
    _RecorderStub,
    events,
    make_agentic_brain,
)
from tests.unit.test_brain_collection_policy import turn
from tests.unit.test_costing_capture import make_config
from voice_runtime.brain import ConversationBrain
from voice_runtime.recording import SessionRecorder


# ── OpenAI provider ────────────────────────────────────────────────────────

class TestOpenAIParams:
    def test_lowest_effort_per_family(self):
        assert lowest_reasoning_effort("gpt-6-luna") == "none"
        assert lowest_reasoning_effort("gpt-5.4-mini") == "none"
        assert lowest_reasoning_effort("gpt-5-mini") == "minimal"
        assert lowest_reasoning_effort("gpt-6.1-sol") is None
        assert lowest_reasoning_effort("gpt-4o-mini") is None

    def test_effort_none_sends_temperature_and_completion_cap(self):
        assert _chat_generation_params(
            "gpt-6-luna", temperature=0.0, max_tokens=200, reasoning_effort="lowest",
        ) == {"max_completion_tokens": 200, "reasoning_effort": "none", "temperature": 0.0}

    def test_minimal_effort_never_sends_temperature(self):
        assert _chat_generation_params(
            "gpt-5-mini", temperature=0.0, max_tokens=200, reasoning_effort="lowest",
        ) == {"max_completion_tokens": 200, "reasoning_effort": "minimal"}

    def test_non_reasoning_model_ignores_the_effort(self):
        assert _chat_generation_params(
            "gpt-4o-mini", temperature=0.0, max_tokens=200, reasoning_effort="lowest",
        ) == {"temperature": 0.0, "max_tokens": 200}

    def _provider(self, monkeypatch, model, extra):
        monkeypatch.setenv("OPENAI_UNIT_TEST_KEY", "sk-unit-test-not-real")
        return OpenAILLM(ProviderConfig(
            provider="openai", model=model, api_key_reference="env:OPENAI_UNIT_TEST_KEY",
            timeout_seconds=5, extra=extra,
        ))

    async def test_decision_options_reach_generate_and_the_result(self, monkeypatch):
        provider = self._provider(monkeypatch, "gpt-6-luna", {
            "service_tier": "fast", "prompt_cache_key": "bot_1",
            "reasoning_effort": "lowest", "unrelated": "x",
        })
        create = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}", tool_calls=[]),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=2400, completion_tokens=70,
                                  prompt_tokens_details=SimpleNamespace(cached_tokens=2304)),
            service_tier="fast", model="gpt-6-luna-2026-09-30",
        ))
        provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result = await provider.generate([{"role": "user", "content": "x"}], temperature=0.0, max_tokens=200)
        kwargs = create.call_args.kwargs
        assert kwargs["service_tier"] == "fast" and kwargs["prompt_cache_key"] == "bot_1"
        assert kwargs["reasoning_effort"] == "none" and kwargs["temperature"] == 0.0
        assert "unrelated" not in kwargs and "max_tokens" not in kwargs
        assert (result.cached_tokens, result.service_tier, result.model) == (
            2304, "fast", "gpt-6-luna-2026-09-30")

    async def test_stream_forwards_the_request_options(self, monkeypatch):
        provider = self._provider(monkeypatch, "gpt-4o-mini", {"prompt_cache_key": "bot_1"})

        async def _chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))], usage=None)

        create = AsyncMock(return_value=_chunks())
        provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        assert [t async for t in provider.stream([{"role": "user", "content": "x"}])] == ["hi"]
        assert create.call_args.kwargs["prompt_cache_key"] == "bot_1"


# ── Goal Engine ────────────────────────────────────────────────────────────

class TestGoalEngineResponseText:
    def test_prompt_without_response_text(self):
        on = GoalEngine(llm=None, policy=BotGoalPolicy())._build_system()
        off = GoalEngine(llm=None, policy=BotGoalPolicy(), include_response_text=False)._build_system()
        assert '"context_question":false,"response_text":""}' in on
        assert on.count("response_text") >= 4
        assert "response_text" not in off
        assert '"context_question":false}' in off
        assert len(off) < len(on) - 900

    async def test_provider_metadata_is_kept_per_decision(self):
        class _LLM:
            supports_json_output = False

            async def generate(self, messages, **kwargs):
                return SimpleNamespace(
                    text=json.dumps({"scope": "in_scope", "next_action": "answer"}),
                    input_tokens=2400, output_tokens=60, cached_tokens=2304,
                    service_tier="fast", model="gpt-6-luna")

        engine = GoalEngine(llm=_LLM(), policy=BotGoalPolicy())
        assert await engine.decide("haan", []) is not None
        assert engine.last_usage == (2400, 60)
        assert engine.last_meta == {"model": "gpt-6-luna", "service_tier": "fast", "cached_tokens": 2304}


# ── decision engine resolution ─────────────────────────────────────────────

def _settings(**overrides):
    values = dict(orchestration_provider="openai", orchestration_model="gpt-6-luna",
                  orchestration_service_tier="fast", llm_provider="openai", llm_model="gpt-4o-mini")
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture()
def governance(monkeypatch):
    refused: set = set()
    monkeypatch.setattr(
        bot_config, "_engine_allowed",
        lambda session, kind, provider, model: "inactive" if model in refused else None,
    )
    monkeypatch.setattr(bot_config, "_secret_ref_for", lambda *a, **k: "env:OPENAI_API_KEY")
    return refused


class TestDecisionEngineResolution:
    def _resolve(self, settings=None, **kwargs):
        values = dict(bot_id="bot_1", llm_provider="openai", llm_model="gpt-4o-mini", llm_settings={})
        values.update(kwargs)
        return bot_config._resolve_decision_engine(None, settings or _settings(), **values)

    def test_platform_default_with_decision_only_options(self, governance):
        engine = self._resolve()
        assert (engine["provider"], engine["model"], engine["source"]) == ("openai", "gpt-6-luna", "platform")
        assert engine["extra"] == {"service_tier": "fast", "reasoning_effort": "lowest",
                                   "prompt_cache_key": "bot_1"}

    def test_bot_override_and_tier_opt_out_win(self, governance):
        engine = self._resolve(llm_settings={
            "orchestration_provider": "openai", "orchestration_model": "gpt-4.1",
            "orchestration_service_tier": "default",
        })
        assert (engine["model"], engine["source"]) == ("gpt-4.1", "bot")
        assert engine["extra"]["service_tier"] == "default"

    def test_refused_default_falls_back_to_the_conversation_model(self, governance):
        governance.add("gpt-6-luna")
        engine = self._resolve(llm_model="gpt-4.1")
        assert (engine["model"], engine["source"]) == ("gpt-4.1", "conversation")
        assert engine["extra"]["service_tier"] == "fast"

    def test_reasoning_conversation_model_never_decides_first(self, governance):
        governance.add("gpt-6-luna")
        engine = self._resolve(llm_model="gpt-5-mini")
        assert (engine["model"], engine["source"]) == ("gpt-4o-mini", "reasoning_fallback")

    def test_everything_refused_yields_none(self, governance):
        governance.update({"gpt-6-luna", "gpt-4o-mini"})
        assert self._resolve() is None


# ── brain ──────────────────────────────────────────────────────────────────

WORKFLOW_INTENTS = [{"name": "start", "samples": ["haan"], "route": "workflow:wf_1"}]
PLAIN_INTENTS = [{"name": "billing_query", "samples": ["bill galat aaya"], "route": ""}]


class TestDecisionReplyText:
    def _writes(self, *, intents, settings=None, policy=None):
        stub = SimpleNamespace(_policy=policy)
        config = SimpleNamespace(intents=intents)
        return ConversationBrain._decision_writes_reply_text(stub, config, settings or {})

    def test_auto_is_off_for_workflow_bots(self):
        assert self._writes(intents=WORKFLOW_INTENTS) is False

    def test_auto_is_on_for_collections_and_prompt_only_bots(self):
        assert self._writes(intents=WORKFLOW_INTENTS, policy=object()) is True
        assert self._writes(intents=PLAIN_INTENTS) is True
        assert self._writes(intents=[]) is True

    def test_explicit_setting_wins(self):
        assert self._writes(intents=WORKFLOW_INTENTS, settings={"orchestration_response_text": True}) is True
        assert self._writes(intents=PLAIN_INTENTS, settings={"orchestration_response_text": False}) is False

    def test_the_engine_is_built_with_the_choice(self):
        brain = make_agentic_brain(context=None, intents=WORKFLOW_INTENTS, llm=_AgenticLLMStub([]))
        assert brain._goal_engine._include_response_text is False
        brain = make_agentic_brain(context=None, intents=PLAIN_INTENTS, llm=_AgenticLLMStub([]))
        assert brain._goal_engine._include_response_text is True


class TestDecisionEventsAndUsage:
    async def test_decision_event_and_usage_are_the_decision_models(self):
        brain = make_agentic_brain(
            context=None, intents=PLAIN_INTENTS,
            llm=_AgenticLLMStub([{"scope": "in_scope", "next_action": "answer"}]),
        )
        await turn(brain, "bill galat aaya hai")
        if brain._generation is not None:
            await brain._generation
        decided = events(brain, "orchestration_decision")[-1]
        assert (decided["input_tokens"], decided["output_tokens"], decided["cached_tokens"]) == (150, 60, 0)
        assert "model" in decided and "service_tier_requested" in decided
        usage = brain._recorder.usage
        assert usage["decision_llm_requests"] == 1
        assert usage["decision_llm_input_tokens"] == 150
        assert usage.get("llm_input_tokens", 0) == 0  # the reply streamed no usage report

    def test_decision_usage_is_billed_under_the_decision_model(self):
        config = make_config(llm={
            "provider": "openai", "model": "gpt-4o-mini",
            "orchestration": {"provider": "openai", "model": "gpt-6-luna", "source": "platform"},
        })
        recorder = SessionRecorder("vs_test", config)
        recorder.usage.update(llm_requests=1, llm_output_tokens=40,
                              decision_llm_requests=3, decision_llm_input_tokens=7200,
                              decision_llm_output_tokens=210, decision_llm_cached_tokens=6900)
        captured = []
        recorder._record_usage_events(object(), lambda db, **kw: captured.append(kw), 60)
        by_request = {e["request_id"]: e for e in captured if e["capability"] == "llm"}
        decision = by_request["vs_test:llm-decision"]
        assert decision["model_code"] == "gpt-6-luna"
        assert (decision["requests"], decision["input_tokens"], decision["cached_tokens"]) == (3, 7200, 6900)
        assert by_request["vs_test:llm"]["model_code"] == "gpt-4o-mini"


# ── decision budget per decision model ─────────────────────────────────────

class TestDecisionBudget:
    """gpt-6-luna on the fast tier decides in p50 1.44 s / p90 1.76 s of the
    decisions that complete (live demo 2026-10-23..29), and 13.6 % of its
    attempts ran past the former 2.0 s budget, so its default budget is
    2.5 s. A bot's own orchestration_timeout_seconds still wins."""

    def _brain(self, monkeypatch, *, model="gpt-4o-mini", orchestration=None, settings=None):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
        llm = {"provider": "openai", "model": model, "settings": settings or {},
               "api_key_reference": "env:OPENAI_API_KEY"}
        if orchestration:
            llm["orchestration"] = {"provider": "openai", "model": orchestration,
                                    "api_key_reference": "env:OPENAI_API_KEY", "source": "platform"}
        config = ResolvedBotConfig(
            tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1", published=True,
            language="hi-IN", languages=["hi-IN"], stt={"provider": "sarvam"},
            system_prompt="You are a support bot.", intents=PLAIN_INTENTS, llm=llm,
        )
        return ConversationBrain(config=config, llm=_AgenticLLMStub([]), recorder=_RecorderStub(),
                                 finalize_grace=GRACE, complete_endpoint=GRACE)

    def test_default_budget_per_model(self):
        assert default_timeout_seconds("gpt-6-luna") == 2.5
        assert default_timeout_seconds("GPT-6-Luna-2026-10-01") == 2.5
        assert default_timeout_seconds("gpt-4o-mini") == 1.6
        assert default_timeout_seconds("gpt-6-lunar") == 1.6
        assert default_timeout_seconds(None) == 1.6

    def test_the_decision_models_budget_applies(self, monkeypatch):
        assert self._brain(monkeypatch, orchestration="gpt-6-luna")._goal_engine.timeout_seconds == 2.5
        # No dedicated engine: the conversation model decides, on its budget.
        assert self._brain(monkeypatch, model="gpt-6-luna")._goal_engine.timeout_seconds == 2.5
        assert self._brain(monkeypatch, model="gpt-4o-mini")._goal_engine.timeout_seconds == 1.6

    def test_the_bots_own_budget_wins_and_stays_clamped(self, monkeypatch):
        brain = self._brain(monkeypatch, orchestration="gpt-6-luna",
                            settings={"orchestration_timeout_seconds": 1.2})
        assert brain._goal_engine.timeout_seconds == 1.2
        brain = self._brain(monkeypatch, orchestration="gpt-6-luna",
                            settings={"orchestration_timeout_seconds": 9})
        assert brain._goal_engine.timeout_seconds == 5.0
        brain = self._brain(monkeypatch, orchestration="gpt-6-luna",
                            settings={"orchestration_timeout_seconds": None})
        assert brain._goal_engine.timeout_seconds == 2.5

    async def test_decision_events_carry_the_budget(self):
        brain = make_agentic_brain(
            context=None, intents=PLAIN_INTENTS,
            llm=_AgenticLLMStub([{"scope": "in_scope", "next_action": "answer"}]),
        )
        await turn(brain, "bill galat aaya hai")
        if brain._generation is not None:
            await brain._generation
        assert events(brain, "orchestration_decision")[-1]["budget_ms"] == 1600


# ── post-call analysis keeps its engine ───────────────────────────────────

class TestPostCallEngine:
    def _config(self, source):
        return SimpleNamespace(llm={
            "provider": "openai", "model": "gpt-4o-mini", "api_key_reference": "env:K",
            "extra": {"prompt_cache_key": "bot_1"},
            "orchestration": {"provider": "openai", "model": "gpt-6-luna", "source": source,
                              "api_key_reference": "env:K",
                              "extra": {"service_tier": "fast", "prompt_cache_key": "bot_1"}},
        })

    @pytest.mark.parametrize("source,model", [
        ("platform", "gpt-4o-mini"), ("conversation", "gpt-4o-mini"),
        ("bot", "gpt-6-luna"), ("reasoning_fallback", "gpt-6-luna"),
    ])
    def test_engine_choice(self, monkeypatch, source, model):
        from shared.post_call import analyzer, processor
        import shared.providers.factory as factory

        seen = []
        monkeypatch.setattr(factory, "get_llm_provider", lambda conf: seen.append(conf) or conf)
        analyzer.build_analysis_llm(self._config(source))
        assert seen[-1].model == model
        assert "service_tier" not in seen[-1].extra
        assert processor._analysis_engine_conf(self._config(source))["model"] == model


# ── catalog migration ──────────────────────────────────────────────────────

def test_gpt6_luna_migration_round_trip():
    import importlib

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = importlib.import_module(
        "backend.alembic.versions.d3f5b7c9e1a2_openai_gpt6_luna_decision_model")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "create table provider_models (id varchar(40) primary key, provider_code varchar(50), "
            "capability varchar(20), code varchar(80), display_name varchar(150), description text, "
            "languages text, codecs text, sample_rates text, streaming int, params_schema text, "
            "is_default int, status varchar(20), sort_order int, created_at datetime, "
            "updated_at datetime, is_deleted int default 0)"))
        conn.execute(sa.text(
            "create table provider_pricing (id varchar(40) primary key, provider_code varchar(50), "
            "capability varchar(20), model_code varchar(80), component varchar(40), unit varchar(30), "
            "unit_price numeric, currency_code varchar(3), effective_from datetime, status varchar(20), "
            "sort_order int, created_at datetime, updated_at datetime, is_deleted int default 0)"))
        conn.execute(sa.text("create table currencies (code varchar(3) primary key)"))
        conn.execute(sa.text("insert into currencies (code) values ('USD')"))
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # idempotent
        rows = conn.execute(sa.text(
            "select status, is_default from provider_models where code='gpt-6-luna'")).all()
        assert rows == [("active", 0)]
        prices = dict(conn.execute(sa.text(
            "select component, unit_price from provider_pricing where model_code='gpt-6-luna'")).all())
        assert {k: float(v) for k, v in prices.items()} == {
            "input_tokens": 0.10, "cached_input_tokens": 0.01, "output_tokens": 0.50}
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.downgrade()
        assert conn.execute(sa.text("select count(*) from provider_models")).scalar() == 0
        assert conn.execute(sa.text("select count(*) from provider_pricing")).scalar() == 0
