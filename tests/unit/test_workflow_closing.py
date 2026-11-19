"""A farewell is interruptible until playback and the hangup grace finish."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndWorkerFrame,
    InterruptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.orchestration.router import RouteDecision, RouteKind
from shared.orchestration.decision_schema import ConversationDecision
from tests.unit.test_brain_workflow_routing import (
    _LLMStub, _WorkflowStub, consumed_result, make_brain, settle, transcript,
)
from voice_runtime.frames import WorkflowClosingFrame, WorkflowClosingPlayedFrame

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM
WORKFLOW = "completed_service_flow"


@pytest.fixture(autouse=True)
def short_close_grace(monkeypatch):
    monkeypatch.setattr("voice_runtime.brain._WORKFLOW_CLOSE_GRACE_S", 0.02)
    monkeypatch.setattr("voice_runtime.brain._SMART_TURN_FINAL_WAIT_S", 0.12)


async def closing_brain(reply="The exact visit time is not confirmed. Thank you."):
    workflow = _WorkflowStub(consumed_result("Thank you, goodbye!", done=True))
    workflow.rollback_last_turn = AsyncMock()
    llm = _LLMStub(reply=reply)
    brain = make_brain(workflow_engine=workflow, llm=llm)
    brain._complete_endpoint = brain._short_reply_endpoint = 0.01
    brain._recorder.call_state = {}
    brain._take_decision = AsyncMock(return_value=ConversationDecision(
        next_action="answer", signal="question", confidence=1,
    ))
    await brain._handle_workflow(
        RouteDecision(kind=RouteKind.WORKFLOW, action=WORKFLOW), "yes", 0,
    )
    return brain, workflow, llm


def fence(brain):
    return [f for f in brain._pushed if isinstance(f, WorkflowClosingFrame)][-1]


def endings(brain):
    return [f for f in brain._pushed if isinstance(f, EndWorkerFrame)]


async def played(brain, token=None):
    await brain.process_frame(
        WorkflowClosingPlayedFrame(token=fence(brain).token if token is None else token), UP,
    )


async def cleanup(brain):
    brain.begin_shutdown()
    await brain._cancel_finalize()
    await brain._cancel_generation("test_cleanup")
    brain._disarm_silence_timer()
    await settle()


async def test_no_hangup_until_playback_fence_then_exactly_one_completion():
    brain, _, _ = await closing_brain()
    try:
        assert not brain._closing and not endings(brain)
        assert "call_completed_by_workflow" not in brain._recorder.event_kinds()
        # Synthesis/generation completion and ordinary bot-stop events are
        # insufficient: the farewell's ordered output fence must arrive.
        await brain.process_frame(BotStoppedSpeakingFrame(), UP)
        await asyncio.sleep(0.04)
        assert not endings(brain)
        await played(brain)
        task = brain._workflow_close_task
        await played(brain)  # duplicate delivery must not start another timer
        assert brain._workflow_close_task is task
        await task
        await played(brain)
        assert brain._closing
        assert [end.reason for end in endings(brain)] == ["workflow_completed"]
        assert brain._recorder.event_kinds().count("call_completed_by_workflow") == 1
    finally:
        await cleanup(brain)


@pytest.mark.parametrize("phase", ["before_audio", "during_audio", "grace"])
async def test_interruption_answers_question_and_closes_after_new_reply_only(phase):
    brain, workflow, llm = await closing_brain()
    try:
        old_token = fence(brain).token
        if phase != "before_audio":
            await brain.process_frame(BotStartedSpeakingFrame(), UP)
        if phase == "grace":
            await brain.process_frame(BotStoppedSpeakingFrame(), UP)
            await played(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(InterruptionFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), UP)
        assert brain._pending_workflow_close is None and not brain._closing
        assert any(isinstance(f, InterruptionFrame) for f in brain._pushed)
        await played(brain, old_token)  # an already-dequeued stale fence
        await asyncio.sleep(0.04)
        assert not endings(brain)
        await brain.process_frame(transcript("Kab tak hoga ye?"), DOWN)
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await asyncio.sleep(0.15)
        assert len(workflow.calls) == 1
        workflow.rollback_last_turn.assert_not_called()
        assert "Caller continued before disconnect" in llm.systems[-1]
        assert any(t.role == "user" and t.text == "Kab tak hoga ye?"
                   for t in brain._recorder.turns)
        assert "post_hangup_transcript_dropped" not in brain._recorder.event_kinds()
        new_token = fence(brain).token
        assert new_token != old_token and not endings(brain)
        await played(brain, old_token)
        assert brain._workflow_close_task is None
        await played(brain, new_token)
        await brain._workflow_close_task
        assert [end.reason for end in endings(brain)] == ["workflow_completed"]
    finally:
        await cleanup(brain)


async def test_inviting_the_unasked_question_keeps_the_call_open():
    brain, workflow, llm = await closing_brain(reply="Ji, apna sawaal batayiye.")
    try:
        brain._cancel_workflow_close("caller_interruption")
        brain._take_decision.return_value = ConversationDecision(
            next_action="clarify", needs_clarification=True, confidence=1,
        )
        await brain._handle_turn("Achha ek baat batao")
        assert brain._pending_workflow_close is None and not endings(brain)
        llm.reply = "The visit time has not been confirmed. Thank you, goodbye."
        brain._take_decision.return_value = ConversationDecision(
            next_action="answer", signal="question", confidence=1,
        )
        await brain._handle_turn("Technician kab aayega?")
        assert len(workflow.calls) == 1
        assert brain._pending_workflow_close is not None and not endings(brain)
        await played(brain)
        await brain._workflow_close_task
        assert len(endings(brain)) == 1
    finally:
        await cleanup(brain)


async def test_accepted_transcript_without_vad_invalidates_old_close():
    brain, _, _ = await closing_brain()
    try:
        await played(brain)
        old_token = fence(brain).token
        await brain.process_frame(transcript("Kab tak hoga ye?"), DOWN)
        assert brain._pending_workflow_close is None
        await played(brain, old_token)
        await asyncio.sleep(0.15)
        assert not endings(brain)
        assert "workflow_closing_followup" in brain._recorder.event_kinds()
    finally:
        await cleanup(brain)


async def test_raw_vad_delays_commit_but_noise_does_not_cancel_closing():
    brain, _, _ = await closing_brain()
    try:
        await played(brain)
        token = fence(brain).token
        await brain.process_frame(VADUserStartedSpeakingFrame(), DOWN)
        await asyncio.sleep(0.04)
        assert not endings(brain)
        assert brain._pending_workflow_close[0] == token
        await brain.process_frame(VADUserStoppedSpeakingFrame(), DOWN)
        await brain._workflow_close_task
        assert len(endings(brain)) == 1
    finally:
        await cleanup(brain)


async def test_unconfirmed_filler_during_farewell_does_not_orphan_the_hangup():
    brain, _, _ = await closing_brain()
    try:
        await brain.process_frame(BotStartedSpeakingFrame(), UP)
        token = fence(brain).token
        await brain.process_frame(transcript("hmm"), DOWN)
        assert brain._pending_workflow_close[0] == token
        await brain.process_frame(BotStoppedSpeakingFrame(), UP)
        assert "filler_only_dropped" in brain._recorder.event_kinds()
        await played(brain)
        await brain._workflow_close_task
        assert len(endings(brain)) == 1
    finally:
        await cleanup(brain)


async def test_late_transcript_after_raw_vad_stop_still_cancels_hangup():
    brain, _, _ = await closing_brain()
    try:
        await played(brain)
        await brain.process_frame(VADUserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(VADUserStoppedSpeakingFrame(), DOWN)
        await asyncio.sleep(0.06)  # exceeds grace, within the STT final wait
        assert not brain._closing and not endings(brain)
        await brain.process_frame(transcript("Kab tak hoga ye?"), DOWN)
        assert brain._pending_workflow_close is None
        await asyncio.sleep(0.15)
        assert "workflow_closing_followup" in brain._recorder.event_kinds()
        assert "post_hangup_transcript_dropped" not in brain._recorder.event_kinds()
    finally:
        await cleanup(brain)


async def test_missing_transcript_recovery_cancels_close_before_synthesizing_repeat():
    brain, _, _ = await closing_brain()
    try:
        await played(brain)
        await brain._recover_missing_stt_final(10)
        assert brain._pending_workflow_close is None
        assert "stt_repeat_requested" in brain._recorder.event_kinds()
        await asyncio.sleep(0.04)
        assert not endings(brain) and not brain._closing
    finally:
        await cleanup(brain)


@pytest.mark.parametrize("text,reason", [
    ("Phone kat karo", "caller_hangup_request"),
    ("Mujhe dobara call mat karna", "do_not_call_request"),
])
async def test_explicit_hangup_and_dnc_remain_irreversible(text, reason):
    brain, _, _ = await closing_brain()
    try:
        old_token = fence(brain).token
        await played(brain)
        await brain.process_frame(transcript(text), DOWN)
        assert brain._closing
        await played(brain, old_token)
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await asyncio.sleep(0.04)
        assert [end.reason for end in endings(brain)] == [reason]
    finally:
        await cleanup(brain)


async def test_shutdown_cancels_played_close_and_late_ack_is_inert():
    brain, _, _ = await closing_brain()
    await played(brain)
    task = brain._workflow_close_task
    token = fence(brain).token
    assert brain.begin_shutdown()
    await played(brain, token)
    await asyncio.gather(task, return_exceptions=True)
    assert brain._workflow_close_task is None and not endings(brain)


async def test_hold_during_closing_waits_without_replaying_workflow():
    brain, workflow, _ = await closing_brain()
    try:
        brain._cancel_workflow_close("caller_interruption")
        brain._take_decision.return_value = ConversationDecision(
            next_action="answer", signal="hold", confidence=1,
        )
        await brain._handle_turn("Ek minute ruko")
        assert brain._pending_workflow_close is None and not endings(brain)
        assert len(workflow.calls) == 1
        assert brain._hold_requested_at is not None
    finally:
        await cleanup(brain)


@pytest.mark.parametrize("route_action", [WORKFLOW, "wf_1"])
async def test_old_workflow_intent_and_resolved_id_do_not_repeat_terminal_node(route_action):
    brain, workflow, llm = await closing_brain()
    try:
        brain._cancel_workflow_close("caller_interruption")
        brain._router.decide = lambda *args, **kwargs: RouteDecision(
            kind=RouteKind.WORKFLOW, action=route_action,
        )
        await brain._handle_turn("Technician kab aayega?")
        assert len(workflow.calls) == 1
        assert "Caller continued before disconnect" in llm.systems[-1]
    finally:
        await cleanup(brain)


async def test_new_workflow_intent_retains_normal_routing():
    brain, workflow, _ = await closing_brain()
    try:
        brain._cancel_workflow_close("caller_interruption")
        brain._router.decide = lambda *args, **kwargs: RouteDecision(
            kind=RouteKind.WORKFLOW, action="address_change_flow",
        )
        workflow.result = consumed_result("Please tell me the new address.")
        await brain._handle_turn("Address change karna hai")
        assert len(workflow.calls) == 2
        assert workflow.calls[-1]["workflow_name"] == "address_change_flow"
        assert brain._active_workflow == "address_change_flow"
        assert brain._completed_workflow is None and not endings(brain)
    finally:
        await cleanup(brain)


async def test_new_configured_tool_intent_is_not_disabled_by_completed_flow():
    from shared.orchestration.intent_classifier import IntentClassification

    brain, _, llm = await closing_brain()
    try:
        brain._cancel_workflow_close("caller_interruption")
        brain._take_decision.return_value = ConversationDecision(
            intent="change_address", next_action="call_tool", confidence=1,
        )
        brain._intent_pipeline.from_decision = lambda decision: IntentClassification(
            intent="change_address", requires_tool=True,
            tool_name="configured_address_update", confidence=1,
        )
        brain._run_intent_tool = AsyncMock(return_value="\nVerified tool result: address updated.")
        await brain._handle_turn("Address change karna hai")
        brain._run_intent_tool.assert_awaited_once()
        assert brain._run_intent_tool.call_args.args[0].tool_name == "configured_address_update"
        assert "Verified tool result: address updated." in llm.systems[-1]
    finally:
        await cleanup(brain)


async def test_missing_decision_keeps_followup_open_instead_of_guessing_completion():
    brain, _, _ = await closing_brain()
    try:
        brain._cancel_workflow_close("caller_interruption")
        brain._take_decision.return_value = None
        await brain._handle_turn("Ek baat aur")
        assert brain._pending_workflow_close is None and not endings(brain)
    finally:
        await cleanup(brain)
