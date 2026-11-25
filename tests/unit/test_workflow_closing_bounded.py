"""After the closing line: bounded, deterministic, and never a blank outcome.

DEMO 2026-11-25 (haier/qa/REPORT_2026-11-25.md, problems 6, 10, 14, 23 and
the empty records of problem 1): calls ran 2-3 minutes past the closing line,
a goodbye was spoken without the line closing, "kuch aur poochhna hai?" was
asked, and a caller repeating their answer wiped the recorded outcome.
"""
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import TextFrame

from shared.orchestration.decision_schema import ConversationDecision
from shared.orchestration.router import RouteDecision, RouteKind
from shared.orchestration.workflow_corrections import CORRECTION_STATE_KEY
from tests.unit.test_brain_workflow_routing import _LLMStub, _WorkflowStub, consumed_result, make_brain
from tests.unit.test_workflow_closing import cleanup, endings, played, short_close_grace  # noqa: F401

WORKFLOW = "completed_service_flow"
QUESTION = "क्या आपको service की requirement है?"
CLOSING = "मैं आपकी service request को cancel कर देती हूँ। Haier को समय देने के लिए धन्यवाद, आपका दिन शुभ रहे।"
TAIL = "Haier को समय देने के लिए धन्यवाद, आपका दिन शुभ रहे।"
OPTIONS = [{"id": "yes", "meaning": "needs service"}, {"id": "no", "meaning": "does not need service"}]


async def closed_call(*, answered="no", reply="Technician 48 घंटे में contact करेंगे।", side_effects=False,
                      followups=2):
    """A call whose workflow just closed after the caller answered QUESTION."""
    asked = {**consumed_result(QUESTION), "nodePrompt": QUESTION, "awaitingOptions": OPTIONS,
             "awaitingSignals": ["affirm", "refusal"], "awaitingEdges": []}
    done = {**consumed_result(CLOSING, done=True), "slots": {"outcome": "cancel_no_service"},
            "terminalSetSlots": ["outcome"], "answeredOption": answered, "turnSideEffects": side_effects,
            "closingFollowups": followups}
    workflow = _WorkflowStub(asked)
    workflow.rollback_last_turn = AsyncMock(return_value=True)
    llm = _LLMStub(reply=reply)
    brain = make_brain(workflow_engine=workflow, llm=llm)
    brain._complete_endpoint = brain._short_reply_endpoint = 0.01
    brain._recorder.call_state = {}
    brain._take_decision = AsyncMock(return_value=ConversationDecision(
        next_action="answer", signal="question", confidence=1, source="llm",
    ))
    route = RouteDecision(kind=RouteKind.WORKFLOW, action=WORKFLOW)
    await brain._handle_workflow(route, "haan ji", 0)
    workflow.result = done
    await brain._handle_workflow(route, "nahi", 0)
    return brain, workflow, llm


def spoken(brain):
    return [frame.text for frame in brain._pushed if isinstance(frame, TextFrame)]


async def test_the_terminal_turn_is_remembered_with_its_question_and_answer():
    brain, _, _ = await closed_call()
    try:
        terminal = brain._terminal_turn
        assert terminal["question"] == QUESTION and terminal["option"] == "no"
        assert [o["id"] for o in terminal["options"]] == ["yes", "no"]
        assert brain._terminal_farewell_tail == TAIL
        state = brain._orchestration_state()
        assert state["answer_options"].startswith("yes = needs service")
        assert QUESTION in state["pending_question"] and 'option "no"' in state["pending_question"]
    finally:
        await cleanup(brain)


@pytest.mark.parametrize("text", ["ठीक है", "ठीक। हाँ।", "अच्छा", "ok ji", "थैंक यू", "धन्यवाद मैडम", "बाय", "हम्म", "हेलो", "आपका भी दिन शुभ रहे"])
async def test_a_reaction_to_the_farewell_closes_without_a_model_turn(text):
    brain, workflow, llm = await closed_call()
    try:
        brain._cancel_workflow_close("caller_interruption")
        await brain._handle_turn(text)
        assert llm.systems == [] and len(workflow.calls) == 2
        brain._take_decision.assert_not_called()
        assert brain._pending_workflow_close is not None
        assert "workflow_closing_acknowledgement" in brain._recorder.event_kinds()
    finally:
        await cleanup(brain)


@pytest.mark.parametrize(("cut_at", "replayed"), [(0.7, True), (1.5, True), (6.5, False)])
async def test_a_farewell_cut_in_its_first_words_is_spoken_once_more_before_closing(cut_at, replayed):
    brain, _, _ = await closed_call()
    try:
        brain._cancel_workflow_close("caller_interruption")
        brain._farewell_cut_played = cut_at
        before = len(spoken(brain))
        await brain._handle_turn("ठीक है")
        assert (spoken(brain)[before:] == [CLOSING]) is replayed
        assert ("workflow_farewell_replayed" in brain._recorder.event_kinds()) is replayed
        assert brain._pending_workflow_close is not None
        # never twice: a second acknowledgement only closes
        brain._cancel_workflow_close("caller_interruption")
        brain._farewell_cut_played = 0.5
        again = len(spoken(brain))
        await brain._handle_turn("हाँ जी")
        assert (spoken(brain)[again:] == []) is replayed
    finally:
        await cleanup(brain)


async def test_a_follow_up_is_answered_then_the_authored_closing_line_ends_the_call():
    brain, workflow, llm = await closed_call()
    try:
        brain._cancel_workflow_close("caller_interruption")
        before = len(spoken(brain))
        await brain._handle_turn("Technician kab aayega?")
        assert spoken(brain)[before:] == ["Technician 48 घंटे में contact करेंगे।", TAIL]
        assert "do not say goodbye" in llm.systems[-1] and "anything else" not in llm.systems[-1].split("Caller continued")[1]
        assert len(workflow.calls) == 2
        assert brain._pending_workflow_close is not None and not endings(brain)
        await played(brain)
        await brain._workflow_close_task
        assert len(endings(brain)) == 1
    finally:
        await cleanup(brain)


