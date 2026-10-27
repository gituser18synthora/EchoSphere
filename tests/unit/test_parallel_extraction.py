"""Phase 4 Change 4: the MDND pipeline in parallel
(llm_settings.parallel_extraction, default off).

- the workflow's semantic extraction starts at dispatch, together with the
  decision, and the engine uses it only when the turn reaches the same
  workflow with the same inputs (else it extracts as before);
- a constrained grounded rewrite streams into TTS sentence by sentence; the
  last sentence waits for the whole reply to validate.
"""

import asyncio
import json
import runpy
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import MemorySaver
from pipecat.frames.frames import TextFrame

import shared.orchestration.workflow_engine as wfe
from shared.bot_config import ResolvedBotConfig
from shared.providers.base import LLMResult, ProviderError
from voice_runtime.brain import ConversationBrain

from tests.unit.test_agentic_orchestration import GRACE, _RecorderStub
from tests.unit.test_brain_collection_policy import turn

CONTEXT = {"mdnd_deduction_amount": "400 rupees", "mdnd_order_last4": "9203",
           "mdnd_deduction_date": "4 August"}


# ── the engine ───────────────────────────────────────────────────────────────

@pytest.fixture
def mdnd(monkeypatch):
    config = runpy.run_path("zepto/setup/06_single_bots.py")
    nodes, edges = config["build_mdnd_workflow"]()
    for node in nodes:
        if node["id"] == "n_start":
            node["config"]["semanticExtraction"] = "llm"
            node["config"].pop("semanticExtractionLanguages", None)
    definition = {"id": "wf_semantic_mdnd", "name": "MDND", "version": 1,
                  "nodes": nodes, "edges": edges}
    monkeypatch.setattr(wfe, "load_workflow_definition", lambda *args: definition)

    async def memory(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer
    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", memory)
    llm = AsyncMock()
    llm.generate.return_value = LLMResult(text=json.dumps({
        "patch": {"reached_location": "yes"}, "evidence": {"reached_location": "location par gaya"},
    }))
    return wfe.WorkflowEngine(), llm, definition


TURN = dict(tenant_id="tn_x", bot_id="bot_x", workflow_name="mdnd", language="hi-IN")
HISTORY = [{"role": "user", "content": "location par gaya tha"}]


async def _ahead(engine, llm, session, text):
    return await engine.start_extraction(
        session_id=session, user_text=text, history=HISTORY, llm=llm, **TURN,
    )


def _future(value):
    future = asyncio.get_event_loop().create_future()
    future.set_result(value)
    return future


class TestTheEngine:
    async def test_an_extraction_started_ahead_is_used(self, mdnd):
        engine, llm, _ = mdnd
        await engine.handle_turn_detailed(session_id="a", user_text="हाँ", llm=llm,
                                          context_values=CONTEXT, history=[], **TURN)
        before = llm.generate.await_count
        prepared = await _ahead(engine, llm, "a", "location par gaya tha")
        assert prepared is not None
        result = await engine.handle_turn_detailed(
            session_id="a", user_text="location par gaya tha", llm=llm, context_values=CONTEXT,
            history=HISTORY, extraction_ahead=_future(prepared), **TURN,
        )
        assert result["extractionAhead"] == "used"
        assert llm.generate.await_count - before == 4          # the four readers, once

    async def test_the_same_slots_as_extracting_after_the_decision(self, mdnd):
        engine, llm, _ = mdnd
        for session in ("seq", "par"):
            await engine.handle_turn_detailed(session_id=session, user_text="हाँ", llm=llm,
                                              context_values=CONTEXT, history=[], **TURN)
        sequential = await engine.handle_turn_detailed(
            session_id="seq", user_text="location par gaya tha", llm=llm,
            context_values=CONTEXT, history=HISTORY, **TURN,
        )
        prepared = await _ahead(engine, llm, "par", "location par gaya tha")
        parallel = await engine.handle_turn_detailed(
            session_id="par", user_text="location par gaya tha", llm=llm, context_values=CONTEXT,
            history=HISTORY, extraction_ahead=_future(prepared), **TURN,
        )
        assert parallel["slots"] == sequential["slots"]
        assert parallel["reply"] == sequential["reply"]
        assert sequential["extractionAhead"] is None

    async def test_changed_inputs_extract_again(self, mdnd):
        engine, llm, _ = mdnd
        await engine.handle_turn_detailed(session_id="b", user_text="हाँ", llm=llm,
                                          context_values=CONTEXT, history=[], **TURN)
        before = llm.generate.await_count
        prepared = await _ahead(engine, llm, "b", "location par gaya tha")
        result = await engine.handle_turn_detailed(
            session_id="b", user_text="location par gaya tha, call bhi kiya", llm=llm,
            context_values=CONTEXT, history=HISTORY, extraction_ahead=_future(prepared), **TURN,
        )
        assert result["extractionAhead"] == "mismatch"
        # The ahead readers (instant in this stub) plus a fresh extraction.
        assert llm.generate.await_count - before == 8
        assert prepared.task.cancelled() or prepared.task.done()

    async def test_a_flow_without_llm_extraction_starts_nothing(self, mdnd, monkeypatch):
        engine, llm, definition = mdnd
        for node in definition["nodes"]:
            node.get("config", {}).pop("semanticExtraction", None)
        assert await _ahead(engine, llm, "c", "हाँ") is None
        assert llm.generate.await_count == 0


# ── the brain ────────────────────────────────────────────────────────────────

DECISION_S = 0.2


class _DecisionLLM:
    def __init__(self, decision=None, chunks=(), gate=None, fail=False):
        self.decision = decision or {"scope": "in_scope", "next_action": "continue_workflow"}
        self.chunks, self.gate, self.fail = list(chunks), gate, fail
        self.streams = []
        self.generate_calls = 0
        self.decided_at = None
        self.last_stream_usage = None

    async def generate(self, messages, *, system=None, temperature=None,
                       max_tokens=None, tools=None, response_format=None):
        self.generate_calls += 1
        await asyncio.sleep(DECISION_S)
        self.decided_at = asyncio.get_event_loop().time()
        payload = self.decision

        class _Result:
            text = json.dumps(payload)
            input_tokens = 100
            output_tokens = 40

        return _Result()

    def stream(self, messages, *, system, temperature, max_tokens):
        self.streams.append(system)

        async def _gen():
            if self.fail:
                raise ProviderError("stub", "timeout", "down")
            for index, chunk in enumerate(self.chunks):
                if index == 1 and self.gate is not None:
                    await self.gate.wait()
                yield chunk

        return _gen()


class _Workflows:
    def __init__(self, result):
        self.result = result
        self.started = []
        self.calls = []
        self.prepared = None

    async def start_extraction(self, **kwargs):
        self.started.append((asyncio.get_event_loop().time(), kwargs))
        self.prepared = wfe.PreparedExtraction("k", asyncio.ensure_future(asyncio.sleep(10)))
        return self.prepared

    async def handle_turn_detailed(self, **kwargs):
        # The history as it is at call time (the brain keeps appending to it).
        self.calls.append((asyncio.get_event_loop().time(),
                           {**kwargs, "history": [dict(m) for m in kwargs.get("history") or []]}))
        ahead = kwargs.get("extraction_ahead")
        if ahead is not None:
            (await ahead).task.cancel()
            return {**self.result, "extractionAhead": "used"}
        return self.result


def _result(reply="ठीक है। क्या आप customer की location पर पहुँचे थे?", **extra):
    return {"reply": reply, "done": False, "status": "collecting", "source": "definition",
            "workflowId": "wf_1", "trace": ["n_x"], "slots": {}, "handoffQueue": None,
            "offScript": False, "nodePrompt": "क्या आप customer की location पर पहुँचे थे?",
            "signal": None, "spokenNodes": ["n_x"], **extra}


def _brain(llm, workflows, *, enabled=True) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="MDND", version="v1",
        published=True, language="hi-IN", languages=["hi-IN", "en-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are MDND support.",
        intents=[{"name": "mdnd_concern", "samples": ["mdnd"], "route": "workflow:wf_1"}],
        llm={"provider": "openai", "model": "gpt-x",
             "settings": {"parallel_extraction": True} if enabled else {}},
    )
    brain = ConversationBrain(config=config, llm=llm, recorder=_RecorderStub(),
                              workflow_engine=workflows,
                              finalize_grace=GRACE, complete_endpoint=GRACE)
    brain._pushed, brain._notified = [], []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.push_frame, brain._notify_client = _push, _notify
    brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)
    brain.cancel_task = _cancel_task
    brain._active_workflow = "wf_1"
    brain._workflow_ever_routed = True
    brain._pending_workflow_question = "बताइए — क्या हुआ था?"
    brain._last_bot_reply = "बताइए — क्या हुआ था?"
    return brain


