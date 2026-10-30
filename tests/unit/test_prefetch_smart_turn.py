"""Phase 5 Change 2: no decision prefetch on a call whose turns Smart Turn ends.

With Smart Turn the endpoint is ~150 ms, so the speculative decision had
nothing to overlap: on demo (2026-10-23..29, 196 Smart Turn calls) it was
ready at dispatch on 62 of 962 turns while 109 completed decisions and
~230 cancelled ones answered text the turn never used. The brain learns the
call's endpointing mode from the pipeline and starts the decision at
dispatch, once.
"""

from shared.bot_config import ResolvedBotConfig
from voice_runtime.brain import ConversationBrain

from tests.unit.test_agentic_orchestration import GRACE, _AgenticLLMStub, _RecorderStub


def _brain(*, smart_turn_active: bool) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Support", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Support.",
        intents=[{"name": "support", "samples": ["help"], "route": ""}],
    )
    brain = ConversationBrain(
        config=config, llm=_AgenticLLMStub([]), recorder=_RecorderStub(),
        finalize_grace=GRACE, complete_endpoint=GRACE, smart_turn_active=smart_turn_active,
    )
    import asyncio

    brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)
    brain._pending_segments = ["mujhe invoice samajh nahi aaya"]
    return brain


def _events(brain, kind):
    return [data for k, data in brain._recorder.events if k == kind]


async def test_smart_turn_calls_start_no_prefetch_and_say_so_once():
    brain = _brain(smart_turn_active=True)
    assert brain._goal_engine.enabled
    brain._start_decision_prefetch()
    brain._start_decision_prefetch(text_override="mujhe invoice samajh nahi aaya")
    assert brain._decision_prefetch is None
    assert _events(brain, "decision_prefetch_disabled") == [{"reason": "smart_turn"}]


async def test_other_calls_keep_the_prefetch():
    brain = _brain(smart_turn_active=False)
    brain._start_decision_prefetch()
    assert brain._decision_prefetch is not None
    assert _events(brain, "decision_prefetch_disabled") == []
    brain._discard_decision_prefetch("test")


def test_the_pipeline_derives_the_flag_from_the_smart_turn_gate(monkeypatch):
    from voice_runtime.smart_turn import MODE_OFF, MODE_ON, smart_turn_decision

    assert smart_turn_decision(MODE_ON, "hi-IN", enabled=True) == (True, "tenant_on")
    assert smart_turn_decision(MODE_OFF, "hi-IN", enabled=True)[0] is False
    assert smart_turn_decision(MODE_ON, "hi-IN", enabled=False)[0] is False
