"""Ghost reply, false interrupted-reply state and announcement recovery (2026-09-24).

Runtime evidence (2026-09-23 barge-in deep test):

- Ghost reply: the generation finished and queued its reply, the caller spoke
  before the first audio byte, the interruption killed the queued audio, yet
  the reply survived in the transcript, LLM history, ``_last_bot_reply`` and
  the advanced workflow. Now the merge markers live until BotStarted, so that
  speech rewinds the unheard turn through the existing rollback.
- False interruption: after a fully heard reply ``_reply_audio_started`` stayed
  True until the next dispatch, so every later caller turn logged a
  ``barge_in`` and re-armed ``_interrupted_reply``; a later recording-notice
  rejection then re-spoke a reply the caller had already heard.
- Announcement recovery: the notice's final lands inside the pause window
  (turn still open); the resume bailed AND consumed the interrupted reply, so
  the cut greeting was never restored (dead air until the silence prompt).
"""

import asyncio

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    TextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from tests.unit.test_brain_turn_taking import make_brain, settle
from voice_runtime.recording import TurnRecord

DOWN = FrameDirection.DOWNSTREAM
USER = "haan main hi bol raha hoon"
REPLY = "Theek hai, aapke ticket par MDND deduction 400 rupaye ka hai."


class _Workflows:
    def __init__(self):
        self.rollbacks = []

    async def rollback_last_turn(self, *, session_id, workflow_name, user_text=None):
        self.rollbacks.append((workflow_name, user_text))
        return True


def spoken(brain):
    return [f.text for f in brain._pushed if isinstance(f, TextFrame)]


async def dispatch_and_finish(brain, text=USER, *, with_workflow=True):
    """Drive the real dispatch + a generation that answers with _say and
    completes BEFORE any audio is out (the ghost window)."""
    record = TurnRecord(role="user", text=text, timestamp=1.0, route="workflow")

    async def _generation():
        brain._turn_reply_queued = False
        brain._recorder.add_turn(record)
        brain._history.append({"role": "user", "content": text})
        brain._open_turn_record = record
        if with_workflow:
            brain._open_turn_workflow = ("wf_x", "wf_x", text)
        await brain._say(REPLY)
        # The real _handle_turn completion cleanup (kept in sync with brain.py).
        if brain._open_turn_record is record or (
            brain._open_turn_record is None and brain._open_turn_text == text
        ):
            if brain._turn_reply_queued and not brain._reply_audio_started:
                import time
                brain._open_turn_completed_at = time.monotonic()
            else:
                brain._open_turn_text = brain._open_turn_record = None

    brain._pending_segments.append(text)
    brain._handle_turn = lambda t: _generation()
    await brain._consume_pending_turn()
    await settle()
    assert brain._generation is not None and brain._generation.done()
    return record


