"""Phase 4 Change 5: one short acknowledgement when no reply audio is ready
``early_ack_ms`` after dispatch (llm_settings.early_ack_ms, default off).

The word is the planner's pre-rendered acknowledgement in the bot's voice;
it plays in the early slot only when its clip is ready (nothing else ever
plays that early). Never on two consecutive turns, never in front of an
identity / compliance / exact-wording line, never when a deterministic short
answer already settled the turn.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.naturalness import SpeechNaturalnessPlanner
from shared.orchestration.workflow_engine import _exact_wording_ahead
from voice_runtime.brain import ConversationBrain

from tests.unit.test_agentic_orchestration import GRACE, _AgenticLLMStub, _RecorderStub
from tests.unit.test_latency_filler import (
    DOWN,
    _AcknowledgementCueStub,
    _ShortLibrary,
    make_filler,
    tts_audio,
    wait,
)


# ── the filler's early slot ──────────────────────────────────────────────────

class TestTheEarlySlot:
    async def test_the_acknowledgement_plays_early_ack_ms_after_dispatch(self):
        filler = make_filler(delay_ms=600, library=_ShortLibrary(clip_ms=60),
                             cue_library=_AcknowledgementCueStub(clip_ms=60))
        dispatched = time.monotonic()
        await filler.arm(turn_id=1, gender="male", dispatched_at=dispatched,
                         acknowledgement={"text": "जी…"}, ack_after_ms=80)
        await wait(0.13)
        played = filler._recorder.data("latency_filler_played")
        assert len(played) == 1 and played[0]["sound"] == "acknowledgement"
        assert 70 <= played[0]["waited_ms"] < 130
        assert filler._recorder.data("early_ack_played")[0]["early"] is True

    async def test_no_clip_ready_keeps_the_regular_schedule(self):
        filler = make_filler(delay_ms=200, library=_ShortLibrary(clip_ms=60),
                             cue_library=_AcknowledgementCueStub(ready=False, clip_ms=60))
        await filler.arm(turn_id=1, gender="male", dispatched_at=time.monotonic(),
                         acknowledgement={"text": "जी…"}, ack_after_ms=50)
        await wait(0.1)
        assert filler._recorder.data("latency_filler_played") == []   # nothing that early
        assert filler._recorder.data("early_ack_not_ready") == [{"turn": 1}]
        await wait(0.15)
        played = filler._recorder.data("latency_filler_played")
        assert played and played[0]["sound"] == "breath" and played[0]["waited_ms"] >= 190

    async def test_reply_audio_before_the_slot_plays_nothing(self):
        filler = make_filler(delay_ms=600, library=_ShortLibrary(clip_ms=60),
                             cue_library=_AcknowledgementCueStub(clip_ms=60))
        await filler.arm(turn_id=1, gender="male", dispatched_at=time.monotonic(),
                         acknowledgement={"text": "जी…"}, ack_after_ms=80)
        await filler.process_frame(tts_audio(), DOWN)
        await wait(0.12)
        assert filler._recorder.data("latency_filler_played") == []

    async def test_without_a_planned_word_the_slot_does_not_exist(self):
        filler = make_filler(delay_ms=200, library=_ShortLibrary(clip_ms=60),
                             cue_library=_AcknowledgementCueStub(clip_ms=60))
        await filler.arm(turn_id=1, gender="male", dispatched_at=time.monotonic(),
                         ack_after_ms=50)
        await wait(0.1)
        assert filler._recorder.data("latency_filler_played") == []


# ── the planner ──────────────────────────────────────────────────────────────

def _planner(probability: float) -> SpeechNaturalnessPlanner:
    planner = SpeechNaturalnessPlanner({"enabled": True, "acknowledgement_probability": probability})
    return planner


class TestThePlanner:
    def test_force_skips_the_roll(self):
        planner = _planner(0.0)
        assert planner.plan_early_ack(language="hi-IN", turn_index=3, commit=False) == ""
        assert planner.last_early_ack_reason == "roll"
        assert planner.plan_early_ack(language="hi-IN", turn_index=3, commit=False, force=True)

    def test_never_on_two_consecutive_turns_even_forced(self):
        planner = _planner(1.0)
        assert planner.plan_early_ack(language="hi-IN", turn_index=3, force=True)
        assert planner.plan_early_ack(language="hi-IN", turn_index=4, force=True) == ""
        assert planner.last_early_ack_reason == "anti_repetition"


# ── the engine's look-ahead ──────────────────────────────────────────────────

def _flow(next_config: dict, *, via_message: bool = False):
    nodes = [{"id": "ask", "kind": "ask", "config": {"question": "क्या आप पहुँचे थे?"}},
             {"id": "msg", "kind": "message", "config": {"text": "ठीक है।"}},
             {"id": "next", "kind": "ask", "config": next_config},
             {"id": "after", "kind": "message", "config": {"text": "x", "responseMode": "exact"}}]
    edges = ([{"from": "ask", "to": "msg"}, {"from": "msg", "to": "next"}] if via_message
             else [{"from": "ask", "to": "next"}])
    edges.append({"from": "next", "to": "after"})
    return {"nodes": nodes, "edges": edges}, nodes[0]


class TestExactWordingAhead:
    def test_an_exact_line_next(self):
        assert _exact_wording_ahead(*_flow({"text": "Disclosure.", "responseMode": "exact"}))

    def test_through_a_message_chain(self):
        assert _exact_wording_ahead(*_flow({"responseMode": "exact"}, via_message=True))

    def test_a_compliance_wording(self):
        assert _exact_wording_ahead(*_flow({"question": "{{recording_notice}} आगे बढ़ें?"}))

    def test_an_ordinary_next_step_and_nothing_past_the_next_pause(self):
        assert not _exact_wording_ahead(*_flow({"question": "Guard का नाम?"}))


# ── the brain ────────────────────────────────────────────────────────────────

class _Filler:
    def __init__(self):
        self.arms = []
        self.last_cue_played = None
        self.fillers_played = 0

    async def arm(self, **kwargs):
        self.arms.append(kwargs)

    async def cancel(self, reason="cancelled"):
        pass


class _Planner:
    enabled = True
    acknowledgements_enabled = True
    latency_filler_kind = "breath"
    last_early_ack_reason = ""

    def __init__(self):
        self.forced = []

    def plan_early_ack(self, **kwargs):
        self.forced.append(kwargs.get("force", False))
        return "जी…"

    def discard_early_ack(self):
        pass


def _brain(*, early_ack_ms=800, greeting="") -> ConversationBrain:
    settings = {"early_ack_ms": early_ack_ms, "deterministic_answers": True} if early_ack_ms else {}
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Support", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Support.", greeting=greeting,
        intents=[{"name": "support", "samples": ["help"], "route": "workflow:wf_1"}],
        llm={"provider": "openai", "model": "gpt-x", "settings": settings},
    )
    brain = ConversationBrain(config=config, llm=_AgenticLLMStub([]), recorder=_RecorderStub(),
                              finalize_grace=GRACE, complete_endpoint=GRACE)
    brain._latency_filler = _Filler()
    brain._naturalness = _Planner()
    brain._active_workflow = "wf_1"
    brain._workflow_ever_routed = True
    brain._pending_workflow_question = "आपने customer को call किया था?"
    brain._last_bot_reply = "आपने customer को call किया था?"
    return brain


async def _arm(brain, text):
    await brain._arm_latency_filler(text)
    return brain._latency_filler.arms[-1]


class TestTheBrain:
    async def test_a_slow_turn_gets_the_early_slot_and_a_forced_word(self):
        brain = _brain()
        arm = await _arm(brain, "मैंने call किया था पर उसने उठाया नहीं")
        assert arm["ack_after_ms"] == 800 and arm["acknowledgement"]["text"] == "जी…"
        assert brain._naturalness.forced == [True]

    async def test_off_by_default(self):
        brain = _brain(early_ack_ms=None)
        arm = await _arm(brain, "मैंने call किया था पर उसने उठाया नहीं")
        assert "ack_after_ms" not in arm and brain._naturalness.forced == [False]

    async def test_not_when_a_deterministic_answer_settles_the_turn(self):
        brain = _brain()
        brain._pending_ask_shape = {"kind": "yes_no", "options": []}
        arm = await _arm(brain, "हाँ जी")
        assert "ack_after_ms" not in arm

    async def test_not_in_front_of_an_exact_wording_line(self):
        brain = _brain()
        brain._pending_exact_ahead = True
        arm = await _arm(brain, "मैंने call किया था पर उसने उठाया नहीं")
        assert "ack_after_ms" not in arm

    async def test_not_while_identity_or_the_opening_question_is_pending(self):
        brain = _brain(greeting="नमस्ते! क्या मैं Ravi जी से बात कर रहा हूँ?")
        brain._active_workflow, brain._workflow_ever_routed = None, False
        brain._router._affirm_entry = ("support", "wf_1")
        arm = await _arm(brain, "हाँ बोलिए, क्या काम है")
        assert "ack_after_ms" not in arm

    async def test_not_on_a_collections_call(self):
        brain = _brain()
        brain._policy = SimpleNamespace(awaiting_identity=False, verified=True,
                                        awaiting_reference=False, transaction_reference=None)
        assert not brain._early_ack_allowed("मैंने payment कर दिया था")
