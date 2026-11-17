"""Explicit wait and readback controls preserve active ID collection."""

from unittest.mock import AsyncMock

import pytest

from tests.unit.test_identifier_capture_runtime import (
    capture_for,
    engine,
    executor,
    make_brain,
)
from voice_runtime.brain import RouteDecision, RouteKind


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Wait", "Hold on", "एक मिनट रुको", "ek minute ruko", "अरे वेट करो"])
async def test_explicit_wait_during_identifier_collection_skips_classifier_and_workflow(text):
    brain = make_brain(workflow_engine=object())
    capture = capture_for(brain, held="1234")
    brain._pending_workflow_question = "Please tell me your complete ID."
    brain._intent_pipeline._enabled = True
    brain._classify_turn = AsyncMock(side_effect=AssertionError("wait must not call classifier"))
    brain._take_decision = AsyncMock(side_effect=AssertionError("wait must not call decision model"))
    brain._handle_workflow = AsyncMock(side_effect=AssertionError("wait must not advance ID capture"))
    brain._generate_reply = AsyncMock(side_effect=AssertionError("wait needs only existing hold acknowledgement"))
    brain._pending_segments = [text]
    brain._start_decision_prefetch()
    assert brain._classify_prefetch is None
    brain._pending_segments.clear()
    await brain._handle_turn(text)
    assert brain._hold_requested_at is not None
    assert brain._identifier_capture is capture
    assert capture.held_digits == "1234"
    assert brain._pending_workflow_question == "Please tell me your complete ID."
    assert any(kind == "hold_acknowledged" for kind, _ in brain._recorder.events)
    brain._classify_turn.assert_not_awaited()
    brain._take_decision.assert_not_awaited()
    brain._handle_workflow.assert_not_awaited()
    brain._generate_reply.assert_not_awaited()
    await brain.cleanup()


def test_wait_shortcut_requires_active_identifier_workflow_and_respects_other_controls():
    brain = make_brain()
    assert brain._deterministic_fast_path("Wait") is None
    capture_for(brain)
    assert brain._deterministic_fast_path("Wait", RouteDecision(kind=RouteKind.HANDOFF)) is None
    assert brain._deterministic_fast_path("Wait", RouteDecision(kind=RouteKind.CALL_CONTROL)) is None
    assert brain._deterministic_fast_path("Wait, what is my bank name?") is None
    brain._active_workflow = None
    assert brain._deterministic_fast_path("Wait") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "कौन सा नोट किया है फिर से बोलिए एक बार।",
    "क्या नोट किया है बताओ तो इसे",
    "what did you note?",
])
async def test_identifier_readback_routes_to_real_buffer_without_classifier_or_llm(
    engine, executor, text,
):
    brain = make_brain(workflow_engine=engine)
    brain._say = AsyncMock(return_value=None)
    route = RouteDecision(kind=RouteKind.WORKFLOW, action="order_support_journey")
    await brain._handle_workflow(route, "hello", 0.0)
    await brain._handle_workflow(route, "one two three four", 0.0)
    assert brain._identifier_capture.held_digits == "1234"
    brain._say.reset_mock()
    brain._intent_pipeline._enabled = True
    brain._classify_turn = AsyncMock(side_effect=AssertionError("readback must skip classifier"))
    brain._take_decision = AsyncMock(side_effect=AssertionError("readback must skip decision model"))
    brain._generate_reply = AsyncMock(side_effect=AssertionError("buffer must answer readback"))
    brain._pending_segments = [text]
    brain._start_decision_prefetch()
    assert brain._classify_prefetch is None
    assert brain._decision_prefetch is None
    brain._pending_segments.clear()

    await brain._handle_turn(text)

    assert brain._identifier_capture.held_digits == "1234"
    assert brain._active_workflow == "order_support_journey"
    assert brain._say.await_count == 1
    readback = brain._say.await_args.args[0]
    assert "4" in readback
    assert "one, two, three, four" in readback
    assert executor.calls == []
    assert any(kind == "deterministic_fast_path" and data["rule"] == "identifier_readback"
               for kind, data in brain._recorder.events)
    brain._classify_turn.assert_not_awaited()
    brain._take_decision.assert_not_awaited()
    brain._generate_reply.assert_not_awaited()
    await brain.cleanup()


def test_readback_shortcut_requires_active_capture_and_respects_other_controls():
    text = "what did you note?"
    brain = make_brain()
    assert brain._deterministic_fast_path(text) is None
    capture_for(brain)
    assert brain._deterministic_fast_path(text, RouteDecision(kind=RouteKind.HANDOFF)) is None
    assert brain._deterministic_fast_path(text, RouteDecision(kind=RouteKind.CALL_CONTROL)) is None
    assert brain._deterministic_fast_path("what is my bank name?") is None
    assert brain._deterministic_fast_path("क्या नोट किया? गलत है, दोबारा") is None
    brain._active_workflow = None
    assert brain._deterministic_fast_path(text) is None


@pytest.mark.asyncio
async def test_phone_readback_fast_path_keeps_masked_engine_response(engine, executor):
    brain = make_brain(workflow_engine=engine)
    brain._say = AsyncMock(return_value=None)
    route = RouteDecision(kind=RouteKind.WORKFLOW, action="phone_capture_journey")
    await brain._handle_workflow(route, "hello", 0.0)
    await brain._handle_workflow(route, "nine eight seven six five", 0.0)
    brain._say.reset_mock()
    brain._classify_turn = AsyncMock(side_effect=AssertionError("readback must skip classifier"))
    brain._take_decision = AsyncMock(side_effect=AssertionError("readback must skip decision model"))
    brain._generate_reply = AsyncMock(side_effect=AssertionError("phone readback must use masking"))

    await brain._handle_turn("what did you note?")

    readback = brain._say.await_args.args[0]
    assert "98765" not in readback.replace(" ", "")
    assert "nine, eight, seven" not in readback
    assert "six, five" in readback
    assert brain._identifier_capture.held_digits == "98765"
    assert executor.calls == []
    await brain.cleanup()
