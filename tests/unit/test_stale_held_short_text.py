"""Held-segment guard, short text: a backchannel held during the reply is the
answer to the reply's closing question only when the reply ended soon after.

Live Haier calls 2026-11-06 (bot_450a4f7b4832, DEMO):

* cv_7094a4ab5cc3 — the caller's late "हाँ" to the identity question landed
  0.3 s after the intro began; held (below the barge-in word gate) for
  15.6 s and dispatched at reply end as YES to the intro's own question
  ("did a technician call you?"). The caller never got to answer it and
  answered over the next question instead.
* cv_c191017d2061 — a loud "हेलो" said as the bot began a 10 s reply, held
  8.8 s, dispatched as the answer to the re-dispatch confirm; the LLM said
  clarify, the confirm hub's else edge closed the call, and the caller's
  real answer ("Free of cost.") arrived under the closing line and was
  dropped post-hangup.

With ``held_segment_guard_enabled`` on, a short segment whose newest final
is older than ``_HELD_SHORT_MAX_AGE_S`` at reply end is discarded (event
``stale_held_short_dropped``) and the no-response ladder resumes; one heard
within the window (an early "haan" over the end of the question) is still
dispatched. Guard off keeps today's behaviour.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

import voice_runtime.brain as brain_module
from tests.unit.test_brain_turn_taking import settle_turn, stub_turn_handler
from tests.unit.test_held_segment_guard import kinds, make_brain, say

DOWN = FrameDirection.DOWNSTREAM


def _event(brain, kind):
    return next(data for name, data in brain._recorder.events if name == kind)


async def _held_short_then_reply_end(brain, gate, *, caller: str, age_s: float, bot_text: str = "क्या मैं जान सकती हूँ, क्या आपको Haier technician की तरफ़ से call आया था?"):
    brain._last_bot_reply = bot_text
    await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
    await say(brain, gate, caller, during_bot=True, segment_ms=600.0)
    await settle_turn()
    assert "stt_segment_held_during_bot_audio" in kinds(brain)
    assert brain._pending_segments == [caller]
    # The reply kept playing for `age_s` after the held final (wall-clock in
    # the runtime; the unit harness moves the timestamp instead of sleeping).
    brain._pending_newest_final_at = time.monotonic() - age_s
    await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
    await settle_turn()


class TestStaleHeldShortText:
    async def test_live_cv_7094_late_haan_held_under_the_intro_is_not_the_intro_answer(self):
        brain, gate, _ = make_brain(guard=True)
        handled, _ = stub_turn_handler(brain)
        brain._active_workflow = "wf_1c3e59362c51"
        brain._pending_workflow_question = "क्या आपको Haier technician की तरफ़ से call आया था?"
        brain._pending_ask_shape = {"kind": "yes_no"}
        await _held_short_then_reply_end(brain, gate, caller="हाँ।", age_s=15.6)
        assert handled == []                                     # never dispatched as the answer
        assert brain._pending_segments == []
        event = _event(brain, "stale_held_short_dropped")
        assert event["text"] == "हाँ।" and event["words"] == 1 and event["pending_kind"] == "yes_no"
        assert event["age_ms"] >= 15_000 and event["max_age_ms"] == 2500.0
        assert brain._silence_task is not None and not brain._silence_task.done()   # the caller has the floor

    async def test_live_cv_c191_hello_said_as_the_reply_began_does_not_answer_the_confirm(self):
        brain, gate, _ = make_brain(guard=True)
        handled, _ = stub_turn_handler(brain)
        await _held_short_then_reply_end(
            brain, gate, caller="हेलो।", age_s=8.8,
            bot_text="जी, service charges technician visit के समय confirm करेंगे। क्या मैं आपकी request को re-dispatch के लिए आगे भेजूँ?",
        )
        assert handled == []
        assert "stale_held_short_dropped" in kinds(brain)

    async def test_live_cv_c191_ji_to_the_previous_sentence_is_dropped_at_the_margin(self):
        # "जी" 0.65 s into the intro's closing question (2.69 s before the
        # reply ended) acknowledged "...आने के लिए कहा था।", not the question.
        brain, gate, _ = make_brain(guard=True)
        handled, _ = stub_turn_handler(brain)
        await _held_short_then_reply_end(brain, gate, caller="जी।", age_s=2.69)
        assert handled == []
        assert "stale_held_short_dropped" in kinds(brain)

    @pytest.mark.parametrize("caller, age_s", [("हाँ।", 0.3), ("जी।", 1.0), ("नहीं।", 2.0), ("hmm", 0.5), ("ठीक है।", 1.5)])
    async def test_short_reply_over_the_end_of_the_question_is_still_dispatched(self, caller, age_s):
        brain, gate, _ = make_brain(guard=True)
        handled, _ = stub_turn_handler(brain)
        brain._pending_ask_shape = {"kind": "yes_no"}
        brain._pending_workflow_question = "क्या आपको service की requirement है?"
        await _held_short_then_reply_end(brain, gate, caller=caller, age_s=age_s)
        assert handled == [caller]
        assert "stale_held_short_dropped" not in kinds(brain)

    async def test_guard_off_keeps_todays_behaviour(self):
        brain, gate, _ = make_brain(guard=False)
        handled, _ = stub_turn_handler(brain)
        await _held_short_then_reply_end(brain, gate, caller="हाँ।", age_s=15.6)
        assert handled == ["हाँ।"]
        assert "stale_held_short_dropped" not in kinds(brain)

    async def test_identifier_dictation_is_exempt(self):
        brain, gate, _ = make_brain(guard=True)
        handled, _ = stub_turn_handler(brain)
        brain._identifier_capture = SimpleNamespace()
        brain._pending_newest_final_at = time.monotonic() - 10.0
        brain._pending_segments = ["पाँच"]
        assert brain._drop_stale_held_short_text() is False
        assert brain._pending_segments == ["पाँच"]

    async def test_multiword_pending_text_is_not_touched(self):
        brain, gate, _ = make_brain(guard=True)
        brain._pending_newest_final_at = time.monotonic() - 10.0
        brain._pending_segments = ["हाँ मुझे service चाहिए"]
        assert brain._drop_stale_held_short_text() is False

    async def test_after_the_drop_the_callers_real_answer_runs_normally(self):
        brain, gate, _ = make_brain(guard=True)
        handled, _ = stub_turn_handler(brain)
        await _held_short_then_reply_end(brain, gate, caller="हाँ।", age_s=15.6)
        assert handled == []
        await say(brain, gate, "कल। अभी तो होगा। नहीं किया।")
        await settle_turn()
        assert handled == ["कल। अभी तो होगा। नहीं किया।"]

    def test_threshold_is_a_module_constant(self):
        assert brain_module._HELD_SHORT_MAX_AGE_S == 2.5
