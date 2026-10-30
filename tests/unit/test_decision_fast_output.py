"""Phase 4 Change 2: the decision writes its routing fields first and no
reason (llm_settings.decision_fast_output, default off).

The default prompt — and with it every bot's cached prefix — is unchanged.
"""

import json

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.goal_engine import GoalEngine
from voice_runtime.brain import ConversationBrain

from tests.unit.test_agentic_orchestration import GRACE, _AgenticLLMStub, _RecorderStub

ROUTING_ORDER = ('"intent"', '"signal"', '"decision"', '"next_action"', '"scope"',
                 '"context_question"', '"slots"')


def _template(system: str) -> str:
    return next(line for line in system.splitlines() if line.startswith('{"intent"'))


class TestThePrompt:
    def test_routing_fields_come_first_and_reason_is_gone(self):
        template = _template(GoalEngine(llm=None, fast_output=True)._build_system())
        positions = [template.index(key) for key in ROUTING_ORDER]
        assert positions == sorted(positions)
        assert '"reason"' not in template
        system = GoalEngine(llm=None, fast_output=True)._build_system()
        assert "Keep reason" not in system
        assert "Omit empty optional fields" in system

    def test_the_default_prompt_keeps_the_reason(self):
        system = GoalEngine(llm=None)._build_system()
        assert '"confidence":0.0,"reason":"","slots":{},"next_action":"answer"' in _template(system)
        assert "Keep reason to at most 12 words." in system

    def test_response_text_follows_the_engine_either_way(self):
        assert _template(GoalEngine(llm=None, fast_output=True, include_response_text=False)
                         ._build_system()).endswith('"needs_clarification":false}')
        assert _template(GoalEngine(llm=None, fast_output=True)._build_system()).endswith(
            '"response_text":""}')


async def test_a_decision_without_reason_parses():
    llm = _AgenticLLMStub([{"intent": None, "signal": "affirm", "decision": "confirmed",
                            "next_action": "continue_workflow", "scope": "in_scope",
                            "context_question": False}])
    engine = GoalEngine(llm=llm, fast_output=True)
    decision = await engine.decide("हाँ जी")
    assert (decision.signal, decision.decision, decision.reason) == ("affirm", "confirmed", "")
    assert json.loads(json.dumps(decision.as_event()))["reason"] == ""


def _brain(settings: dict) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Support", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Support.",
        intents=[{"name": "support", "samples": ["help"], "route": ""}],
        llm={"provider": "openai", "model": "gpt-x", "settings": settings},
    )
    return ConversationBrain(config=config, llm=_AgenticLLMStub([]), recorder=_RecorderStub(),
                             finalize_grace=GRACE, complete_endpoint=GRACE)


def test_the_brain_follows_the_setting():
    assert '"reason"' not in _template(
        _brain({"decision_fast_output": True})._goal_engine._build_system())
    assert '"reason"' in _template(_brain({})._goal_engine._build_system())
