"""Phase 4 Change 1: short answers to a paused workflow question skip the
decision LLM (llm_settings.deterministic_answers, default off).

The resolver reads "हाँ" / "जी नहीं" / "पहला वाला" / "दो" / "कल" / "शाम पाँच
बजे" against the pending ask; anything with more in it (a question, a hold,
an unknown word, both polarities) stays with the decision LLM. In the brain
the rule only acts inside an active workflow and never on a collections bot.
"""

from types import SimpleNamespace

import pytest
from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.deterministic_answers import (
    RESOLVER_CHOICE,
    RESOLVER_DATETIME,
    RESOLVER_NUMBER,
    RESOLVER_YES_NO,
    resolve_short_answer,
    yes_no_polarity,
)
from shared.orchestration.workflow_engine import _awaiting_edge_signals
from shared.runtime_context import build_runtime_context
from voice_runtime.brain import ConversationBrain

from tests.unit.test_agentic_orchestration import GRACE, _AgenticLLMStub, _RecorderStub
from tests.unit.test_brain_collection_policy import turn


# ── the resolver ─────────────────────────────────────────────────────────────

class TestYesNo:
    @pytest.mark.parametrize("text", [
        "हाँ।", "हाँ जी", "हांजी।", "जी।", "हाँ हाँ सही है।", "ठीक है।", "Yes.",
        "Okay", "हा.", "हाँ मैम।", "haan ji",
    ])
    def test_a_plain_yes(self, text):
        answer = resolve_short_answer(text, "yes_no")
        assert (answer.value, answer.signal, answer.resolver) == ("yes", "affirm", RESOLVER_YES_NO)

    @pytest.mark.parametrize("text", ["नहीं।", "जी नहीं", "नहीं नहीं।", "नहीं है।", "No, sir.", "nahi ji"])
    def test_a_plain_no(self, text):
        answer = resolve_short_answer(text, "yes_no")
        assert (answer.value, answer.signal) == ("no", "refusal")

    @pytest.mark.parametrize("text", [
        "हाँ ना",                    # both polarities ("na" is also a softener)
        "हाँ नहीं",
        "हाँ बोलो।",                 # a go-ahead is more than a yes
        "नहीं पता",                  # an unknown content word
        "हम्म।",                     # read as 'clarify' by the model on demo
        "hmm",
        "हाँ?",                      # a question
        "क्या?",
        "ek minute ruko",           # a hold
        "नहीं, मैं नहीं हूँ",         # more than four words
        "haan ji haan ji haan",
        "",
    ])
    def test_anything_more_stays_with_the_model(self, text):
        assert resolve_short_answer(text, "yes_no") is None

    def test_the_courtesy_ji_goes_with_either_polarity(self):
        assert yes_no_polarity("जी") == "yes"
        assert yes_no_polarity("जी नहीं") == "no"
        assert yes_no_polarity("नहीं जी") == "no"


class TestValues:
    OPTIONS = ("morning", "evening")

    @pytest.mark.parametrize("text,value", [
        ("पहला वाला", "morning"), ("दूसरा", "evening"), ("हाँ, पहला वाला", "morning"),
        ("evening", "evening"), ("Evening sir", "evening"),
    ])
    def test_a_choice(self, text, value):
        answer = resolve_short_answer(text, "choice", options=self.OPTIONS,
                                      question="morning ya evening?")
        assert (answer.value, answer.resolver, answer.signal) == (value, RESOLVER_CHOICE, None)

    @pytest.mark.parametrize("text", ["शाम", "नहीं", "पहला नहीं दूसरा", "कोई भी"])
    def test_a_choice_it_cannot_place(self, text):
        assert resolve_short_answer(text, "choice", options=self.OPTIONS) is None

    @pytest.mark.parametrize("text,value", [("दो", "2"), ("पचास", "50"), ("2", "2"), ("जी, तीन", "3")])
    def test_a_number(self, text, value):
        answer = resolve_short_answer(text, "number")
        assert (answer.value, answer.resolver) == (value, RESOLVER_NUMBER)

    @pytest.mark.parametrize("text", ["दो तीन", "teen saal", "नहीं"])
    def test_a_number_it_cannot_place(self, text):
        assert resolve_short_answer(text, "number") is None

    @pytest.mark.parametrize("text", ["कल", "कल।", "परसों को", "Monday", "अगले सोमवार"])
    def test_a_day(self, text):
        assert resolve_short_answer(text, "date").resolver == RESOLVER_DATETIME

    @pytest.mark.parametrize("text", ["कल नहीं", "कल शाम", "कल या परसों", "next week"])
    def test_a_day_it_cannot_place(self, text):
        assert resolve_short_answer(text, "date") is None

    def test_a_time_must_be_exact(self):
        assert resolve_short_answer("शाम पाँच बजे", "time").value == "शाम पाँच बजे"
        assert resolve_short_answer("5 बजे", "time") is None      # morning or evening?

    @pytest.mark.parametrize("kind", ["free_text", "none"])
    def test_open_questions_are_never_resolved(self, kind):
        assert resolve_short_answer("हाँ", kind) is None


