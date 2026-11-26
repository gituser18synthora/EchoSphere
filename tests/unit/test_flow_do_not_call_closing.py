"""A "do not call me again" request ends on the workflow's own closing.

The platform's acknowledgement says the number is put on a do-not-call list.
The platform keeps no such list: it records a disposition on the call. A
workflow that declares ``doNotCallNode`` (start node config) answers the
request with its own authored closing and outcome instead; every other
workflow keeps the platform acknowledgement.
"""
from unittest.mock import AsyncMock

from pipecat.frames.frames import TextFrame

from shared.orchestration.phrases import canned
from shared.orchestration.router import RouteDecision, RouteKind
from tests.unit.test_brain_workflow_routing import _LLMStub, _WorkflowStub, consumed_result, make_brain
from tests.unit.test_workflow_closing import cleanup, endings, played, short_close_grace  # noqa: F401

WORKFLOW = "service_flow"
QUESTION = "क्या आपको service की requirement है?"
OWN_CLOSING = ("बार-बार call आने के लिए माफ़ी चाहूँगी। मैं आपकी बात note करके आगे भेज रही हूँ। "
               "Haier को समय देने के लिए धन्यवाद, आपका दिन शुभ रहे।")
OPTIONS = [{"id": "yes", "meaning": "needs service"}, {"id": "no", "meaning": "does not need service"}]
REQUEST = "अब दोबारा कॉल मत करना।"


async def call_at_question(*, declares: bool):
    asked = {**consumed_result(QUESTION), "nodePrompt": QUESTION, "awaitingOptions": OPTIONS,
             "awaitingSignals": ["affirm", "refusal"], "awaitingEdges": []}
    workflow = _WorkflowStub(asked)
    workflow.call_control_target = AsyncMock(return_value="m_stop" if declares else None)
    llm = _LLMStub()
    brain = make_brain(workflow_engine=workflow, llm=llm)
    brain._complete_endpoint = brain._short_reply_endpoint = 0.01
    brain._recorder.call_state = {}
    await brain._handle_workflow(RouteDecision(kind=RouteKind.WORKFLOW, action=WORKFLOW), "haan ji", 0)
    workflow.result = {
        **consumed_result(OWN_CLOSING, done=True), "slots": {"outcome": "repeat_calls_noted"},
        "terminalSetSlots": ["outcome"], "callControlHandled": "do_not_call", "closingFollowups": 2,
    }
    return brain, workflow, llm


def spoken(brain):
    return " ".join(frame.text for frame in brain._pushed if isinstance(frame, TextFrame))


async def test_the_flow_that_declares_a_closing_speaks_it_and_records_the_request():
    brain, workflow, llm = await call_at_question(declares=True)
    try:
        await brain._handle_turn(REQUEST)
        assert workflow.calls[-1]["call_control"] == "do_not_call"
        assert workflow.calls[-1]["user_text"] == REQUEST
        said = spoken(brain)
        assert "बार-बार call आने के लिए माफ़ी" in said
        assert canned("dnc_ack", "hi-IN") not in said
        assert llm.systems == []                      # an authored line, no model turn
        assert brain._recorder.disposition == "do_not_call"
        assert brain._recorder.call_state["last_disposition"] == "do_not_call"
        assert brain._recorder.workflow_slots.get("outcome") == "repeat_calls_noted"
        assert brain._completed_workflow == WORKFLOW and brain._pending_workflow_close is not None
        # nothing said after that closing re-enters the flow at the question
        assert brain._terminal_turn["options"] == []
        assert not brain._terminal_turn_reanswerable()
    finally:
        await cleanup(brain)


async def test_a_flow_without_such_a_closing_keeps_the_platform_acknowledgement():
    brain, workflow, _ = await call_at_question(declares=False)
    try:
        calls = len(workflow.calls)
        await brain._handle_turn(REQUEST)
        assert len(workflow.calls) == calls           # the flow was not run for it
        assert canned("dnc_ack", "hi-IN") in spoken(brain)
        assert brain._recorder.disposition == "do_not_call" and brain._closing
    finally:
        await cleanup(brain)


async def test_an_engine_without_the_lookup_keeps_the_platform_acknowledgement():
    brain, workflow, _ = await call_at_question(declares=True)
    try:
        del workflow.call_control_target              # an engine build that predates the lookup
        calls = len(workflow.calls)
        await brain._handle_turn(REQUEST)
        assert len(workflow.calls) == calls
        assert canned("dnc_ack", "hi-IN") in spoken(brain)
    finally:
        await cleanup(brain)


async def test_the_flow_is_asked_once_in_the_background_and_the_segment_handler_never_waits():
    """The segment handler must not wait for the lookup: an interruption
    cancels a waiting handler and the caller's words with it. The answer is
    learnt in the background when the flow is entered; until it is known the
    platform acknowledgement applies, exactly as for a flow that declares
    nothing."""
    import asyncio

    brain, workflow, _ = await call_at_question(declares=True)
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert workflow.call_control_target.await_count == 1
        assert workflow.call_control_target.await_args.kwargs["control"] == "do_not_call"
        assert brain._known_do_not_call_flow() == WORKFLOW          # no await: the cached answer
        assert await brain._do_not_call_flow() == WORKFLOW
        assert workflow.call_control_target.await_count == 1        # never asked twice
        brain._flow_dnc_targets.clear()                             # not learnt yet
        assert brain._known_do_not_call_flow() is None
    finally:
        await cleanup(brain)


async def test_said_after_the_closing_line_the_recorded_outcome_is_kept():
    brain, workflow, _ = await call_at_question(declares=True)
    try:
        route = RouteDecision(kind=RouteKind.WORKFLOW, action=WORKFLOW)
        workflow.result = {**consumed_result("ठीक है। धन्यवाद, आपका दिन शुभ रहे।", done=True),
                           "slots": {"outcome": "redispatch"}, "terminalSetSlots": ["outcome"],
                           "answeredOption": "yes", "closingFollowups": 2}
        await brain._handle_workflow(route, "haan", 0)
        assert brain._completed_workflow == WORKFLOW
        # the engine keeps an outcome that is already recorded (unit-tested there)
        workflow.result = {**consumed_result(OWN_CLOSING, done=True), "slots": {"outcome": "redispatch"},
                           "terminalSetSlots": [], "callControlHandled": "do_not_call", "closingFollowups": 2}
        await brain._handle_turn(REQUEST)
        assert workflow.calls[-1]["call_control"] == "do_not_call"
        assert "बार-बार call आने के लिए माफ़ी" in spoken(brain)
        assert canned("dnc_ack", "hi-IN") not in spoken(brain)
        assert brain._recorder.workflow_slots.get("outcome") == "redispatch"
        assert brain._recorder.disposition == "do_not_call"
        assert brain._pending_workflow_close is not None
    finally:
        await cleanup(brain)