async def speak(brain, text):
    await turn(brain, text)
    await asyncio.sleep(DECISION_S + 0.1)


def _events(brain, kind):
    return [data for k, data in brain._recorder.events if k == kind]


class TestTheBrainStartsItWithTheDecision:
    async def test_the_extraction_starts_before_the_decision_returns(self):
        llm, workflows = _DecisionLLM(), _Workflows(_result())
        brain = _brain(llm, workflows)
        await speak(brain, "मैं location पर गया था और customer को call किया था")
        assert len(workflows.started) == 1 and len(workflows.calls) == 1
        started_at, kwargs = workflows.started[0]
        handled_at, call = workflows.calls[0]
        assert started_at < llm.decided_at <= handled_at        # in parallel with the decision
        assert kwargs["user_text"] == call["user_text"]
        assert kwargs["history"] == call["history"]
        assert kwargs["workflow_name"] == call["workflow_name"]
        assert _events(brain, "extraction_ahead") == [{"outcome": "used"}]

    async def test_off_by_default(self):
        llm, workflows = _DecisionLLM(), _Workflows(_result())
        brain = _brain(llm, workflows, enabled=False)
        await speak(brain, "मैं location पर गया था")
        assert workflows.started == []
        assert "extraction_ahead" not in workflows.calls[0][1]

    async def test_a_turn_that_never_reaches_the_workflow_cancels_it(self):
        llm, workflows = _DecisionLLM({"signal": "hold", "scope": "in_scope",
                                       "next_action": "answer"}), _Workflows(_result())
        brain = _brain(llm, workflows)
        await speak(brain, "ek minute ruko")
        assert workflows.calls == []
        await asyncio.sleep(0)
        assert workflows.prepared.task.cancelled()
        assert _events(brain, "extraction_ahead") == [{"outcome": "unused"}]