def test_the_engine_reports_a_paused_hubs_edge_signals():
    definition = {
        "nodes": [{"id": "hub", "kind": "intent"}, {"id": "a", "kind": "end"},
                  {"id": "b", "kind": "end"}, {"id": "ask", "kind": "ask"}],
        "edges": [{"from": "hub", "to": "a", "label": "haan / yes"},
                  {"from": "hub", "to": "b", "label": "nahi / no"},
                  {"from": "hub", "to": "missing", "label": "baad mein"}],
    }
    assert _awaiting_edge_signals(definition, definition["nodes"][0]) == ["affirm", "refusal"]
    assert _awaiting_edge_signals(definition, definition["nodes"][3]) is None
    assert _awaiting_edge_signals(definition, None) is None


# ── the brain ────────────────────────────────────────────────────────────────

QUESTION = "क्या technician आपके घर आए थे?"


class _Workflows:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def handle_turn_detailed(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.pop(0)


def _result(*, ask=None, signals=None, reply="ठीक है, आगे बढ़ते हैं। क्या आप कल उपलब्ध हैं?"):
    return {
        "reply": reply, "done": False, "status": "collecting", "source": "definition",
        "workflowId": "wf_1", "trace": ["n_next"], "slots": {}, "handoffQueue": None,
        "offScript": False, "nodePrompt": "क्या आप कल उपलब्ध हैं?", "signal": None,
        "awaitingAsk": ask, "awaitingSignals": signals, "spokenNodes": ["n_next"],
    }


def _brain(llm, workflows=None, *, enabled=True, runtime_context=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Support", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Support.",
        intents=[{"name": "technician_visit", "samples": ["technician"],
                  "route": "workflow:wf_1", "confidence_threshold": 0.6}],
        llm={"provider": "openai", "model": "gpt-x",
             "settings": {"deterministic_answers": True} if enabled else {}},
    )
    brain = ConversationBrain(
        config=config, llm=llm, recorder=_RecorderStub(),
        workflow_engine=workflows or _Workflows([_result()]),
        runtime_context=runtime_context,
        finalize_grace=GRACE, complete_endpoint=GRACE,
    )
    brain._pushed, brain._notified = [], []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    import asyncio

    def _create_task(coro, name=None):
        return asyncio.get_event_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.push_frame, brain._notify_client = _push, _notify
    brain.create_task, brain.cancel_task = _create_task, _cancel_task
    return brain


def _pause_at(brain, *, ask=None, hub=None):
    brain._active_workflow = "wf_1"
    brain._workflow_ever_routed = True
    brain._pending_workflow_question = QUESTION
    brain._last_bot_reply = QUESTION
    brain._pending_ask_shape = ask
    brain._pending_hub_signals = frozenset(hub) if hub is not None else None


async def _heard(brain):
    """The reply played: the next caller turn is a new turn, not a merge."""
    await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


def _events(brain, kind):
    return [data for k, data in brain._recorder.events if k == kind]


class TestTheBrainSkipsTheDecision:
    async def test_a_yes_at_a_yes_no_ask_takes_the_workflow_edge(self):
        llm, workflows = _AgenticLLMStub([]), _Workflows([_result()])
        brain = _brain(llm, workflows)
        _pause_at(brain, ask={"kind": "yes_no", "options": []})
        await turn(brain, "हाँ जी।")
        assert llm.generate_calls == []                        # no decision call
        assert workflows.calls[0]["signal"] == "affirm"
        assert _events(brain, "decision_skipped_deterministic") == [{
            "resolver": RESOLVER_YES_NO, "ask_kind": "yes_no", "signal": "affirm", "value": "yes",
        }]
        assert _events(brain, "orchestration_turn")[0]["interpretation"] == "deterministic"

    async def test_a_no_at_a_hub_with_a_refusal_edge(self):
        llm, workflows = _AgenticLLMStub([]), _Workflows([_result()])
        brain = _brain(llm, workflows)
        _pause_at(brain, hub={"affirm", "refusal", "callback"})
        await turn(brain, "नहीं।")
        assert llm.generate_calls == []
        assert workflows.calls[0]["signal"] == "refusal"

    async def test_a_no_at_a_hub_without_a_refusal_edge_asks_the_model(self):
        llm = _AgenticLLMStub([{"signal": "refusal", "scope": "in_scope",
                                "next_action": "continue_workflow"}])
        brain = _brain(llm, _Workflows([_result()]))
        _pause_at(brain, hub={"affirm"})
        await turn(brain, "नहीं।")
        assert len(llm.generate_calls) == 1
        assert _events(brain, "decision_skipped_deterministic") == []

    async def test_a_value_reaches_the_workflow_as_said(self):
        llm, workflows = _AgenticLLMStub([]), _Workflows([_result()])
        brain = _brain(llm, workflows)
        _pause_at(brain, ask={"kind": "date", "options": []})
        await turn(brain, "कल।")
        assert llm.generate_calls == []
        assert workflows.calls[0]["user_text"] == "कल।"
        assert workflows.calls[0]["signal"] is None
        # A spoken day is never logged.
        assert _events(brain, "decision_skipped_deterministic")[0]["value"] is None

    async def test_the_next_hub_is_tracked_from_the_engine(self):
        llm = _AgenticLLMStub([])
        workflows = _Workflows([_result(signals=["affirm", "refusal"]), _result()])
        brain = _brain(llm, workflows)
        _pause_at(brain, ask={"kind": "yes_no", "options": []})
        await turn(brain, "हाँ।")
        assert brain._pending_hub_signals == {"affirm", "refusal"}
        await _heard(brain)
        await turn(brain, "नहीं जी।")
        assert llm.generate_calls == []
        assert workflows.calls[1]["signal"] == "refusal"

    async def test_more_than_an_answer_asks_the_model(self):
        llm = _AgenticLLMStub([{"signal": "question", "scope": "in_scope",
                                "next_action": "answer"}])
        brain = _brain(llm, _Workflows([_result()]))
        _pause_at(brain, ask={"kind": "yes_no", "options": []})
        await turn(brain, "हाँ पर कब आएंगे?")
        assert len(llm.generate_calls) == 1

    async def test_off_by_default(self):
        llm = _AgenticLLMStub([{"signal": "affirm", "scope": "in_scope",
                                "next_action": "continue_workflow"}])
        brain = _brain(llm, _Workflows([_result()]), enabled=False)
        _pause_at(brain, ask={"kind": "yes_no", "options": []})
        await turn(brain, "हाँ।")
        assert len(llm.generate_calls) == 1
        assert _events(brain, "decision_skipped_deterministic") == []


class TestWhereItNeverActs:
    def test_outside_a_workflow(self):
        brain = _brain(_AgenticLLMStub([]))
        brain._last_bot_reply = "क्या आप अभी बात कर सकते हैं?"
        assert brain._pending_ask().kind == "yes_no"
        assert brain._deterministic_answer("हाँ") is None

    def test_on_a_collections_bot(self):
        brain = _brain(_AgenticLLMStub([]))
        _pause_at(brain, ask={"kind": "yes_no", "options": []})
        assert brain._deterministic_answer("हाँ") is not None
        brain._policy = SimpleNamespace(awaiting_identity=False, verified=True,
                                        awaiting_reference=False, transaction_reference=None)
        assert brain._deterministic_answer("हाँ") is None

    def test_on_a_collections_runtime_context(self):
        runtime_context = build_runtime_context(
            tenant_id="tn-x", bot_id="bot-x", payload={"customer_name": "Ramesh"},
            payload_source="test", domain_policy="collections",
        )
        brain = _brain(_AgenticLLMStub([]), runtime_context=runtime_context)
        brain._policy = None
        _pause_at(brain, ask={"kind": "yes_no", "options": []})
        assert brain._deterministic_answer("हाँ") is None

    def test_on_another_route_or_an_identifier_ask(self):
        from shared.orchestration.router import RouteDecision, RouteKind

        brain = _brain(_AgenticLLMStub([]))
        _pause_at(brain, ask={"kind": "yes_no", "options": []})
        assert brain._deterministic_answer("हाँ", RouteDecision(kind=RouteKind.CHAT)) is None
        brain._identifier_capture = object()
        assert brain._deterministic_answer("हाँ") is None

    def test_no_prefetch_for_an_answer_the_rule_takes(self):
        brain = _brain(_AgenticLLMStub([]))
        _pause_at(brain, ask={"kind": "yes_no", "options": []})
        brain._pending_segments = ["हाँ जी"]
        brain._start_decision_prefetch()
        assert brain._decision_prefetch is None

    async def test_a_rewound_turn_restores_the_hub(self):
        class _Rewindable(_Workflows):
            async def rollback_last_turn(self, **kwargs):
                return True

        workflows = _Rewindable([_result(ask={"kind": "number", "options": []})])
        brain = _brain(_AgenticLLMStub([]), workflows)
        _pause_at(brain, hub={"affirm", "refusal"})
        await turn(brain, "हाँ।")
        assert brain._pending_hub_signals is None          # the engine moved on to an ask
        await brain._rollback_open_turn(("हाँ।", None, ("wf_1", "wf_1", "हाँ।")))
        assert brain._pending_hub_signals == {"affirm", "refusal"}
