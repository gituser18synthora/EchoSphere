"""Phase 4 Change 3: the reply LLM starts at dispatch, in parallel with the
decision (llm_settings.speculative_reply, default off).

The buffered reply reaches TTS only when the decision kept the turn a plain
in-scope chat reply AND the reply request built after the decision is the
one the speculation sent; otherwise it is cancelled and the turn generates
as before. Never on a collections bot, inside a workflow or while the
opening question is pending.
"""

import asyncio
import json

import pytest

from shared.bot_config import ResolvedBotConfig
from voice_runtime.brain import ConversationBrain
from voice_runtime.speculative_reply import SpeculativeReply

from tests.unit.test_agentic_orchestration import GRACE, _RecorderStub
from tests.unit.test_brain_collection_policy import bot_replies, turn


def _create_task(coro, name=None):
    return asyncio.get_event_loop().create_task(coro)


class _Stream:
    """Streams scripted chunks; ``gate`` holds the stream before its first
    chunk; ``fail`` raises instead of streaming."""

    def __init__(self, chunks=("ठीक", " है।"), gate=None, fail=None):
        self.chunks, self.gate, self.fail = list(chunks), gate, fail
        self.calls = []
        self.last_stream_usage = None

    def stream(self, messages, *, system, temperature, max_tokens):
        self.calls.append({"messages": [dict(m) for m in messages], "system": system})

        async def _gen():
            if self.gate is not None:
                await self.gate.wait()
            if self.fail is not None:
                raise self.fail
            for chunk in self.chunks:
                yield chunk

        return _gen()


async def _drain(speculation):
    return [chunk async for chunk in speculation.replay()]


class TestTheBuffer:
    def _speculation(self, llm):
        return SpeculativeReply(
            llm, messages=[{"role": "user", "content": "hi"}], system="S",
            temperature=0.3, max_tokens=100, assumed_route="chat",
            create_task=_create_task,
        )

    async def test_buffered_chunks_then_the_live_rest(self):
        gate = asyncio.Event()
        speculation = self._speculation(_Stream(("a", "b", "c"), gate=gate))
        await asyncio.sleep(0)
        assert speculation.chunks == [] and not speculation.done
        consumer = asyncio.ensure_future(_drain(speculation))
        await asyncio.sleep(0)
        gate.set()
        assert await consumer == ["a", "b", "c"]
        assert speculation.generated_chars == 3

    async def test_a_provider_failure_reaches_the_consumer(self):
        speculation = self._speculation(_Stream(fail=RuntimeError("upstream")))
        with pytest.raises(RuntimeError, match="upstream"):
            await _drain(speculation)

    async def test_a_consumer_that_stops_early_cancels_the_request(self):
        gate = asyncio.Event()
        speculation = self._speculation(_Stream(gate=gate))
        pending = asyncio.ensure_future(speculation.replay().__anext__())
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert speculation._task.cancelled()

    def test_only_the_identical_request_matches(self):
        speculation = self._speculation(_Stream())
        speculation.cancel()
        assert speculation.matches([{"role": "user", "content": "hi"}], "S", 0.3, 100)
        assert not speculation.matches([{"role": "user", "content": "hi"}], "S2", 0.3, 100)
        assert not speculation.matches([{"role": "user", "content": "ho"}], "S", 0.3, 100)
        assert not speculation.matches([{"role": "user", "content": "hi"}], "S", 0.3, 120)


# ── the brain ────────────────────────────────────────────────────────────────

DECISION_S = 0.25


class _LLM(_Stream):
    """Decisions (generate) take DECISION_S — still running at dispatch, as
    on a live call — and also wait on ``decide``."""

    def __init__(self, decisions, chunks=("जी, बिल्कुल।",)):
        super().__init__(chunks)
        self.decisions = list(decisions)
        self.decide = asyncio.Event()
        self.decide.set()
        self.generate_calls = 0

    async def generate(self, messages, *, system=None, temperature=None,
                       max_tokens=None, tools=None, response_format=None):
        self.generate_calls += 1
        await asyncio.sleep(DECISION_S)
        await self.decide.wait()
        payload = self.decisions.pop(0) if self.decisions else {"scope": "in_scope", "next_action": "answer"}

        class _Result:
            text = json.dumps(payload)
            input_tokens = 150
            output_tokens = 60

        return _Result()


def _brain(llm, *, enabled=True, goal_policy=None, workflows=None, greeting="") -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Survey", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You run a feedback survey.",
        greeting=greeting, goal_policy=goal_policy or {},
        intents=[{"name": "feedback", "samples": ["feedback dena hai"], "route": ""}],
        llm={"provider": "openai", "model": "gpt-x",
             "settings": {"speculative_reply": True} if enabled else {}},
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
    brain.create_task, brain.cancel_task = _create_task, _cancel_task
    brain._last_bot_reply = "आपको हमारी service कैसी लगी?"
    return brain


async def speak(brain, text):
    await turn(brain, text)
    await asyncio.sleep(DECISION_S + 0.1)


def _events(brain, kind):
    return [data for k, data in brain._recorder.events if k == kind]


IN_SCOPE = {"signal": None, "scope": "in_scope", "next_action": "answer"}


