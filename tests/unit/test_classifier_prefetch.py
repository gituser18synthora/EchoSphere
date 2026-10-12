"""Speculative intent classification for Goal-Engine-off bots.

The understanding stage of an engine-off bot is the hybrid classifier's LLM
call. It used to run fully serial at dispatch — after the endpoint wait had
already elapsed — while engine bots overlapped their decision with that wait.
This suite pins the classifier prefetch to the same contract the decision
prefetch has:

1. started from the buffered text while the turn is still open, consumed at
   dispatch on an EXACT text match, exactly one model call for the turn;
2. a prefetch for different text (straggler merged in) is discarded and the
   classifier runs fresh on the real turn;
3. never started when the engine is on (the decision prefetch owns that) or
   when the classifier is not allowed to spend a model call;
4. the pipeline's own degradation (timeout/failure → regex signals) is
   unchanged on the speculative path, and the tokens a prefetch spent are
   billed once, to the turn that consumed it;
5. cancellation of the dispatched generation propagates through a pending
   prefetch (a barge-in must not let the cancelled turn run on).
"""

import asyncio
import json
import time

from pipecat.frames.frames import UserStartedSpeakingFrame, UserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.router import classify_user_signal
from shared.providers.base import LLMResult
from voice_runtime.brain import ConversationBrain

from tests.unit.test_agentic_orchestration import events
from tests.unit.test_brain_collection_policy import (
    GRACE,
    _RecorderStub,
    _StreamingLLMStub,
    transcript,
    turn,
)

INTENTS = [
    {"name": "billing_query", "description": "billing problems",
     "samples": ["bill galat aaya"], "route": "", "confidence_threshold": 0.6},
]
CLASSIFICATION = json.dumps({
    "intent": "billing_query", "signal": None, "confidence": 0.91,
    "entities": {}, "should_interrupt_current_flow": False,
    "context_question": False,
})
DECISION = json.dumps({
    "intent": "billing_query", "signal": None, "decision": None,
    "scope": "in_scope", "confidence": 0.9, "reason": "billing problem",
    "slots": {}, "next_action": "answer", "needs_clarification": False,
    "context_question": False, "response_text": "",
})
TEXT = "mujhe apna invoice samajh nahi aa raha"


class _ClassifierLLM(_StreamingLLMStub):
    """Stage-A classification via generate(); the reply streams as usual."""

    def __init__(self, *, delay=0.0, fail=False, payload=CLASSIFICATION):
        super().__init__(tokens=("ठीक", " है।"))
        self.generate_calls = []
        self.completed = []
        self._delay = delay
        self._fail = fail
        self._payload = payload

    async def generate(self, messages, *, system=None, temperature=None,
                       max_tokens=None, tools=None, response_format=None):
        self.generate_calls.append(messages[-1]["content"])
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError("classifier provider down")
        self.completed.append(messages[-1]["content"])
        return LLMResult(text=self._payload, input_tokens=120, output_tokens=30)


def make_brain(llm, *, goal_engine=False, intent_llm=True, intent_timeout=5.0,
               decision_timeout=5.0):
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Support Bot.",
        llm={"settings": {
            "goal_engine_enabled": goal_engine,
            "intent_llm_enabled": intent_llm,
            "intent_timeout_seconds": intent_timeout,
            "orchestration_timeout_seconds": decision_timeout,
        }},
        intents=INTENTS,
    )
    brain = ConversationBrain(
        config=config, llm=llm, recorder=_RecorderStub(),
        finalize_grace=GRACE, complete_endpoint=GRACE,
    )
    brain._notified = []

    async def _push(frame, direction=None):
        pass

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify
    brain.create_task = lambda coro, name=None: (
        asyncio.get_event_loop().create_task(coro)
    )

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.cancel_task = _cancel_task
    return brain


async def settle(brain):
    if brain._generation is not None:
        await brain._generation
    for _ in range(5):
        await asyncio.sleep(0)


# ── 1: started while the turn is open, consumed at dispatch ──────────────────


