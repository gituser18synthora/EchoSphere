"""Regressions from the 23 Nov overlap, farewell and call-screening audit.

These exercise actual turn delivery and workflow handling. A recognized short
answer is retained, but it cannot silently answer a question it predates.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, EndWorkerFrame,
    InterruptionFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from tests.unit.test_brain_turn_taking import make_brain, settle_turn, stub_turn_handler
from tests.unit.test_brain_workflow_routing import transcript
from tests.unit.test_brain_workflow_routing import (
    make_brain as make_workflow_brain, _WorkflowStub, _LLMStub, consumed_result,
)
from tests.unit.test_held_segment_guard import make_brain as make_gate_brain
from tests.unit.test_midday_call_review_runtime import _sms_call, _said, _cut, SMS_STEPS
from tests.unit.test_workflow_closing import (
    closing_brain, cleanup, endings, fence, played,
)
from voice_runtime.brain import ConversationBrain
from shared.orchestration.decision_schema import ConversationDecision

DOWN = FrameDirection.DOWNSTREAM


@pytest.mark.parametrize("text", ["हाँ।", "नहीं।", "haan ji", "no"])
async def test_stale_short_answer_is_retained_for_reask_instead_of_silent_drop(text):
    brain, _, _ = make_gate_brain()
    brain._unheard_reply_guard = True
    brain._active_workflow = "service_flow"
    brain._pending_workflow_question = "Do you still require service?"
    brain._pending_ask_shape = {"kind": "yes_no"}
    brain._reply_nodes = ["service_question"]
    brain._pending_segments = [text]
    brain._pending_newest_final_at = time.monotonic() - 3.825
    assert brain._drop_stale_held_short_text() is False
    assert brain._pending_segments == [text]
    assert brain._unheard_reply["text"] == brain._pending_workflow_question
    brain._turn_counter += 1
    record = brain._take_unheard_reply(text, None)
    assert record and record["reason"] == "held_answer_before_question_end"
    assert "stale_held_short_dropped" not in brain._recorder.event_kinds()
    brain.begin_shutdown()


async def test_recorded_stale_haan_runs_real_workflow_reask_without_advancing(monkeypatch):
    brain = await _sms_call(monkeypatch, guard=True)
    try:
        brain._pending_segments = ["हाँ।"]
        brain._pending_newest_final_at = time.monotonic() - 3.825
        assert brain._drop_stale_held_short_text() is False
        await brain._consume_pending_turn()
        await settle_turn()
        assert brain._active_workflow == "wf_sms"
        assert brain._recorder.turns[-2].text == "हाँ।"
        assert _said(brain)[-1] == brain._pending_workflow_question
        assert "unheard_reply_not_answered" in brain._recorder.event_kinds()
    finally:
        await brain.cleanup()


@pytest.mark.parametrize("text", ["Hello", "हेलो।", "Hello hello", "Hello ji sir"])
async def test_attention_over_unheard_question_does_not_answer_it(monkeypatch, text):
    brain = await _sms_call(monkeypatch, guard=True)
    try:
        _cut(brain, 0.64)
        await brain._handle_turn(text)
        assert _said(brain)[-1] == SMS_STEPS
        assert brain._active_workflow == "wf_sms"
        assert "unheard_reply_not_answered" in brain._recorder.event_kinds()
    finally:
        await brain.cleanup()


@pytest.mark.parametrize("text", ["Hello, service nahi chahiye", "Hello, when will you come?", "ji 12345"])
def test_attention_detector_never_swallows_real_content(text):
    assert not ConversationBrain._attention_only_turn(text)


@pytest.mark.parametrize("text", ["Hello", "हाँ जी", "hmm", "Thank you", "ठीक है", "haan thanks"])
async def test_confirmed_closing_ack_finishes_without_another_llm_or_workflow_turn(monkeypatch, text):
    monkeypatch.setattr("voice_runtime.brain._WORKFLOW_CLOSE_GRACE_S", 0.01)
    brain, workflow, llm = await closing_brain()
    try:
        old_token = fence(brain).token
        brain._take_decision = AsyncMock(side_effect=AssertionError("ack cannot request a decision"))
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(InterruptionFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(transcript(text), DOWN)
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await asyncio.sleep(0.15)
        assert len(workflow.calls) == 1 and llm.systems == []
        assert "workflow_closing_acknowledgement" in brain._recorder.event_kinds()
        assert "workflow_closing_followup" not in brain._recorder.event_kinds()
        assert fence(brain).token != old_token
        await played(brain)
        await brain._workflow_close_task
        assert [frame.reason for frame in endings(brain)] == ["workflow_completed"]
    finally:
        await cleanup(brain)


@pytest.mark.parametrize("text", [
    "Nahi, cancel mat karo", "Wait, address is wrong", "Kab tak hoga?",
    "Achha ek baat batao", "haan lekin payment ka kya", "Hello I need help",
])
def test_substantive_farewell_correction_or_question_stays_interruptible(text):
    brain = make_brain()
    assert not brain._closing_acknowledgement(text)


SCREENING = "Hi, if you record your name and reason for calling, I'll see if this person is available."


@pytest.mark.parametrize("text", [
    SCREENING,
    "This automated screening service asks you to state your name and reason for the call. I'll see if the person is available.",
    "Please leave a message after the tone.",
    "I am unavailable. Leave me a voice message at the beep.",
])
async def test_explicit_machine_prompt_closes_before_identity_llm_or_business(text):
    brain = make_brain()
    brain._take_decision = AsyncMock(side_effect=AssertionError("machine cannot answer identity"))
    try:
        await brain._handle_turn(text)
        assert brain._closing and brain._recorder.disposition == "voicemail"
        assert brain._recorder.call_state["answering_machine_detected"] is True
        assert [f.reason for f in brain._pushed if isinstance(f, EndWorkerFrame)] == ["answering_machine"]
        assert brain._recorder.turns[0].route == "answering_machine"
        assert "route_decision" not in brain._recorder.event_kinds()
    finally:
        await brain.cleanup()


@pytest.mark.parametrize("text", [
    "Please stay on the line.", "Who are you and why are you calling?",
    "Please record your name for my notes.",
    "Please say your name and reason for calling. I'll see if he is available.",
    "Please state your name and reason for the call. I'll see if the person is available.",
    "My phone says please leave a message after the tone.",
    "I heard record your name and reason for calling, I'll see if this person is available.",
])
def test_human_questions_hold_and_reported_recording_prompt_are_not_machine(text):
    assert not ConversationBrain._answering_machine_prompt(text)


def test_short_interruption_context_needs_a_heard_question_and_exempts_identifier():
    brain = make_brain()
    brain._active_workflow = "service_flow"
    brain._pending_workflow_question = "Do you require service?"
    brain._pending_ask_shape = {"kind": "yes_no"}
    brain._bot_speaking = True
    brain._reply_audio_started = True
    brain._reply_audio_started_at = time.monotonic() - 10
    assert brain._short_barge_in_context("हाँ") is None  # wall time is not proof of playback
    assert brain._short_barge_in_context("Hello") == "attention"
    brain._reply_heard = True
    assert brain._short_barge_in_context("हाँ") == "answer"
    brain._identifier_capture = object()
    assert brain._short_barge_in_context("हाँ") is None
    assert brain._short_barge_in_context("Hello") is None
    assert brain._short_barge_in_context("रुको।") == "attention"
    brain._closing = True
    assert brain._short_barge_in_context("stop") is None


@pytest.mark.parametrize("outcome,needs_clarification", [("ambiguous", False), (None, True)])
async def test_model_explicit_uncertainty_does_not_consume_its_affirm_signal(outcome, needs_clarification):
    workflow = _WorkflowStub(consumed_result("Consent confirmed. Goodbye.", done=True))
    llm = _LLMStub(reply="Do you want us to proceed? Please confirm yes or no.")
    brain = make_workflow_brain(workflow_engine=workflow, llm=llm)
    brain._active_workflow = "service_flow"
    brain._pending_workflow_question = "Do you want us to proceed?"
    brain._take_decision = AsyncMock(return_value=ConversationDecision(
        signal="affirm", confidence=0.55, decision=outcome,
        needs_clarification=needs_clarification, next_action="clarify",
    ))
    try:
        await brain._handle_turn("Yeah?")
        assert workflow.calls == [] and len(llm.systems) == 1
        assert brain._active_workflow == "service_flow"
        assert "workflow_answer_needs_clarification" in brain._recorder.event_kinds()
        assert brain._pending_workflow_close is None
    finally:
        await brain.cleanup()


async def test_yes_after_substantive_followup_question_is_not_a_farewell_ack():
    brain, workflow, llm = await closing_brain(reply="Please tell me what you want to ask.")
    try:
        brain._cancel_workflow_close("caller_interruption")
        brain._take_decision.return_value = ConversationDecision(
            next_action="clarify", needs_clarification=True, confidence=1,
        )
        await brain._handle_turn("Achha ek baat batao")
        assert brain._workflow_farewell_active is False
        await brain._handle_turn("yes")
        assert len(llm.systems) == 2 and len(workflow.calls) == 1
        assert brain._pending_workflow_close is None
        assert "workflow_closing_acknowledgement" not in brain._recorder.event_kinds()
    finally:
        await cleanup(brain)


async def test_orphan_final_does_not_reuse_previous_turn_speech_timestamps():
    brain = make_brain()
    handled, _ = stub_turn_handler(brain)
    old = time.monotonic() - 19
    brain._latency.speech_started_at = old - 1
    brain._latency.speech_stopped_at = old
    brain._latency.dispatched_at = old + 0.2
    brain._latency.first_final_at = old + 0.1
    brain._latency.smart_turn_complete = True
    try:
        await brain.process_frame(transcript("No service is required."), DOWN)
        await settle_turn()
        assert handled == ["No service is required."]
        assert brain._latency.speech_started_at is None
        assert brain._latency.speech_stopped_at is None
        assert brain._latency.smart_turn_complete is None
        assert brain._latency.first_final_at > old + 18
        assert "response" not in brain._latency.snapshot()
    finally:
        await brain.cleanup()


def test_provisional_output_pause_is_not_counted_as_heard_audio(monkeypatch):
    brain = make_brain()
    brain._unheard_reply_guard = True
    brain._active_workflow = "service_flow"
    brain._reply_nodes = ["question"]
    brain._reply_text = "An introduction followed by a question?"
    brain._reply_audio_started_at = 100.0
    brain._bot_speaking = True
    now = [101.35]
    monkeypatch.setattr("voice_runtime.brain.time.monotonic", lambda: now[0])
    brain._note_reply_playback_pause(True)
    brain._note_reply_playback_pause(True)  # duplicate pause is idempotent
    now[0] = 102.35
    brain._on_reply_interrupted()
    assert brain._reply_cut["played"] == pytest.approx(1.35)
    assert brain._unheard_reply["reason"] == "early_cut"
    brain._begin_reply_tracking()
    assert brain._reply_pause_started_at is None and brain._reply_paused_seconds == 0


def test_resumed_output_excludes_all_completed_pause_intervals(monkeypatch):
    brain = make_brain()
    brain._reply_audio_started_at = 100
    now = [101.0]
    monkeypatch.setattr("voice_runtime.brain.time.monotonic", lambda: now[0])
    brain._note_reply_playback_pause(True)
    now[0] = 102
    brain._note_reply_playback_pause(False)
    brain._note_reply_playback_pause(False)  # duplicate resume
    now[0] = 103
    brain._note_reply_playback_pause(True)
    now[0] = 105
    brain._note_reply_playback_pause(False)
    assert brain._reply_played_seconds() == pytest.approx(2.0)


def test_short_exception_checks_finished_segment_against_enforced_caller_baseline():
    brain, gate, baseline = make_gate_brain()
    baseline.enforce = True
    baseline.observe_trusted(-20.0)
    brain._active_workflow = "service_flow"
    brain._pending_workflow_question = "Do you need service?"
    brain._pending_ask_shape = {"kind": "yes_no"}
    brain._bot_speaking = True
    brain._reply_heard = True
    gate.live_speech_ms = 0  # final followed the physical VAD stop
    gate.snapshot = {"speech_dbfs": -50.0, "live": False}
    assert brain._short_barge_in_context("Hello") is None
    assert brain._short_barge_in_context("हाँ") is None
    gate.snapshot = {"speech_dbfs": -20.0, "live": False}
    assert brain._short_barge_in_context("Hello") == "attention"
    assert brain._short_barge_in_context("हाँ") == "answer"