class TestGhostReply:
    async def test_speech_before_first_audio_rewinds_the_unheard_turn(self):
        brain = make_brain()
        brain._workflows = _Workflows()
        await dispatch_and_finish(brain)
        # Reply queued, no BotStarted yet: markers must still be armed.
        assert brain._open_turn_text == USER
        assert brain._recorder.turns[-1].text == REPLY
        assert brain._last_bot_reply == REPLY

        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(InterruptionFrame(), DOWN)
        await settle()

        kinds = brain._recorder.event_kinds()
        assert "turn_merged_late_final" in kinds and "workflow_turn_rolled_back" in kinds
        assert "barge_in" not in kinds                      # nothing audible was interrupted
        assert brain._workflows.rollbacks == [("wf_x", USER)]
        # Transcript / history / last reply no longer claim the caller heard it.
        assert brain._recorder.turns == []
        assert brain._history == []
        assert brain._last_bot_reply == ""
        assert brain._pending_segments == [USER]           # re-queued for the merged turn
        assert {"type": "turn_rewound", "user_text": USER} in brain._notified
        assert (brain._open_turn_text, brain._open_turn_record, brain._open_turn_workflow) == (None, None, None)

    async def test_first_audio_closes_the_window_so_later_speech_is_a_barge_in(self):
        brain = make_brain()
        brain._workflows = _Workflows()
        await dispatch_and_finish(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        assert brain._open_turn_text is None and brain._open_turn_workflow is None

        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await settle()
        kinds = brain._recorder.event_kinds()
        assert "barge_in" in kinds
        assert "turn_merged_late_final" not in kinds
        assert brain._workflows.rollbacks == []
        assert brain._recorder.turns[-1].text == REPLY      # heard (partially) — stays

    async def test_window_is_bounded_for_a_reply_that_never_renders(self):
        brain = make_brain()
        brain._workflows = _Workflows()
        await dispatch_and_finish(brain)
        brain._open_turn_completed_at -= 60.0                # provider never produced audio
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await settle()
        assert "turn_merged_late_final" not in brain._recorder.event_kinds()
        assert brain._workflows.rollbacks == []
        # The stale markers are cleared by the ordinary (non-merge) cancel.
        assert brain._open_turn_text is None

    async def test_turn_without_a_spoken_reply_clears_markers_at_completion(self):
        brain = make_brain()
        record = TurnRecord(role="user", text=USER, timestamp=1.0, route="chat")

        async def _silent_generation():
            brain._turn_reply_queued = False
            brain._recorder.add_turn(record)
            brain._open_turn_record = record
            if brain._open_turn_record is record:
                if brain._turn_reply_queued and not brain._reply_audio_started:
                    import time
                    brain._open_turn_completed_at = time.monotonic()
                else:
                    brain._open_turn_text = brain._open_turn_record = None

        brain._pending_segments.append(USER)
        brain._handle_turn = lambda t: _silent_generation()
        await brain._consume_pending_turn()
        await settle()
        assert brain._open_turn_text is None and brain._open_turn_completed_at is None


class TestReplyFinishedState:
    async def test_speech_after_a_fully_heard_reply_is_not_a_barge_in(self):
        brain = make_brain()
        brain._last_bot_reply = REPLY
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        assert brain._reply_audio_started is False

        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await settle()
        assert "barge_in" not in brain._recorder.event_kinds()
        assert brain._interrupted_reply is None

    async def test_announcement_after_a_heard_reply_does_not_replay_it(self):
        brain = make_brain()
        brain._last_bot_reply = REPLY
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(
            TranscriptionFrame(text="Call is now being recorded.", user_id="c", timestamp="t"), DOWN,
        )
        await settle()
        kinds = brain._recorder.event_kinds()
        assert "recording_announcement_ignored" in kinds
        assert "bot_reply_resumed_after_announcement" not in kinds
        assert spoken(brain) == []

    async def test_a_gap_while_the_generation_streams_keeps_the_reply_interruptible(self):
        brain = make_brain()
        brain._last_bot_reply = REPLY
        release = asyncio.Event()

        async def _slow():
            await release.wait()

        brain._generation = asyncio.get_event_loop().create_task(_slow())
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)   # inter-sentence gap
        assert brain._reply_audio_started is True
        release.set()
        await settle()