class TestTheBrain:
    async def test_a_plain_chat_turn_keeps_the_speculation(self):
        llm = _LLM([IN_SCOPE])
        brain = _brain(llm)
        await speak(brain, "बहुत अच्छी लगी")
        assert len(llm.calls) == 1                       # one reply request, sent early
        assert bot_replies(brain) == ["जी, बिल्कुल।"]
        kept = _events(brain, "speculative_reply_kept")
        assert len(kept) == 1 and kept[0]["buffered_chars"] == len("जी, बिल्कुल।")
        assert _events(brain, "speculative_reply_discarded") == []
        assert llm.calls[0]["messages"][-1] == {"role": "user", "content": "बहुत अच्छी लगी"}

    async def test_it_starts_before_the_decision_returns(self):
        llm = _LLM([IN_SCOPE])
        llm.decide.clear()
        brain = _brain(llm)
        task = asyncio.ensure_future(turn(brain, "बहुत अच्छी लगी"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if llm.calls:
                break
        assert llm.calls and llm.generate_calls == 1     # reply requested, decision pending
        llm.decide.set()
        await task
        await asyncio.sleep(DECISION_S + 0.1)
        assert _events(brain, "speculative_reply_kept")[0]["first_token_ready"] is True

    async def test_a_decision_that_changes_the_prompt_discards_it(self):
        llm = _LLM([{"signal": None, "scope": "in_scope", "next_action": "answer",
                     "slots": {"rating": {"status": "provided", "value": "5"}}}])
        brain = _brain(llm, goal_policy={"slots": [{"name": "rating", "required": True},
                                                   {"name": "comment", "required": True}]})
        await speak(brain, "पाँच")
        assert [d["reason"] for d in _events(brain, "speculative_reply_discarded")] == ["prompt"]
        assert len(llm.calls) == 2                       # the turn generated afresh
        assert "rating" not in llm.calls[1]["system"].split("still missing:")[-1].split("\n")[0]

    async def test_a_direct_decision_reply_discards_it(self):
        llm = _LLM([{**IN_SCOPE, "response_text": "धन्यवाद! आपकी rating क्या होगी?"}])
        brain = _brain(llm)
        await speak(brain, "अच्छी लगी")
        assert bot_replies(brain) == ["धन्यवाद! आपकी rating क्या होगी?"]
        assert [d["reason"] for d in _events(brain, "speculative_reply_discarded")] == ["not_reply_turn"]

    async def test_an_out_of_scope_turn_discards_it(self):
        llm = _LLM([{"scope": "out_of_scope", "next_action": "redirect_to_goal"}])
        brain = _brain(llm)
        await speak(brain, "एक गाना सुनाओ")
        assert _events(brain, "speculative_reply_discarded")[0]["reason"] == "scope"
        assert _events(brain, "speculative_reply_kept") == []

    async def test_off_by_default(self):
        llm = _LLM([IN_SCOPE])
        brain = _brain(llm, enabled=False)
        await speak(brain, "बहुत अच्छी लगी")
        assert len(llm.calls) == 1
        assert _events(brain, "speculative_reply_kept") == []
        assert _events(brain, "speculative_reply_discarded") == []

    async def test_a_barge_in_discards_it(self):
        llm = _LLM([IN_SCOPE])
        llm.decide.clear()
        brain = _brain(llm)
        brain._speculation = None
        task = asyncio.ensure_future(turn(brain, "बहुत अच्छी लगी"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if brain._speculation is not None:
                break
        assert brain._speculation is not None
        await brain._cancel_generation("barge_in")
        assert _events(brain, "speculative_reply_discarded")[0]["reason"] == "cancelled"
        llm.decide.set()
        await task


class TestWhereItNeverStarts:
    def _route(self):
        from shared.orchestration.router import RouteDecision, RouteKind

        return RouteDecision(kind=RouteKind.CHAT, reason="default_chat")

    def test_inside_a_workflow(self):
        brain = _brain(_LLM([]))
        brain._active_workflow = "wf_1"
        brain._start_speculative_reply("हाँ", self._route())
        assert brain._speculation is None

    def test_on_a_collections_bot(self):
        brain = _brain(_LLM([]))
        brain._policy = object()
        brain._start_speculative_reply("हाँ", self._route())
        assert brain._speculation is None

    def test_while_the_opening_question_is_pending(self):
        brain = _brain(_LLM([]), greeting="नमस्ते! क्या मैं Ravi जी से बात कर रहा हूँ?")
        brain._router._affirm_entry = ("start_survey", "wf_1")
        assert brain._pending_opening_question()
        brain._start_speculative_reply("हाँ", self._route())
        assert brain._speculation is None

    async def test_on_an_intent_route_it_assumes_that_route(self):
        from shared.orchestration.router import RouteDecision, RouteKind

        brain = _brain(_LLM([]))
        brain._start_speculative_reply("feedback dena hai", RouteDecision(kind=RouteKind.INTENT))
        assert brain._speculation.assumed_route == "intent"
        brain._discard_speculation("test")

    async def test_on_another_route(self):
        from shared.orchestration.router import RouteDecision, RouteKind

        brain = _brain(_LLM([]))
        brain._start_speculative_reply("ऑफर क्या है?", RouteDecision(kind=RouteKind.KNOWLEDGE))
        assert brain._speculation is None
        brain._start_speculative_reply("ऑफर क्या है?", self._route())
        assert brain._speculation is not None
        brain._discard_speculation("test")
