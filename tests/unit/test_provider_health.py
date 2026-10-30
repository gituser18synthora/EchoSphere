"""Provider outage detection (voice_runtime.provider_health).

Three consecutive calls — across bots — whose FIRST provider outcome is an
account- or service-level ProviderError (auth, rate limit / spend limit,
upstream 5xx) open an outage: one ERROR log line with the HTTP status and
message, one ``provider_outage`` event carrying both. The first healthy call
closes it with ``provider_recovered``. Timeouts and bad requests never vote.
"""

import asyncio
import logging

import httpx
import pytest
from openai import APITimeoutError

from shared.providers.base import ProviderError, error_fields
from shared.providers.llm.openai_llm import _provider_error
from voice_runtime.provider_health import ProviderHealth, provider_health

from tests.unit.test_agentic_orchestration import (
    HEALTHCARE_GOAL_CONFIG, _AgenticLLMStub, make_agentic_brain,
)

LOGGER = "voice_runtime.provider_health"


def _quota() -> ProviderError:
    return ProviderError(
        "openai-llm", "rate_limit",
        "Error code: 429 - {'error': {'code': 'insufficient_quota'}}", status=429,
    )


@pytest.fixture(autouse=True)
def _fresh_tracker():
    provider_health.reset()
    yield
    provider_health.reset()


class TestTheTracker:
    def test_three_consecutive_failed_calls_open_an_outage_once(self, caplog):
        health = ProviderHealth()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert health.failed("c1", _quota(), stage="warmup") is None
            assert health.failed("c2", _quota(), stage="warmup") is None
            event = health.failed("c3", _quota(), stage="decision")
            assert health.failed("c4", _quota(), stage="warmup") is None   # counted, not re-announced
        assert event["kind"] == "provider_outage"
        assert event["status"] == 429 and event["category"] == "rate_limit"
        assert "insufficient_quota" in event["message"] and event["stage"] == "decision"
        assert event["calls"] == 3 and event["since"]
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "HTTP 429" in errors[0].getMessage() and "insufficient_quota" in errors[0].getMessage()
        assert health.outage["failed_calls"] == 4

    def test_one_vote_per_call(self):
        health = ProviderHealth()
        for _ in range(5):
            health.failed("same-call", _quota(), stage="decision")
        assert health.outage is None
        assert health.succeeded("same-call") is None              # already voted: ignored
        assert health.failed("b", _quota(), stage="warmup") is None
        assert health.failed("c", _quota(), stage="warmup")["kind"] == "provider_outage"

    def test_a_healthy_call_resets_the_streak_and_closes_an_outage(self, caplog):
        health = ProviderHealth()
        health.failed("a", _quota(), stage="warmup")
        health.failed("b", _quota(), stage="warmup")
        assert health.succeeded("c") is None                     # streak broken before the third
        for call in ("d", "e"):
            assert health.failed(call, _quota(), stage="warmup") is None
        assert health.failed("f", _quota(), stage="warmup")["kind"] == "provider_outage"
        with caplog.at_level(logging.INFO, logger=LOGGER):
            recovered = health.succeeded("g")
        assert recovered["kind"] == "provider_recovered"
        assert recovered["failed_calls"] == 3 and recovered["duration_s"] >= 0 and recovered["since"]
        assert health.outage is None and health.succeeded("h") is None
        assert [r.levelno for r in caplog.records if "recovered" in r.getMessage()] == [logging.INFO]

    def test_timeouts_bad_requests_and_other_exceptions_never_vote(self):
        health = ProviderHealth()
        assert health.failed("a", ProviderError("llm", "timeout", "no first token"), stage="reply") is None
        assert health.failed("b", ProviderError("openai-llm", "invalid_input", "400"), stage="decision") is None
        assert health.failed("c", RuntimeError("boom"), stage="decision") is None
        assert health.failed("d", None, stage="warmup") is None
        assert health.outage is None
        # none of those calls has voted: a real failure on them still counts
        for call in ("a", "b"):
            assert health.failed(call, _quota(), stage="decision") is None
        assert health.failed("c", _quota(), stage="decision")["kind"] == "provider_outage"

    @pytest.mark.parametrize("category,status", [("auth", 401), ("upstream", 503), ("upstream", None)])
    def test_auth_and_upstream_failures_count(self, category, status):
        health = ProviderHealth()
        error = ProviderError("openai-llm", category, "down", status=status)
        assert health.failed("a", error, stage="warmup") is None
        assert health.failed("b", error, stage="warmup") is None
        event = health.failed("c", error, stage="warmup")
        assert event["category"] == category and event["status"] == status