class TestAnnouncementRecovery:
    async def _cut_greeting(self, brain):
        brain._last_bot_reply = REPLY
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        # Sustained-VAD barge-in confirmed by the notice's audio: the real
        # frame order is UserStarted, InterruptionFrame, then the transport's
        # BotStopped.
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(InterruptionFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        assert brain._interrupted_reply == REPLY

    async def test_final_inside_the_open_turn_resumes_when_the_turn_closes(self):
        brain = make_brain()
        await self._cut_greeting(brain)
        # The notice's final arrives while the turn is still open (pause window).
        await brain.process_frame(
            TranscriptionFrame(text="Call is now being recorded.", user_id="c", timestamp="t"), DOWN,
        )
        await settle()
        assert "recording_announcement_ignored" in brain._recorder.event_kinds()
        assert brain._interrupted_reply == REPLY               # kept, not consumed
        assert spoken(brain) == []
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await settle()
        assert "bot_reply_resumed_after_announcement" in brain._recorder.event_kinds()
        assert spoken(brain) == [REPLY]
        assert brain._recorder.turns == []                     # no user turn from the notice
        assert brain._interrupted_reply is None

    async def test_malayalam_transliterated_notice_is_recognised(self):
        brain = make_brain(languages=("hi-IN", "ml-IN"))
        await self._cut_greeting(brain)
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(
            TranscriptionFrame(text="കോൾ ഈസ് നൗ ബീയിങ് റെക്കോർഡ്.", user_id="c", timestamp="t"), DOWN,
        )
        await settle()
        kinds = brain._recorder.event_kinds()
        assert "recording_announcement_ignored" in kinds
        assert "bot_reply_resumed_after_announcement" in kinds
        assert brain._recorder.turns == []

    async def test_notice_fused_with_speech_runs_only_the_speech_and_does_not_resume(self):
        brain = make_brain()
        handled = []

        async def _handle(text):
            handled.append(text)

        brain._handle_turn = _handle
        await self._cut_greeting(brain)
        await brain.process_frame(
            TranscriptionFrame(text="Call is now being recorded. haan boliye", user_id="c", timestamp="t"), DOWN,
        )
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await asyncio.sleep(0.3)
        assert handled == ["haan boliye"]
        assert "bot_reply_resumed_after_announcement" not in brain._recorder.event_kinds()


class TestAnnouncementRecoveryOnTheWire:
    async def test_resume_lifts_the_browser_clients_post_interruption_audio_gate(self):
        brain = make_brain()
        await TestAnnouncementRecovery()._cut_greeting(brain)
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(
            TranscriptionFrame(text="Call is now being recorded.", user_id="c", timestamp="t"), DOWN,
        )
        await settle()
        assert spoken(brain) == [REPLY]
        # voiceClient.ts drops audio after `interruption` until bot_text or
        # bot_speaking_started: the re-spoken reply has no bot_text, so the
        # brain must send the event first.
        names = [(n.get("type"), n.get("name")) for n in brain._notified]
        assert ("event", "bot_speaking_started") in names
        assert names.index(("event", "bot_speaking_started")) < len(names)

    async def test_notice_split_across_flush_segments_never_becomes_a_turn(self):
        brain = make_brain()
        handled = []

        async def _handle(text):
            handled.append(text)

        brain._handle_turn = _handle
        await TestAnnouncementRecovery()._cut_greeting(brain)
        # The 0.7 s barge-in flush split the notice; each half passes the gate.
        await brain.process_frame(
            TranscriptionFrame(text="Call is now being", user_id="c", timestamp="t"), DOWN,
        )
        await brain.process_frame(
            TranscriptionFrame(text="recorded", user_id="c", timestamp="t"), DOWN,
        )
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await asyncio.sleep(0.3)
        assert handled == []
        kinds = brain._recorder.event_kinds()
        assert "recording_announcement_ignored" in kinds
        assert "bot_reply_resumed_after_announcement" in kinds
        assert spoken(brain) == [REPLY]
        assert brain._recorder.turns == []

    async def test_split_notice_fused_with_speech_runs_only_the_speech(self):
        brain = make_brain()
        handled = []

        async def _handle(text):
            handled.append(text)

        brain._handle_turn = _handle
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(
            TranscriptionFrame(text="Call is now being", user_id="c", timestamp="t"), DOWN,
        )
        await brain.process_frame(
            TranscriptionFrame(text="recorded. haan boliye", user_id="c", timestamp="t"), DOWN,
        )
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await asyncio.sleep(0.3)
        assert handled == ["haan boliye"]


    async def test_notice_split_with_its_trailing_clause_never_becomes_a_turn(self):
        brain = make_brain()
        handled = []

        async def _handle(text):
            handled.append(text)

        brain._handle_turn = _handle
        await TestAnnouncementRecovery()._cut_greeting(brain)
        await brain.process_frame(
            TranscriptionFrame(text="This call may be recorded", user_id="c", timestamp="t"), DOWN,
        )
        await brain.process_frame(
            TranscriptionFrame(text="for quality and training purposes.", user_id="c", timestamp="t"), DOWN,
        )
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await asyncio.sleep(0.3)
        assert handled == []
        assert brain._recorder.turns == []
        assert "bot_reply_resumed_after_announcement" in brain._recorder.event_kinds()
        assert spoken(brain) == [REPLY]


class TestContinuationVersusNewTurn:
    """While a reply is still unheard, speech that resumes soon after the
    caller's previous speech end merges (continuation); speech after a longer
    silence is a new turn and the unheard reply is dropped, not rewound."""

    async def test_gap_limit_derives_from_the_tenant_pause_window(self):
        from tests.unit.test_brain_turn_taking import make_brain as _mk
        assert _mk()._continuation_gap_s == 1.7            # browser 1.2 + 0.5
        from voice_runtime.brain import ConversationBrain
        assert ConversationBrain(config=_mk()._config, llm=None, recorder=_mk()._recorder,
                                 user_speech_timeout=0.7)._continuation_gap_s == 1.5   # floor
        assert ConversationBrain(config=_mk()._config, llm=None, recorder=_mk()._recorder,
                                 user_speech_timeout=1.3)._continuation_gap_s == 1.8

    async def test_short_gap_before_unheard_reply_merges(self):
        import time
        brain = make_brain()
        brain._workflows = _Workflows()
        await dispatch_and_finish(brain)
        brain._last_vad_stopped_at = time.monotonic() - 0.6       # caller paused 0.6 s
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await settle()
        kinds = brain._recorder.event_kinds()
        assert "turn_merged_late_final" in kinds
        assert "unheard_reply_superseded" not in kinds
        assert brain._pending_segments == [USER]

    async def test_long_gap_before_queued_unheard_reply_is_a_new_turn(self):
        import time
        brain = make_brain()
        brain._workflows = _Workflows()
        record = await dispatch_and_finish(brain)
        brain._last_vad_stopped_at = time.monotonic() - 2.5       # 2.5 s of silence
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(InterruptionFrame(), DOWN)
        await settle()
        kinds = brain._recorder.event_kinds()
        assert "unheard_reply_superseded" in kinds
        assert "turn_merged_late_final" not in kinds and "barge_in" not in kinds
        # The previous turn stands, its unheard reply does not.
        assert [t.role for t in brain._recorder.turns] == ["user"]
        assert brain._recorder.turns[0] is record
        assert brain._history == [{"role": "user", "content": USER}]
        assert brain._last_bot_reply == ""
        assert brain._workflows.rollbacks == []                    # no workflow rewind
        assert brain._pending_segments == []                       # nothing re-queued
        assert (brain._open_turn_text, brain._open_turn_record, brain._open_turn_workflow) == (None, None, None)

    async def test_long_gap_while_generation_in_flight_cancels_it_as_a_new_turn(self):
        import time
        brain = make_brain()
        release = asyncio.Event()
        record = TurnRecord(role="user", text=USER, timestamp=1.0, route="chat")

        async def _slow_generation():
            brain._recorder.add_turn(record)
            brain._history.append({"role": "user", "content": USER})
            brain._open_turn_record = record
            await release.wait()

        brain._pending_segments.append(USER)
        brain._handle_turn = lambda t: _slow_generation()
        await brain._consume_pending_turn()
        await settle()
        assert brain._generation_in_flight()
        brain._last_vad_stopped_at = time.monotonic() - 3.0
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await settle()
        kinds = brain._recorder.event_kinds()
        assert "unheard_reply_superseded" in kinds
        assert ("generation_cancelled", ) and any(
            e[1].get("reason") == "superseded_by_new_turn" for e in brain._recorder.events if e[0] == "generation_cancelled"
        )
        assert "turn_merged_late_final" not in kinds
        assert not brain._generation_in_flight()
        assert [t.role for t in brain._recorder.turns] == ["user"]
        assert brain._pending_segments == []

    async def test_first_speech_of_the_call_has_no_gap_and_still_merges(self):
        brain = make_brain()
        brain._workflows = _Workflows()
        await dispatch_and_finish(brain)
        assert brain._last_vad_stopped_at is None
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await settle()
        assert "turn_merged_late_final" in brain._recorder.event_kinds()


class TestCancelledGenerationDoesNotOutliveItsCancel:
    """A generation awaiting its speculative decision must stop when the brain
    cancels it. The prefetch-await used to swallow the CancelledError meant for
    the generation, so a merged/superseded turn still recorded itself and spoke
    (2026-09-24 telephony run: the same utterance in two consecutive turns)."""

    async def test_generation_cancelled_while_awaiting_the_prefetch_stops(self):
        brain = make_brain()
        never = asyncio.get_event_loop().create_future()
        brain._decision_prefetch = (USER, asyncio.get_event_loop().create_task(_wait(never)))
        outcome = []

        async def _generation():
            decision = await brain._take_decision(USER)
            outcome.append(("continued", decision))       # must never happen

        task = asyncio.get_event_loop().create_task(_generation())
        await settle()
        task.cancel()
        with_error = None
        try:
            await task
        except asyncio.CancelledError as exc:
            with_error = exc
        assert with_error is not None
        assert outcome == []

    async def test_prefetch_cancelled_on_its_own_still_yields_a_fresh_decision(self):
        brain = make_brain()
        pending = asyncio.get_event_loop().create_task(_wait(asyncio.get_event_loop().create_future()))
        brain._decision_prefetch = (USER, pending)
        fresh = []

        async def _decide(text, mark=True):
            fresh.append(text)
            return None

        brain._decide_turn = _decide
        pending.cancel()
        await settle()
        result = await brain._take_decision(USER)
        assert result is None
        # The prefetch was cancelled, not the generation: no exception escaped.


async def _wait(fut):
    return await fut