class TestPrefetchConsumed:
    async def test_final_starts_the_prefetch_and_dispatch_consumes_it(self):
        llm = _ClassifierLLM()
        brain = make_brain(llm)
        assert not brain._goal_engine.enabled
        assert brain._intent_pipeline.llm_enabled

        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(
            transcript("mujhe apna invoice samajh nahi aa raha"), FrameDirection.DOWNSTREAM,
        )
        # The classifier call is in flight BEFORE the turn closes.
        assert brain._classify_prefetch is not None
        assert brain._classify_prefetch[0] == "mujhe apna invoice samajh nahi aa raha"
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(llm.generate_calls) == 1

        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(GRACE * 3)
        await settle(brain)

        # Exactly one model call for the understanding stage of this turn.
        assert len(llm.generate_calls) == 1
        assert brain._classify_prefetch is None
        classified = events(brain, "intent_classified")
        assert classified and classified[-1]["intent"] == "billing_query"
        assert classified[-1]["source"] == "llm"
        rows = events(brain, "orchestration_turn")
        assert rows[-1]["intent"] == "billing_query"
        assert rows[-1]["interpretation"] == "fallback"
        assert brain._latency.counts.get("classification_prefetched") == 1
        # Tokens billed once, to this turn: the classifier's 120/30 plus the
        # streamed reply (one request, estimated output, no input report).
        usage = brain._recorder.usage
        assert usage.get("llm_requests") == 2
        assert usage.get("llm_input_tokens") == 120
        assert usage.get("llm_output_tokens") == 30 + len("ठीक है।") // 4

    async def test_prefetch_overlaps_the_endpoint_wait(self):
        # A classifier that needs 0.3 s; the endpoint grace is 0.05 s. Serial
        # it would add ~0.3 s after the turn closed; prefetched it has already
        # been running since the final arrived.
        llm = _ClassifierLLM(delay=0.3)
        brain = make_brain(llm)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(
            transcript("mujhe apna invoice samajh nahi aa raha"), FrameDirection.DOWNSTREAM,
        )
        await asyncio.sleep(0.25)  # most of the model time elapses "inside" the pause
        closed = time.monotonic()
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(GRACE * 3)
        await settle(brain)
        waited = time.monotonic() - closed
        assert len(llm.generate_calls) == 1
        assert events(brain, "intent_classified")[-1]["intent"] == "billing_query"
        assert waited < 0.25, f"turn waited {waited:.2f}s after close"


# ── 2: stale prefetch discarded, fresh call on the real turn ─────────────────