class TestOpenAIErrorMapping:
    class _StatusError(Exception):
        def __init__(self, code: int) -> None:
            super().__init__(f"Error code: {code} - {{'error': {{'code': 'x'}}}}")
            self.status_code = code

    @pytest.mark.parametrize("code,category", [
        (401, "auth"), (403, "auth"), (429, "rate_limit"), (500, "upstream"),
        (503, "upstream"), (400, "invalid_input"), (404, "invalid_input"),
    ])
    def test_the_http_status_sets_the_category_and_is_kept(self, code, category):
        error = _provider_error("openai-llm", self._StatusError(code))
        assert error.category == category and error.status == code
        assert error.message.startswith(f"Error code: {code}")
        assert str(error) == f"[openai-llm:{category}] {error.message}"

    def test_the_sdk_timeout_and_connection_failures(self):
        timeout = _provider_error("openai-llm", APITimeoutError(request=httpx.Request("POST", "https://x")))
        assert timeout.category == "timeout" and timeout.status is None
        other = _provider_error("openai-llm", ConnectionError("refused"))
        assert other.category == "upstream" and other.status is None

    def test_error_fields(self):
        assert error_fields(None) == {}
        assert error_fields(_quota()) == {
            "error": "ProviderError", "error_category": "rate_limit", "error_status": 429,
            "error_message": _quota().message,
        }
        assert error_fields(RuntimeError("x")) == {"error": "RuntimeError", "error_message": "x"}


class _FailingLLM(_AgenticLLMStub):
    """Every Stage-A call (warm-up included) is refused by the provider."""

    def __init__(self, error: ProviderError | None):
        super().__init__([])
        self.error = error

    async def generate(self, messages, **kwargs):
        if self.error is not None:
            raise self.error
        return await super().generate(messages, **kwargs)


def _events(brain, kind):
    return [data for k, data in brain._recorder.events if k == kind]


async def _warm_up(brain):
    brain._start_decision_warmup()
    await brain._decision_warmup_task


class TestTheBrain:
    async def test_the_warm_up_votes_and_the_third_failed_call_records_the_outage(self, caplog):
        brains = []
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            for i in range(3):
                brain = make_agentic_brain(llm=_FailingLLM(_quota()), goal_policy=HEALTHCARE_GOAL_CONFIG)
                brain._recorder.session_id = f"call-{i}"
                await _warm_up(brain)
                brains.append(brain)
        warmups = [_events(b, "decision_warmup")[0] for b in brains]
        assert all(w["ok"] is False and w["error"] == "ProviderError" for w in warmups)
        assert warmups[0]["error_status"] == 429 and warmups[0]["error_category"] == "rate_limit"
        assert "insufficient_quota" in warmups[0]["error_message"]
        assert [_events(b, "provider_outage") for b in brains[:2]] == [[], []]
        outage = _events(brains[2], "provider_outage")
        assert len(outage) == 1 and outage[0]["status"] == 429 and outage[0]["stage"] == "warmup"
        assert outage[0]["calls"] == 3
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
        # the first healthy call closes it
        healthy = make_agentic_brain(llm=_FailingLLM(None), goal_policy=HEALTHCARE_GOAL_CONFIG)
        healthy._recorder.session_id = "call-ok"
        await _warm_up(healthy)
        recovered = _events(healthy, "provider_recovered")
        assert len(recovered) == 1 and recovered[0]["failed_calls"] == 3
        assert provider_health.outage is None

    async def test_a_failed_decision_records_the_status_on_the_fallback(self):
        brain = make_agentic_brain(llm=_FailingLLM(_quota()), goal_policy=HEALTHCARE_GOAL_CONFIG)
        brain._recorder.session_id = "call-dec"
        assert await brain._decide_turn("hello") is None
        fallback = _events(brain, "orchestration_fallback")
        assert len(fallback) == 1 and fallback[0]["reason"] == "provider_error"
        assert fallback[0]["error_status"] == 429 and fallback[0]["error_category"] == "rate_limit"
        assert "insufficient_quota" in fallback[0]["error_message"]

    async def test_a_timed_out_decision_is_not_an_outage_vote(self):
        """Three calls timing out at the budget say nothing about the account."""
        for i in range(3):
            brain = make_agentic_brain(llm=_FailingLLM(None), goal_policy=HEALTHCARE_GOAL_CONFIG)
            brain._recorder.session_id = f"slow-{i}"
            brain._goal_engine._timeout = 0.0
            assert await brain._decide_turn("hello") is None
            assert _events(brain, "orchestration_fallback")[0]["reason"] in ("timeout", "timeout_before_warmup")
            assert _events(brain, "provider_outage") == []
        assert provider_health.outage is None