async def test_the_second_follow_up_always_closes_even_when_the_decision_wants_to_continue():
    brain, _, llm = await closed_call()
    try:
        brain._take_decision.return_value = ConversationDecision(
            next_action="clarify", needs_clarification=True, confidence=1, source="llm",
        )
        brain._cancel_workflow_close("caller_interruption")
        await brain._handle_turn("Achha ek baat batao")
        assert brain._pending_workflow_close is None            # one clarification is allowed
        assert "Do not say goodbye" in llm.systems[-1]
        await brain._handle_turn("Wo jo pehle bola tha na")
        assert brain._pending_workflow_close is not None        # the second reply closes
        assert brain._closing_followups == 2
    finally:
        await cleanup(brain)


async def test_repeating_the_same_answer_confirms_the_outcome_instead_of_contesting_it():
    brain, workflow, llm = await closed_call()
    try:
        brain._goal_policy = brain._goal_policy.model_copy(update={})
        brain._take_decision.return_value = ConversationDecision(
            next_action="continue_workflow", confidence=0.98, source="llm", option="no",
            correction={"evidence": "nahi chahiye", "slots": ["outcome"]},
        )
        brain._cancel_workflow_close("caller_interruption")
        before = len(spoken(brain))
        await brain._handle_turn("haan nahi chahiye ab")
        assert brain._recorder.workflow_slots["outcome"] == "cancel_no_service"
        assert CORRECTION_STATE_KEY not in brain._recorder.call_state
        assert "workflow_outcome_contested" not in brain._recorder.event_kinds()
        assert "workflow_closing_reaffirmed" in brain._recorder.event_kinds()
        assert spoken(brain)[before:] == [TAIL] and llm.systems == []
        workflow.rollback_last_turn.assert_not_called()
        assert brain._pending_workflow_close is not None
    finally:
        await cleanup(brain)


async def test_a_changed_answer_reopens_the_flow_at_that_question():
    brain, workflow, llm = await closed_call()
    try:
        reopened = {**consumed_result("ठीक है, क्या मैं request forward कर दूँ?"), "nodePrompt": "forward?",
                    "slots": {}, "answeredOption": "yes"}
        workflow.result = reopened
        brain._take_decision.return_value = ConversationDecision(
            next_action="continue_workflow", confidence=0.98, source="llm", option="yes",
        )
        brain._cancel_workflow_close("caller_interruption")
        await brain._handle_turn("नहीं नहीं, cancel मत करो, मुझे service चाहिए")
        workflow.rollback_last_turn.assert_awaited_once()
        assert len(workflow.calls) == 3
        assert workflow.calls[-1]["answer_option"] == "yes" and workflow.calls[-1]["answer_option_judged"] is True
        assert brain._active_workflow == WORKFLOW and brain._completed_workflow is None
        assert "outcome" not in brain._recorder.workflow_slots
        assert "workflow_reopened_after_closing" in brain._recorder.event_kinds()
        assert "workflow_outcome_contested" not in brain._recorder.event_kinds()
        assert brain._pending_workflow_close is None and not endings(brain) and llm.systems == []
    finally:
        await cleanup(brain)


async def test_a_turn_that_ran_a_tool_is_never_reopened():
    brain, workflow, _ = await closed_call(side_effects=True)
    try:
        assert "answer_options" not in brain._orchestration_state()
        brain._take_decision.return_value = ConversationDecision(
            next_action="answer", signal="question", confidence=0.98, source="llm", option="yes",
        )
        brain._cancel_workflow_close("caller_interruption")
        await brain._handle_turn("नहीं नहीं, मुझे service चाहिए")
        workflow.rollback_last_turn.assert_not_called()
        assert brain._completed_workflow == WORKFLOW and len(workflow.calls) == 2
    finally:
        await cleanup(brain)


async def test_a_remark_after_the_closing_never_blanks_the_recorded_outcome():
    brain, workflow, llm = await closed_call()
    try:
        brain._take_decision.return_value = ConversationDecision(
            next_action="continue_workflow", confidence=0.98, source="llm",
            correction={"evidence": "kharab hi ho gaya hai", "slots": ["outcome"]},
        )
        brain._cancel_workflow_close("caller_interruption")
        await brain._handle_turn("kharab hi ho gaya hai wo to")
        assert brain._recorder.workflow_slots["outcome"] == "cancel_no_service"
        assert CORRECTION_STATE_KEY not in brain._recorder.call_state
        assert "workflow_outcome_contested" not in brain._recorder.event_kinds()
        workflow.rollback_last_turn.assert_not_called()
        assert len(llm.systems) == 1 and brain._pending_workflow_close is not None
    finally:
        await cleanup(brain)


async def test_a_workflow_that_declares_no_bound_keeps_the_previous_follow_up_behaviour():
    brain, _, llm = await closed_call(followups=None)
    try:
        assert brain._closing_followup_limit is None and brain._terminal_farewell_tail == ""
        brain._take_decision.return_value = ConversationDecision(
            next_action="continue_workflow", confidence=1, source="llm",
        )
        brain._cancel_workflow_close("caller_interruption")
        before = len(spoken(brain))
        await brain._handle_turn("Ek baat aur")
        assert spoken(brain)[before:] == ["Technician 48 घंटे में contact करेंगे।"]
        assert "ask whether they need anything else" in llm.systems[-1]
        assert brain._pending_workflow_close is None
    finally:
        await cleanup(brain)