class TestStalePrefetch:
    async def test_text_change_replaces_the_running_prefetch(self):
        llm = _ClassifierLLM(delay=0.2)
        brain = make_brain(llm)
        brain._pending_segments = ["mujhe apna invoice"]
        brain._start_decision_prefetch()
        first = brain._classify_prefetch
        assert first is not None and first[0] == "mujhe apna invoice"
        await asyncio.sleep(0)

        brain._pending_segments.append("samajh nahi aa raha")
        brain._start_decision_prefetch()
        second = brain._classify_prefetch
        assert second is not None and second[0] == "mujhe apna invoice samajh nahi aa raha"
        assert second[1] is not first[1]
        await asyncio.sleep(0)
        assert first[1].cancelled()

        classification = await brain._classify_turn("mujhe apna invoice samajh nahi aa raha")
        assert classification.intent == "billing_query"
        # The stale call never completed; the matching one was consumed, so
        # no third call ran.
        assert len(llm.generate_calls) == 2
        assert [c.endswith(TEXT) for c in llm.completed] == [True]

    async def test_dispatch_text_mismatch_runs_fresh(self):
        llm = _ClassifierLLM()
        brain = make_brain(llm)
        brain._pending_segments = ["mujhe apna invoice"]
        brain._start_decision_prefetch()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        classification = await brain._classify_turn("mujhe apna invoice samajh nahi aa raha")
        assert classification.intent == "billing_query"
        assert brain._classify_prefetch is None
        assert len(llm.generate_calls) == 2
        assert llm.generate_calls[-1].endswith("mujhe apna invoice samajh nahi aa raha")

    async def test_same_text_keeps_the_running_prefetch(self):
        llm = _ClassifierLLM(delay=0.2)
        brain = make_brain(llm)
        brain._pending_segments = ["mujhe apna invoice samajh nahi aa raha"]
        brain._start_decision_prefetch()
        task = brain._classify_prefetch[1]
        await asyncio.sleep(0)
        brain._start_decision_prefetch()
        assert brain._classify_prefetch[1] is task
        assert len(llm.generate_calls) == 1
        task.cancel()

    async def test_completed_prefetch_is_kept_when_the_timer_re_arms(self):
        # The finalize timer re-arms on every final and on the VAD stop. A
        # classification that already COMPLETED for the unchanged text must
        # survive that — re-requesting it made dispatch wait a full model
        # round-trip it already had.
        llm = _ClassifierLLM()
        brain = make_brain(llm)
        brain._pending_segments = [TEXT]
        brain._start_decision_prefetch()
        task = brain._classify_prefetch[1]
        for _ in range(5):
            await asyncio.sleep(0)
        assert task.done()
        brain._start_decision_prefetch()
        assert brain._classify_prefetch[1] is task
        classification = await brain._classify_turn(TEXT)
        assert classification.intent == "billing_query"
        assert len(llm.generate_calls) == 1
        assert brain._latency.counts.get("classification_prefetched") == 1

    async def test_completed_decision_prefetch_is_kept_when_the_timer_re_arms(self):
        # Same contract for Goal Engine bots (the pre-existing slot).
        llm = _ClassifierLLM(payload=DECISION)
        brain = make_brain(llm, goal_engine=True)
        brain._pending_segments = [TEXT]
        brain._start_decision_prefetch()
        task = brain._decision_prefetch[1]
        for _ in range(5):
            await asyncio.sleep(0)
        assert task.done() and task.result() is not None
        brain._start_decision_prefetch()
        assert brain._decision_prefetch[1] is task
        orchestrated = await brain._take_decision(TEXT)
        assert orchestrated is not None and orchestrated.intent == "billing_query"
        assert len(llm.generate_calls) == 1
        assert brain._latency.counts.get("decision_prefetched") == 1

    async def test_timed_out_decision_prefetch_is_consumed_not_re_run(self):
        # The speculation timed out BEFORE dispatch (long pause). Re-arming
        # used to request it again — a second 0.5 s budget spent after
        # dispatch for the same text. Now dispatch falls back at once, the
        # same rule a post-dispatch timeout follows.
        llm = _ClassifierLLM(delay=5.0, payload=DECISION)
        brain = make_brain(llm, goal_engine=True, decision_timeout=0.5)
        brain._pending_segments = [TEXT]
        brain._start_decision_prefetch()
        task = brain._decision_prefetch[1]
        await asyncio.sleep(0.7)
        assert task.done() and task.result() is None
        brain._start_decision_prefetch()
        assert brain._decision_prefetch[1] is task
        started = time.monotonic()
        assert await brain._take_decision(TEXT) is None
        assert time.monotonic() - started < 0.2
        assert len(llm.generate_calls) == 1
        assert events(brain, "orchestration_fallback")[-1]["reason"] == "timeout"

    async def test_failed_prefetch_task_is_replaced_on_re_arm(self):
        llm = _ClassifierLLM()
        brain = make_brain(llm)
        brain._pending_segments = [TEXT]

        async def _boom():
            raise RuntimeError("task crashed")

        crashed = asyncio.get_event_loop().create_task(_boom())
        for _ in range(3):
            await asyncio.sleep(0)
        brain._classify_prefetch = (TEXT, crashed)
        brain._start_decision_prefetch()
        assert brain._classify_prefetch[1] is not crashed

    async def test_discard_cancels_the_classifier_prefetch(self):
        llm = _ClassifierLLM(delay=0.2)
        brain = make_brain(llm)
        brain._pending_segments = ["mujhe apna invoice samajh nahi aa raha"]
        brain._start_decision_prefetch()
        task = brain._classify_prefetch[1]
        brain._discard_decision_prefetch("hangup")
        await asyncio.sleep(0)
        assert brain._classify_prefetch is None
        assert task.cancelled()
        assert events(brain, "decision_prefetch_discarded")[-1]["reason"] == "hangup"

    async def test_cleanup_cancels_the_classifier_prefetch(self):
        llm = _ClassifierLLM(delay=0.2)
        brain = make_brain(llm)
        brain._pending_segments = ["mujhe apna invoice samajh nahi aa raha"]
        brain._start_decision_prefetch()
        task = brain._classify_prefetch[1]
        await brain.cleanup()
        await asyncio.sleep(0)
        assert brain._classify_prefetch is None
        assert task.cancelled()


