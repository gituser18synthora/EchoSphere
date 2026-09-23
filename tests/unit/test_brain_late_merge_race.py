"""A late-final merge must rewind the workflow AND re-queue the text, atomically.

Paired Zepto MDND calls cv_7912421c502a (local) / cv_3dbf25aa5a8f (live),
2026-09-23 17:02 IST: the partner answered the combined "reached + called?"
ask in three parts. The first two were merged and consumed (called, reached,
handover = door captured; the CX question generated), then the third part
began ~1 ms after the generation finished, before any reply audio. The merge
path cancelled the (already finished) generation and rolled the workflow
back — but read the text marker AFTER the awaited cancel, by which time the
generation's normal-completion cleanup had blanked it. Result: workflow
rewound, nothing re-queued, `turn_merged_late_final` never emitted, and only
"aur phir bhi MDND marked hua hai" ran at the re-opened ask → canned
"samajh nahi paya" + the same question again; the three answers were lost.
"""

import asyncio

from pipecat.frames.frames import (
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from tests.unit.test_brain_turn_taking import make_brain, settle, settle_turn
from voice_runtime.recording import TurnRecord

STORY = "हाँ मैं कस्टमर को कॉल किया था और मैं कस्टमर के लोकेशन पर भी गया था"
TAIL = "और फिर भी एमडीएनडी मार्क्ड हुआ है"
REPLY = "और क्या इस delivery के बारे में आपको CX support से कोई call आया था?"


class _Workflows:
    def __init__(self):
        self.rollbacks = []

    async def rollback_last_turn(self, *, session_id, workflow_name, user_text=None):
        self.rollbacks.append((workflow_name, user_text))
        return True


def _release_during_cancel(brain, release):
    """The cancel path awaits the latency-filler cancel; make the generation
    finish inside that await (the live race, made deterministic)."""

    async def _cancel_filler(reason):
        release.set()
        for _ in range(3):
            await asyncio.sleep(0)

    brain._cancel_latency_filler = _cancel_filler


def _finishing_generation(brain, text, release, user_record):
    """Mimics _handle_turn: record + history + workflow marker, then the reply
    (queued, never played) and the real normal-completion cleanup."""

    async def _generation():
        brain._recorder.add_turn(user_record)
        brain._history.append({"role": "user", "content": text})
        brain._open_turn_record = user_record
        brain._open_turn_workflow = ("wf_x", "wf_x", text)
        await release.wait()
        brain._last_bot_reply = REPLY
        brain._pending_workflow_question = REPLY
        brain._history.append({"role": "assistant", "content": REPLY})
        brain._recorder.add_turn(TurnRecord(role="bot", text=REPLY))
        if brain._open_turn_record is user_record or (
            brain._open_turn_record is None and brain._open_turn_text == text
        ):
            brain._open_turn_text = brain._open_turn_record = None

    return _generation


class TestGenerationFinishesInsideTheCancelAwait:
    async def test_speech_resume_rewinds_and_requeues_the_whole_turn(self):
        brain = make_brain()
        brain._workflows = _Workflows()
        brain._open_turn_pending_question = "क्या आप location पर पहुंचे थे?"
        release = asyncio.Event()
        user_record = TurnRecord(role="user", text=STORY, timestamp=1.0, route="workflow")
        brain._open_turn_text = STORY
        brain._turn_active = True
        brain._reply_audio_started = False
        brain._generation = asyncio.get_event_loop().create_task(
            _finishing_generation(brain, STORY, release, user_record)()
        )
        await settle()
        assert brain._open_turn_record is user_record
        _release_during_cancel(brain, release)

        await brain._on_physical_speech_resumed()

        assert brain._generation is None or brain._generation.done()
        kinds = brain._recorder.event_kinds()
        assert "workflow_turn_rolled_back" in kinds
        assert "turn_merged_late_final" in kinds, kinds
        assert brain._workflows.rollbacks == [("wf_x", STORY)]
        # The whole utterance runs again, once, against the rewound step.
        assert brain._pending_segments == [STORY]
        assert brain._pending_workflow_question == "क्या आप location पर पहुंचे थे?"
        # Neither the fragment nor the reply nobody heard survive in the
        # transcript / LLM context.
        assert brain._history == []
        assert brain._recorder.turns == []
        assert brain._last_bot_reply == ""
        assert brain._notified[-1] == {"type": "turn_rewound", "user_text": STORY}
        # Markers are consumed: a second resume cannot rewind twice.
        assert (brain._open_turn_text, brain._open_turn_record, brain._open_turn_workflow) == (None, None, None)

    async def test_straggler_final_path_is_race_safe_too(self):
        brain = make_brain()
        brain._workflows = _Workflows()
        release = asyncio.Event()
        user_record = TurnRecord(role="user", text=STORY, timestamp=1.0, route="workflow")
        brain._open_turn_text = STORY
        brain._generation = asyncio.get_event_loop().create_task(
            _finishing_generation(brain, STORY, release, user_record)()
        )
        await settle()
        _release_during_cancel(brain, release)
        dispatched = []

        async def _handle(text):
            dispatched.append(text)

        brain._handle_turn = _handle
        brain._pending_segments.append(TAIL)

        await brain._consume_pending_turn()
        await settle()

        assert brain._workflows.rollbacks == [("wf_x", STORY)]
        assert "turn_merged_late_final" in brain._recorder.event_kinds()
        assert dispatched == [f"{STORY} {TAIL}"]
        assert brain._recorder.turns == [] and brain._history == []

    async def test_frame_driven_resume_passes_the_claim(self):
        """End to end through process_frame: the UserStartedSpeaking merge
        branch claims the markers before its awaits."""
        brain = make_brain()
        brain._workflows = _Workflows()
        release = asyncio.Event()
        handled = []
        started = asyncio.Event()

        async def _handle(text):
            handled.append(text)
            started.set()
            record = TurnRecord(role="user", text=text, timestamp=1.0, route="workflow")
            brain._recorder.add_turn(record)
            brain._history.append({"role": "user", "content": text})
            brain._open_turn_record = record
            brain._open_turn_workflow = ("wf_x", "wf_x", text)
            await release.wait()
            brain._recorder.add_turn(TurnRecord(role="bot", text=REPLY))
            brain._history.append({"role": "assistant", "content": REPLY})
            if brain._open_turn_record is record:
                brain._open_turn_text = brain._open_turn_record = None

        brain._handle_turn = _handle
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(
            TranscriptionFrame(text=STORY, user_id="u", timestamp="t", language="hi-IN"),
            FrameDirection.DOWNSTREAM,
        )
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        await asyncio.wait_for(started.wait(), 1)
        assert handled == [STORY]
        _release_during_cancel(brain, release)

        # The caller starts speaking again before any reply audio.
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle()
        assert brain._workflows.rollbacks == [("wf_x", STORY)]
        assert "turn_merged_late_final" in brain._recorder.event_kinds()
        assert brain._pending_segments == [STORY]
        assert brain._recorder.turns == [] and brain._history == []

        await brain.process_frame(
            TranscriptionFrame(text=TAIL, user_id="u", timestamp="t", language="hi-IN"),
            FrameDirection.DOWNSTREAM,
        )
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert handled[-1] == f"{STORY} {TAIL}"


class TestRollbackWithoutAClaim:
    async def test_markers_are_read_at_call_time_when_nothing_was_claimed(self):
        brain = make_brain()
        brain._workflows = _Workflows()
        brain._open_turn_text = STORY
        brain._open_turn_workflow = ("wf_x", None, STORY)
        await brain._rollback_open_turn()
        kinds = brain._recorder.event_kinds()
        assert "workflow_turn_rolled_back" in kinds
        assert "turn_merged_late_final" in kinds
        assert brain._pending_segments == [STORY]
        assert (brain._open_turn_text, brain._open_turn_workflow) == (None, None)

    async def test_nothing_claimed_is_a_no_op(self):
        brain = make_brain()
        brain._workflows = _Workflows()
        await brain._rollback_open_turn()
        assert brain._workflows.rollbacks == []
        assert brain._pending_segments == []
        assert brain._recorder.event_kinds() == []
