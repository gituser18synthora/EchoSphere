"""Runtime regressions established by the four November 17 demo calls."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame, TextFrame,
    UserStoppedSpeakingFrame, VADUserStartedSpeakingFrame,
)

from tests.unit.test_identifier_capture_runtime import capture_for, frame_in, make_brain, stub_turn_handler, transcript
from voice_runtime.recording import SessionRecorder
from voice_runtime.stt_recovery import STTFinalMissingFrame
from voice_runtime.turn_metrics import TurnLatencyTracker


ALPHANUMERIC = {
    "dataType": "identifier", "preserveAlphanumeric": True,
    "regexPattern": r"(?<![A-Za-z0-9])\d{7,8}[A-Z]?(?![A-Za-z0-9])",
}


@pytest.mark.asyncio
async def test_smart_turn_does_not_cut_a_valid_numeric_prefix_before_suffix():
    brain = make_brain()
    handled = stub_turn_handler(brain)
    capture_for(brain, ALPHANUMERIC, window=0.18)
    await frame_in(brain, transcript("one one three two nine eight six"))
    timer = brain._finalize_task
    brain._turn_active = True
    brain._latency.endpoint_by = "smart_turn"
    brain._latency.smart_turn_complete = True
    brain._latency.smart_turn_probability = 0.99
    brain._latency.verdict_at = time.monotonic()
    await frame_in(brain, UserStoppedSpeakingFrame())
    assert brain._finalize_task is timer
    await asyncio.sleep(0.08)
    assert handled == []
    await frame_in(brain, transcript("A for alpha"))
    await asyncio.sleep(0.03)
    assert handled == ["one one three two nine eight six A for alpha"]
    await brain.cleanup()


@pytest.mark.asyncio
async def test_smart_turn_preserves_incomplete_identifier_window():
    brain = make_brain()
    handled = stub_turn_handler(brain)
    capture_for(brain, ALPHANUMERIC, window=0.12)
    await frame_in(brain, transcript("one two three four"))
    timer = brain._finalize_task
    await brain._smart_turn_short_circuit()
    assert brain._finalize_task is timer
    await asyncio.sleep(0.05)
    assert handled == []
    await brain.cleanup()


def test_numeric_prefix_wait_uses_configured_suffix_window():
    brain = make_brain()
    capture = capture_for(brain, ALPHANUMERIC, window=3.0)
    assert capture.hold_delay("1132986") == 3.0
    assert capture.hold_delay("1132986A") == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", [False, True])
async def test_spelling_cannot_switch_language_but_real_request_can(confirmation):
    brain = make_brain()
    brain._config.languages = ["hi-IN", "en-IN"]
    if confirmation:
        brain._pending_identifier_confirmation = True
    else:
        capture_for(brain, ALPHANUMERIC)
    for text in ["1132986 A for Apple", "and B, B for Boy", "4166725 M for Mango"]:
        await brain._maybe_switch_language(text, "en-IN")
        assert brain._conversation_language == "hi-IN"
    await brain._maybe_switch_language("Can we speak in English please?", "en-IN")
    assert brain._conversation_language == "en-IN"
    await brain.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", [False, True])
async def test_garbled_spelling_or_partial_digits_cannot_switch_language(confirmation):
    """DEMO cv_d0e7a288dfa3 (2026-11-17): "A for Apple" was transcribed as
    "A for S one" / "One is for Apple"; neither validated as dictation, the
    call flipped to English and the Hindi caller was re-prompted in English."""
    brain = make_brain()
    brain._config.languages = ["hi-IN", "en-IN"]
    if confirmation:
        brain._pending_identifier_confirmation = True
    else:
        capture_for(brain, ALPHANUMERIC)
    for text in ["A for S one", "Yes 0701014 One is for Apple", "Size Zero Seven Double Nine One Seven"]:
        await brain._maybe_switch_language(text, "en-IN")
        assert brain._conversation_language == "hi-IN", text
    assert {e["reason"] for e in brain._recorder.events_of("language_switch_blocked")} == {"identifier_attempt"}
    await brain._maybe_switch_language("Can we speak in English please?", "en-IN")
    assert brain._conversation_language == "en-IN"
    await brain.cleanup()


@pytest.mark.asyncio
async def test_spelling_marker_outside_identifier_collection_does_not_block_a_switch():
    brain = make_brain()
    brain._config.languages = ["hi-IN", "en-IN"]
    await brain._maybe_switch_language("I am calling for a paper roll request", "en-IN")
    assert brain._conversation_language == "en-IN"
    await brain.cleanup()


@pytest.mark.asyncio
async def test_clear_identifier_confirmation_never_spends_a_classifier_call():
    brain = make_brain(workflow_engine=object())
    brain._active_workflow = "wf-1"
    brain._pending_identifier_confirmation = True
    brain._pending_workflow_question = "Is that ID correct?"
    brain._classify_turn = AsyncMock(side_effect=AssertionError("unnecessary classifier"))
    brain._take_decision = AsyncMock(side_effect=AssertionError("unnecessary decision"))
    brain._handle_workflow = AsyncMock()
    brain._intent_pipeline._enabled = True
    for text in ["Okay yes, it's correct", "It's correct", "No, this is wrong", "and B, B for Boy"]:
        brain._pending_segments = [text]
        brain._start_decision_prefetch()
        assert brain._classify_prefetch is None
        brain._pending_segments.clear()
        await brain._handle_turn(text)
    assert brain._handle_workflow.await_count == 4
    brain._classify_turn.assert_not_awaited()
    brain._take_decision.assert_not_awaited()
    await brain.cleanup()


@pytest.mark.asyncio
async def test_cleanup_cannot_rearm_silence_from_generation_done_callback():
    brain = make_brain()
    brain._turn_active = False
    brain._generation = asyncio.create_task(asyncio.sleep(20))
    brain._generation.add_done_callback(lambda _: brain._arm_silence_timer())
    await brain.cleanup()
    await asyncio.sleep(0)
    assert brain._closing is True
    assert brain._silence_task is None
    await brain._schedule_finalize(0)
    assert brain._finalize_task is None


@pytest.mark.asyncio
async def test_transport_shutdown_is_immediate_and_preserves_controlled_end_state():
    brain = make_brain()
    brain._arm_silence_timer()
    assert brain.begin_shutdown() is True
    assert brain._closing
    assert brain.begin_shutdown() is False
    await brain._recover_missing_stt_final(1)
    await brain._arm_latency_filler("wait")
    assert brain._recorder.turns == []
    await brain.cleanup()
    assert brain._silence_task is None


@pytest.mark.asyncio
async def test_missing_final_asks_repeat_without_inventing_user_turn_or_advancing():
    brain = make_brain()
    brain._active_workflow = "wf-1"
    brain._pending_workflow_question = "Which option?"
    for generation in [1, 1, 2, 3]:
        await frame_in(brain, STTFinalMissingFrame(generation=generation))
    spoken = [f.text for f in brain._pushed if isinstance(f, TextFrame)]
    assert len(spoken) == 2
    assert all(turn.role == "bot" for turn in brain._recorder.turns)
    assert brain._active_workflow == "wf-1"
    assert brain._pending_workflow_question == "Which option?"
    await brain.cleanup()
    await frame_in(brain, STTFinalMissingFrame(generation=4))
    assert len([f for f in brain._pushed if isinstance(f, TextFrame)]) == 2


@pytest.mark.asyncio
async def test_missing_final_does_not_interrupt_new_speech_or_buffered_digits():
    brain = make_brain()
    brain._physical_speech_active = True
    await brain._recover_missing_stt_final(1)
    brain._physical_speech_active = False
    brain._pending_segments = ["one two three"]
    await brain._recover_missing_stt_final(2)
    assert brain._recorder.turns == []
    await brain.cleanup()


@pytest.mark.asyncio
async def test_missing_final_before_stop_is_deferred_then_recovered_once():
    brain = make_brain()
    brain._turn_active = True
    await frame_in(brain, STTFinalMissingFrame(generation=1))
    assert brain._recorder.turns == []
    assert brain._pending_missing_stt_generation == 1
    await frame_in(brain, UserStoppedSpeakingFrame())
    assert brain._stt_repeat_prompts == 1
    await frame_in(brain, STTFinalMissingFrame(generation=1))
    await frame_in(brain, UserStoppedSpeakingFrame())
    assert brain._stt_repeat_prompts == 1
    await brain.cleanup()


@pytest.mark.asyncio
async def test_missing_final_while_reply_keeps_playing_never_replaces_the_question():
    """DEMO cv_c337303f00bb (2026-11-17): a no-transcript episode 1.3 s into
    the Terminal ID prompt, the ID readback and the bank confirmation made the
    bot say "tell me a bit more about what you need" the moment each finished."""
    brain = make_brain()
    brain._active_workflow = "wf-1"
    brain._pending_workflow_question = "क्या यह सही है?"
    brain._bot_speaking = True
    await frame_in(brain, STTFinalMissingFrame(generation=1))
    assert brain._pending_missing_stt_generation is None
    await frame_in(brain, BotStoppedSpeakingFrame())
    assert brain._stt_repeat_prompts == 0
    assert [f.text for f in brain._pushed if isinstance(f, TextFrame)] == []
    assert brain._recorder.events_of("stt_final_missing_ignored") == [
        {"generation": 1, "reason": "reply_still_playing"}]
    await brain.cleanup()


@pytest.mark.asyncio
async def test_missing_final_that_cut_a_reply_resumes_that_reply():
    """DEMO cv_c337303f00bb 13:17:24Z: the docket confirmation was cut after
    1.15 s by speech that produced no transcript, then replaced by a generic
    clarification — the caller never heard that the docket had been raised."""
    brain = make_brain()
    brain._active_workflow = "wf-1"
    brain._pending_workflow_question = "क्या मैं आज आपकी किसी और चीज़ में मदद कर सकता हूँ?"
    brain._interrupted_reply = "सर, आपका service docket successfully raise हो गया है।"
    await frame_in(brain, STTFinalMissingFrame(generation=7))
    spoken = [f.text for f in brain._pushed if isinstance(f, TextFrame)]
    assert spoken == ["सर, आपका service docket successfully raise हो गया है।"]
    assert brain._interrupted_reply is None
    assert brain._stt_repeat_prompts == 1
    await brain.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("language,workflow,question,expected", [
    ("hi-IN", "wf-1", "क्या आपके bank का नाम INDIAN BANK है?",
     "माफ़ कीजिए, समझ नहीं आया। क्या आपके bank का नाम INDIAN BANK है?"),
    ("en-IN", "wf-1", "Which option?", "Sorry, I didn't catch that. Which option?"),
    ("hi-IN", None, "", "कृपया दोबारा बोलिए?"),
])
async def test_missing_final_reasks_the_pending_question_not_a_generic_clarification(
        language, workflow, question, expected):
    brain = make_brain()
    brain._conversation_language = language
    brain._active_workflow = workflow
    brain._pending_workflow_question = question
    await frame_in(brain, STTFinalMissingFrame(generation=3))
    assert [f.text for f in brain._pushed if isinstance(f, TextFrame)] == [expected]
    await brain.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded_by", ["fresh_speech", "accepted_text", "shutdown"])
async def test_deferred_missing_final_never_reprompts_for_superseded_speech(superseded_by):
    brain = make_brain()
    stub_turn_handler(brain)
    brain._turn_active = True
    await frame_in(brain, STTFinalMissingFrame(generation=1))
    assert brain._pending_missing_stt_generation == 1
    if superseded_by == "fresh_speech":
        await frame_in(brain, VADUserStartedSpeakingFrame())
        brain._physical_speech_active = False
    elif superseded_by == "accepted_text":
        await frame_in(brain, transcript("Please help with my machine"))
    else:
        brain.begin_shutdown()
    await frame_in(brain, UserStoppedSpeakingFrame())
    assert brain._stt_repeat_prompts == 0
    assert brain._pending_missing_stt_generation is None
    await brain.cleanup()


@pytest.mark.parametrize("transferred,hangup,disconnect,expected", [
    (False, None, "client_disconnected", "client_disconnected"),
    (True, None, "client_disconnected", "transferred"),
    (False, {"reason": "caller_hangup"}, "client_disconnected", "caller_hangup"),
    (False, {"reason": "unknown"}, "client_disconnected", "client_disconnected"),
    (False, None, None, "completed"),
])
def test_final_reason_preserves_transport_end_and_telephony_precedence(transferred, hangup, disconnect, expected):
    recorder = SimpleNamespace(transferred=transferred, hangup=hangup, disconnect_reason=disconnect)
    assert SessionRecorder.final_end_reason(recorder, "completed") == expected


def test_workflow_tool_span_populates_real_timing_instead_of_zero():
    tracker = TurnLatencyTracker("test")
    now = time.time() * 1000
    tracker.mark_tool_span(now - 90, now)
    assert tracker.tool_started_at is not None
    assert tracker.tool_done_at - tracker.tool_started_at == pytest.approx(0.09, abs=0.001)
