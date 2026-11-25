"""A decision that leaves the option key out gets one focused second reading."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.orchestration.decision_schema import ConversationDecision, parse_decision
from shared.orchestration.goal_engine import GoalEngine
from tests.unit.test_brain_workflow_routing import make_brain
from tests.unit.test_workflow_closing import cleanup

OPTIONS = [{"id": "yes", "meaning": "agrees"}, {"id": "no", "meaning": "declines"}]


class _LLM:
    supports_json_output = True

    def __init__(self, text="", error=None):
        self.text, self.error, self.calls = text, error, []

    async def generate(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.text, input_tokens=120, output_tokens=6)


@pytest.mark.parametrize(("raw", "stated", "option"), [
    ({"signal": "affirm", "option": "yes"}, True, "yes"),
    ({"signal": "affirm", "option": None}, True, None),
    ({"signal": "affirm", "option": "null"}, True, None),
    ({"signal": "affirm"}, False, None),
    ({"signal": "question", "context_question": True, "option": "yes", "confidence": 0.9}, True, None),
    ({"scope": "out_of_scope", "option": "yes"}, True, None),
])
def test_the_decision_records_whether_it_stated_an_option(raw, stated, option):
    decision = parse_decision(raw)
    assert decision.option == option and decision.option_stated is stated
    assert decision.as_event()["option_stated"] is stated


@pytest.mark.parametrize(("text", "expected"), [
    ('{"option": "yes"}', (True, "yes")), ('{"option": null}', (True, None)),
    ('{"option": "null"}', (True, None)), ('here: {"option":"no"}', (True, "no")),
    ('{"other": 1}', (False, None)), ("not json", (False, None)), ('{"option": ["yes"]}', (True, None)),
])
async def test_the_second_reading_parses_one_small_json_object(text, expected):
    llm = _LLM(text)
    engine = GoalEngine(llm=llm, timeout_seconds=2.5)
    assert await engine.choose_option("May I forward it?", "haan kar do", "yes = agrees; no = declines") == expected
    (messages, kwargs), = llm.calls
    assert "haan kar do" in messages[0]["content"] and "yes = agrees" in messages[0]["content"]
    assert kwargs["max_tokens"] <= 32 and kwargs["temperature"] == 0.0


async def test_the_second_reading_never_raises_and_needs_all_three_inputs():
    engine = GoalEngine(llm=_LLM(error=RuntimeError("provider down")), timeout_seconds=2.5)
    assert await engine.choose_option("Q?", "reply", "yes = agrees") == (False, None)
    quiet = _LLM('{"option": "yes"}')
    engine = GoalEngine(llm=quiet, timeout_seconds=2.5)
    assert await engine.choose_option("Q?", "", "yes = agrees") == (False, None)
    assert await engine.choose_option("Q?", "reply", "") == (False, None)
    assert quiet.calls == []


async def test_the_brain_rereads_only_a_model_decision_that_left_the_option_out():
    brain = make_brain()
    try:
        brain._pending_hub_options = OPTIONS
        brain._pending_workflow_question = "May I forward it?"
        brain._goal_engine.choose_option = AsyncMock(return_value=(True, "yes"))
        stated = ConversationDecision(source="llm", option="no", option_stated=True)
        assert await brain._answer_option_inputs("no", stated) == {"answer_option": "no", "answer_option_judged": True}
        judged_none = ConversationDecision(source="llm", option=None, option_stated=True)
        assert await brain._answer_option_inputs("hmm what", judged_none) == {"answer_option": None, "answer_option_judged": True}
        deterministic = ConversationDecision(source="deterministic", signal="affirm")
        assert await brain._answer_option_inputs("haan", deterministic) == {"answer_option": None, "answer_option_judged": False}
        assert await brain._answer_option_inputs("haan", None) == {"answer_option": None, "answer_option_judged": False}
        brain._goal_engine.choose_option.assert_not_called()

        omitted = ConversationDecision(source="llm")
        assert await brain._answer_option_inputs("haan kar do", omitted) == {"answer_option": "yes", "answer_option_judged": True}
        brain._goal_engine.choose_option.assert_awaited_once()
        assert "answer_option_reread" in brain._recorder.event_kinds()

        brain._goal_engine.choose_option = AsyncMock(return_value=(True, "invented"))
        assert await brain._answer_option_inputs("x", omitted) == {"answer_option": None, "answer_option_judged": True}
        brain._goal_engine.choose_option = AsyncMock(return_value=(False, None))
        assert await brain._answer_option_inputs("x", omitted) == {"answer_option": None, "answer_option_judged": False}

        brain._pending_hub_options = None
        brain._goal_engine.choose_option = AsyncMock(return_value=(True, "yes"))
        assert await brain._answer_option_inputs("haan", omitted) == {"answer_option": None, "answer_option_judged": False}
        brain._goal_engine.choose_option.assert_not_called()
    finally:
        await cleanup(brain)
