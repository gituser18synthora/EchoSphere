"""Filler-only text held during the bot's playback is never an answer.

Live cv_2d1119054a92 (Haier, 2026-10-23): over "maafi… service ki zaroorat
hai? Please haan ya nahi mein bataiye" the caller said "to". The segment was
held (below the barge-in word gate) and dispatched as soon as the reply ended;
the decision judged it ambiguous and the workflow's else edge re-dispatched
the technician. Held text made only of discourse filler — "to", "aur",
"matlab", "haan to", "achha to", "hmm", "ok" (and their Devanagari forms) — is
now dropped at reply end (event ``filler_only_dropped``). A bare hum stays the
caller's yes only through the existing yes/no rule: the whole reply to a
pending yes/no question.
"""

from __future__ import annotations

import pytest
from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from tests.unit.test_brain_turn_taking import make_brain, settle_turn, stub_turn_handler, transcript
from voice_runtime.endpointing import is_filler_only

DOWN = FrameDirection.DOWNSTREAM


def _event(brain, kind):
    return next(data for name, data in brain._recorder.events if name == kind)


async def _held_then_reply_end(brain, *, bot_text: str, caller: str):
    brain._last_bot_reply = bot_text
    await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
    await brain.process_frame(transcript(caller), DOWN)
    await settle_turn()
    await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
    await settle_turn()


class TestFillerOnlyClassifier:
    @pytest.mark.parametrize("text", [
        "तो", "तो।", "to", "toh", "aur", "और।", "matlab", "मतलब", "haan to", "हाँ तो।", "achha to",
        "अच्छा तो", "hmm", "हम्म।", "Hmm hmm.", "ok", "Okay.", "ओके।", "ok, matlab",
    ])
    def test_fillers(self, text):
        assert is_filler_only(text) is True

    @pytest.mark.parametrize("text", [
        "हाँ", "हाँ।", "haan", "achha", "अच्छा।", "नहीं", "ठीक है", "तो हाँ", "to theek hai",
        "तो प्रोडक्ट मैंने गार्ड को दिया", "Yes.", "", "   ",
    ])
    def test_answers_and_content(self, text):
        assert is_filler_only(text) is False


class TestHeldFillerAtReplyEnd:
    async def test_live_pattern_to_over_a_yes_no_question_is_dropped(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        # The live workflow ask (Haier n_apology_visit_clarify), reported to the
        # brain as a yes/no question.
        brain._active_workflow = "wf_1c3e59362c51"
        brain._pending_workflow_question = "क्या आपको Haier की तरफ़ से service की ज़रूरत है?"
        brain._pending_ask_shape = {"kind": "yes_no"}
        await _held_then_reply_end(
            brain,
            bot_text="माफ़ कीजिए, मैं समझ नहीं पाई। क्या आपको Haier की तरफ़ से service की ज़रूरत है? Please हाँ या नहीं में बताइए।",
            caller="तो",
        )
        assert handled == []                                   # never dispatched as the answer
        assert brain._pending_segments == []
        event = _event(brain, "filler_only_dropped")
        assert event["text"] == "तो" and event["pending_kind"] == "yes_no"
        assert brain._silence_task is not None and not brain._silence_task.done()   # the no-response ladder resumes

    @pytest.mark.parametrize("filler", ["aur", "matlab", "हाँ तो", "achha to", "ok", "ओके।"])
    async def test_other_fillers_are_dropped(self, filler):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await _held_then_reply_end(brain, bot_text="आपका order किसको सौंपा था?", caller=filler)
        assert handled == []
        assert "filler_only_dropped" in brain._recorder.event_kinds()

    async def test_bare_hum_to_a_pending_yes_no_question_still_dispatches(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await _held_then_reply_end(brain, bot_text="क्या मैं आपकी request re-dispatch कर दूँ?", caller="हम्म")
        assert handled == ["हम्म"]                             # the existing hmm-as-yes rule decides
        assert "filler_only_dropped" not in brain._recorder.event_kinds()

    async def test_bare_hum_without_a_yes_no_question_is_dropped(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await _held_then_reply_end(brain, bot_text="आपका order किसको सौंपा था?", caller="hmm")
        assert handled == []
        assert _event(brain, "filler_only_dropped")["pending_kind"] == "free_text"

    async def test_a_held_answer_still_runs_at_reply_end(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await _held_then_reply_end(brain, bot_text="क्या आपको service की ज़रूरत है?", caller="हाँ")
        assert handled == ["हाँ"]
        assert "filler_only_dropped" not in brain._recorder.event_kinds()

    async def test_filler_plus_content_is_not_filler(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await _held_then_reply_end(brain, bot_text="क्या आपको service की ज़रूरत है?", caller="तो हाँ चाहिए")
        assert handled == ["तो हाँ चाहिए"]
