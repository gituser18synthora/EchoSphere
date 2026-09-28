"""Generic bots: an opted-in Goal Engine end_call closes the call itself.

Without a domain policy or a workflow end node a generic bot's goodbye was
only words — cv_35733e66a32e (2026-09-28): "Nahi, main Gaurav nahi hoon" →
decision wrong_person/end_call → goodbye spoken, call left open until the
caller hung up. goal_policy.endCall makes that decision binding, per bot.
"""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

from pipecat.frames.frames import EndWorkerFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection

from shared.orchestration.decision_schema import ConversationDecision
from tests.unit.test_brain_workflow_routing import (
    GRACE,
    _LLMStub,
    _RecorderStub,
    _WorkflowStub,
    make_brain,
    off_script_result,
)
from voice_runtime.brain import ConversationBrain

SURVEY_POLICY = {
    "role": "closure feedback executive",
    "goals": [{"id": "feedback", "description": "Collect closure feedback."}],
}
WRONG_PERSON = dict(signal="wrong_person", scope="out_of_scope", decision="unrelated",
                    next_action="end_call", confidence=1.0,
                    reason="Caller is not the intended person.")
GOODBYE = "Sorry, aapko disturb kiya. Aapka din accha rahe."


def survey_brain(goal_policy, *, workflow_engine=None) -> tuple[ConversationBrain, _LLMStub]:
    llm = _LLMStub(reply=GOODBYE)
    template = make_brain(llm=llm)
    brain = ConversationBrain(
        config=replace(template._config, goal_policy=goal_policy),
        llm=llm, recorder=_RecorderStub(), workflow_engine=workflow_engine,
        finalize_grace=GRACE,
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify
    brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)
    return brain, llm


def opted_in(**rule):
    return {**SURVEY_POLICY, "endCall": {"enabled": True, **rule}}


def decide(brain, **data):
    brain._take_decision = AsyncMock(return_value=ConversationDecision.model_validate(data))


def ended(brain) -> bool:
    return any(isinstance(f, EndWorkerFrame) for f in brain._pushed)


async def test_opted_in_wrong_person_says_goodbye_then_ends_the_call():
    brain, llm = survey_brain(opted_in())
    decide(brain, **WRONG_PERSON)
    await brain._handle_turn("Nahi, main Gaurav nahi hoon.")

    assert ended(brain)
    end = next(f for f in brain._pushed if isinstance(f, EndWorkerFrame))
    assert end.reason == "decision_end_call"
    kinds = brain._recorder.event_kinds()
    assert ("call_completed_by_decision", {"reason": "end_call:wrong_person"}) in brain._recorder.events
    assert "scope_redirect" not in kinds  # a goodbye, not a redirect back to the survey
    assert "# Ending the call" in llm.systems[-1]
    assert "# Off-goal turn" not in llm.systems[-1]
    assert brain._history[-1] == {"role": "assistant", "content": GOODBYE}
    assert brain._closing


async def test_default_bot_keeps_todays_behaviour():
    brain, llm = survey_brain(SURVEY_POLICY)
    decide(brain, **WRONG_PERSON)
    await brain._handle_turn("Nahi, main Gaurav nahi hoon.")

    assert not ended(brain)
    assert "scope_redirect" in brain._recorder.event_kinds()
    assert "# Ending the call" not in llm.systems[-1]
    assert not brain._closing


async def test_mid_survey_end_call_at_template_confidence_never_closes():
    # Live 2026-09-25: "Nahi" to "anything else?" → refusal/end_call at 0.0.
    brain, llm = survey_brain(opted_in())
    decide(brain, signal="refusal", scope="in_scope", next_action="end_call",
           confidence=0.0, reason="Caller declined further help.")
    await brain._handle_turn("Nahi")

    assert not ended(brain)
    assert "# Ending the call" not in llm.systems[-1]


async def test_active_workflow_keeps_its_own_close():
    engine = _WorkflowStub(off_script_result(node_prompt="Aapka experience kaisa raha?"))
    brain, llm = survey_brain(opted_in(), workflow_engine=engine)
    brain._active_workflow = "survey_flow"
    decide(brain, **WRONG_PERSON)
    await brain._handle_turn("Nahi, main Gaurav nahi hoon.")

    assert not ended(brain)
    assert "call_completed_by_decision" not in brain._recorder.event_kinds()


async def test_pending_transfer_is_never_ended_by_the_bot():
    brain, llm = survey_brain(opted_in())
    brain._transfer_requested = True
    decide(brain, **WRONG_PERSON)
    await brain._handle_turn("Nahi, main Gaurav nahi hoon.")

    assert not ended(brain)


async def test_speech_after_the_goodbye_gets_no_new_reply():
    brain, llm = survey_brain(opted_in())
    decide(brain, **WRONG_PERSON)
    await brain._handle_turn("Nahi, main Gaurav nahi hoon.")
    replies = len(llm.systems)

    # 25-Sep live call: "Aa okay" after the goodbye restarted the survey.
    await brain.process_frame(
        TranscriptionFrame(text="Aa okay.", user_id="u", timestamp="t", language="hi-IN"),
        FrameDirection.DOWNSTREAM,
    )
    await asyncio.sleep(GRACE * 3)

    assert len(llm.systems) == replies
    assert "post_hangup_transcript_dropped" in brain._recorder.event_kinds()