GROUNDED = dict(responseMode="llm_grounded", responseDirectives=["Read the ticket facts."],
                responseMustInclude=["क्या हुआ था"])
SCRIPT = "आपके ticket पर MDND का deduction दिख रहा है। बताइए — क्या हुआ था?"


class TestTheGroundedRewriteStreams:
    def _brain(self, llm):
        result = _result(SCRIPT, **GROUNDED)
        result["nodePrompt"] = "बताइए — क्या हुआ था?"
        return _brain(llm, _Workflows(result))

    async def test_the_first_sentence_plays_while_the_rest_generates(self):
        gate = asyncio.Event()
        # A sentence is spoken once the next one has started.
        llm = _DecisionLLM(chunks=["आपके ticket पर MDND का deduction 400 रुपये का है। बताइए",
                                   " — क्या हुआ था?"], gate=gate)
        brain = self._brain(llm)
        task = asyncio.ensure_future(speak(brain, "हाँ बोलो"))
        for _ in range(60):
            await asyncio.sleep(0.01)
            if any(isinstance(f, TextFrame) for f in brain._pushed):
                break
        texts = [f.text for f in brain._pushed if isinstance(f, TextFrame)]
        assert texts == ["आपके ticket पर MDND का deduction 400 रुपये का है। "]
        gate.set()
        await task
        assert brain._history[-1]["content"] == (
            "आपके ticket पर MDND का deduction 400 रुपये का है। बताइए — क्या हुआ था?")
        assert _events(brain, "workflow_reply_grounded")[-1]["mode"] == "constrained_stream"
        assert llm.generate_calls == 1                          # the decision only

    async def test_a_reply_that_fails_validation_after_a_sentence_ends_with_the_question(self):
        llm = _DecisionLLM(chunks=["आपके ticket पर MDND का deduction 400 रुपये का है। ",
                                   "मैं अभी check कर रहा हूँ।"])
        brain = self._brain(llm)
        await speak(brain, "हाँ बोलो")
        assert brain._history[-1]["content"] == (
            "आपके ticket पर MDND का deduction 400 रुपये का है। बताइए — क्या हुआ था?")
        assert _events(brain, "workflow_grounded_fallback")[-1]["reason"] == "validation_after_release"
        assert brain._reply_nodes == []

    async def test_a_provider_failure_speaks_the_authored_text(self):
        llm = _DecisionLLM(fail=True)
        brain = self._brain(llm)
        await speak(brain, "हाँ बोलो")
        assert brain._history[-1]["content"] == SCRIPT
        assert _events(brain, "workflow_grounded_fallback")[-1]["reason"] == "provider"

    async def test_off_the_rewrite_is_one_validated_generation(self):
        llm = _DecisionLLM()
        result = _result(SCRIPT, **GROUNDED)
        brain = _brain(llm, _Workflows(result), enabled=False)
        await speak(brain, "हाँ बोलो")
        assert llm.streams == []
        assert llm.generate_calls == 2                          # decision + rewrite