# ── 3: gating ────────────────────────────────────────────────────────────────


class TestGating:
    async def test_engine_bots_prefetch_the_decision_not_the_classifier(self):
        llm = _ClassifierLLM()
        brain = make_brain(llm, goal_engine=True)
        assert brain._goal_engine.enabled
        brain._pending_segments = ["mujhe apna invoice samajh nahi aa raha"]
        brain._start_decision_prefetch()
        assert brain._decision_prefetch is not None
        assert brain._classify_prefetch is None
        brain._decision_prefetch[1].cancel()

    async def test_classifier_off_never_speculates(self):
        llm = _ClassifierLLM()
        brain = make_brain(llm, intent_llm=False)
        assert not brain._intent_pipeline.llm_enabled
        brain._pending_segments = ["mujhe apna invoice samajh nahi aa raha"]
        brain._start_decision_prefetch()
        assert brain._classify_prefetch is None
        assert brain._decision_prefetch is None
        assert llm.generate_calls == []

    async def test_empty_buffer_and_closing_never_speculate(self):
        llm = _ClassifierLLM()
        brain = make_brain(llm)
        brain._start_decision_prefetch()
        assert brain._classify_prefetch is None
        brain._pending_segments = ["mujhe apna invoice samajh nahi aa raha"]
        brain._closing = True
        brain._start_decision_prefetch()
        assert brain._classify_prefetch is None
        assert llm.generate_calls == []


# ── 4: degradation unchanged on the speculative path ─────────────────────────


class TestDegradation:
    async def test_provider_failure_in_the_prefetch_degrades_to_regex(self):
        llm = _ClassifierLLM(fail=True)
        brain = make_brain(llm)
        await turn(brain, "nahi, mujhe nahi chahiye")
        await settle(brain)
        classified = events(brain, "intent_classified")
        assert classified and classified[-1]["source"] == "regex"
        # Exactly the deterministic verdict the non-speculative path gives.
        assert classified[-1]["signal"] == classify_user_signal("nahi, mujhe nahi chahiye")
        # One attempt (the prefetch); the failure did not trigger a second,
        # sequential model call at dispatch.
        assert len(llm.generate_calls) == 1
        # The failed classification billed nothing (only the reply did).
        assert brain._recorder.usage.get("llm_input_tokens", 0) == 0

    async def test_classifier_timeout_in_the_prefetch_degrades_to_regex(self):
        llm = _ClassifierLLM(delay=5.0)
        brain = make_brain(llm, intent_timeout=0.1)
        started = time.monotonic()
        await turn(brain, "nahi, mujhe nahi chahiye")
        await settle(brain)
        elapsed = time.monotonic() - started
        classified = events(brain, "intent_classified")
        assert classified and classified[-1]["source"] == "regex"
        assert len(llm.generate_calls) == 1
        assert elapsed < 1.5, f"turn took {elapsed:.2f}s"


# ── 5: cancellation semantics ────────────────────────────────────────────────


class TestCancellation:
    async def test_cancelling_the_generation_propagates_through_the_prefetch(self):
        llm = _ClassifierLLM(delay=1.0)
        brain = make_brain(llm)
        brain._pending_segments = ["mujhe apna invoice samajh nahi aa raha"]
        brain._start_decision_prefetch()
        consumer = asyncio.get_event_loop().create_task(
            brain._classify_turn("mujhe apna invoice samajh nahi aa raha")
        )
        await asyncio.sleep(0.05)
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass
        assert consumer.cancelled(), "the cancelled turn ran on"
        assert events(brain, "intent_classified") == []

    async def test_a_cancelled_prefetch_alone_reruns_fresh(self):
        # The PREFETCH was cancelled (e.g. by a discard) but the consuming
        # generation was not: the classifier runs fresh for the real turn.
        llm = _ClassifierLLM(delay=0.2)
        brain = make_brain(llm)
        brain._pending_segments = [TEXT]
        brain._start_decision_prefetch()
        await asyncio.sleep(0)  # the speculative call is in flight
        brain._classify_prefetch[1].cancel()
        classification = await brain._classify_turn(TEXT)
        assert classification.intent == "billing_query"
        assert len(llm.generate_calls) == 2
        assert len(llm.completed) == 1  # only the fresh call finished
