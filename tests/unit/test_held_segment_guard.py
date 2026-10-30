"""Held-segment guard and trusted caller-level bootstrap in the brain.

Guard (tenant ``held_segment_guard_enabled``): multi-word speech captured
while the bot talks that never confirmed a barge-in is held, merged only into
the caller's own continuation, and otherwise discarded — never dispatched
because ``BotStoppedSpeakingFrame`` arrived. Short acknowledgements keep the
existing behaviour.

Trusted bootstrap: a turn the call vouches for (identity confirmed,
identifier validated, workflow advanced) seeds the caller-level baseline from
that turn's own, bot-quiet, plainly-accepted audio.
"""

import asyncio
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

import voice_runtime.brain as brain_module
from shared.bot_config import ResolvedBotConfig
from tests.unit.test_background_speech_guard import FakeGate, level_events
from tests.unit.test_brain_turn_taking import (
    GRACE,
    _RecorderStub,
    settle_turn,
    stub_turn_handler,
)
from voice_runtime.brain import ConversationBrain
from voice_runtime.caller_level import CallerLevelBaseline

DOWN = FrameDirection.DOWNSTREAM


def make_brain(*, guard=True, with_gate=True, min_segments=3):
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
    )
    gate = FakeGate() if with_gate else None
    baseline = CallerLevelBaseline(margin_db=12, min_segments=min_segments, enforce=False) if with_gate else None
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(), finalize_grace=GRACE,
        audio_gate=gate, caller_level=baseline, held_segment_guard=guard,
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify
    brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.cancel_task = _cancel_task
    return brain, gate, baseline


async def say(brain, gate, text, *, level=-30.0, during_bot=False, segment_ms=1500.0):
    if gate is not None:
        gate.snapshot = {"snr_db": 30.0, "speech_dbfs": level, "during_bot_audio": during_bot,
                         "segment_ms": segment_ms, "live": False}
    await brain.process_frame(TranscriptionFrame(text=text, user_id="u", timestamp="t"), DOWN)


def kinds(brain):
    return brain._recorder.event_kinds()


class TestHeldMultiword:
    async def test_multiword_during_bot_audio_is_held_not_dispatched_at_bot_stop(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await say(brain, gate, "यहां पर ग्रीन कोऑर्डिनेट कर रहे हैं", during_bot=True)
        await settle_turn()
        assert brain._pending_segments == [] and len(brain._held_multiword) == 1
        assert "stt_segment_held_multiword" in kinds(brain)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await settle_turn()
        assert handled == []
        assert brain._held_continuation_task is not None

    async def test_later_clean_caller_turn_discards_held_text_never_prepends(self):
        # The held sentence may have been anybody's; the later turn is the
        # caller's own and runs alone.
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await say(brain, gate, "यहां पर ग्रीन कोऑर्डिनेट कर रहे हैं", during_bot=True)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await settle_turn()
        await say(brain, gate, "पेमेंट कल हो जाएगा")
        await settle_turn()
        assert handled == ["पेमेंट कल हो जाएगा"]
        discarded = [d for k, d in brain._recorder.events if k == "stt_held_segment_discarded"]
        assert discarded and discarded[-1]["reason"] == "superseded_by_caller_turn"
        assert "stt_held_segment_merged" not in kinds(brain)
        assert brain._held_multiword == [] and brain._held_continuation_task is None

    async def test_confirmed_barge_in_in_the_same_speech_episode_reclaims_held_text(self):
        # A mid-utterance flush final (held) followed by the sustained-VAD
        # commit of the SAME speech: one utterance, reclaimed into the turn.
        brain, gate, _ = make_brain()
        handled, started = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(VADUserStartedSpeakingFrame(), DOWN)
        await say(brain, gate, "मुझे एक बात बतानी है", during_bot=True)
        assert len(brain._held_multiword) == 1
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)        # commit while speech continues
        assert brain._held_multiword == [] and brain._pending_segments == ["मुझे एक बात बतानी है"]
        await say(brain, gate, "पेमेंट कल हो जाएगा", during_bot=True)
        await brain.process_frame(VADUserStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await asyncio.wait_for(started.wait(), 1)
        assert handled == ["मुझे एक बात बतानी है पेमेंट कल हो जाएगा"]
        merged = [d for k, d in brain._recorder.events if k == "stt_held_segment_merged"]
        assert merged and merged[-1]["via"] == "confirmed_barge_in_same_utterance"

    async def test_barge_in_in_a_later_episode_discards_earlier_held_text(self):
        brain, gate, _ = make_brain()
        handled, started = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(VADUserStartedSpeakingFrame(), DOWN)
        await say(brain, gate, "भाई गुरु भाई ये सब चीजें हैं", during_bot=True)   # background, held
        await brain.process_frame(VADUserStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(VADUserStartedSpeakingFrame(), DOWN)          # new episode: the caller
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        assert brain._held_multiword == [] and brain._pending_segments == []
        discarded = [d for k, d in brain._recorder.events if k == "stt_held_segment_discarded"]
        assert discarded[-1]["reason"] == "superseded_by_barge_in"
        await say(brain, gate, "एक मिनट रुकिए मेरी बात सुनिए", during_bot=True)
        await brain.process_frame(VADUserStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await asyncio.wait_for(started.wait(), 1)
        assert handled == ["एक मिनट रुकिए मेरी बात सुनिए"]

    async def test_no_continuation_discards_cleanly(self, monkeypatch):
        monkeypatch.setattr(brain_module, "_HELD_CONTINUATION_WINDOW_S", 0.1)
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await say(brain, gate, "टीवी पर समाचार चल रहा है", during_bot=True)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await asyncio.sleep(0.25)
        assert handled == [] and brain._held_multiword == []
        discarded = [d for k, d in brain._recorder.events if k == "stt_held_segment_discarded"]
        assert discarded and discarded[-1]["reason"] == "no_continuation"
        # A later genuine turn is unaffected by the discarded text.
        await say(brain, gate, "हाँ मैं सुन रहा हूँ")
        await settle_turn()
        assert handled == ["हाँ मैं सुन रहा हूँ"]

    async def test_new_reply_starting_discards_stale_held_text(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await say(brain, gate, "भाई गुरु भाई ये सब चीजें हैं", during_bot=True)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)  # silence prompt / next step
        discarded = [d for k, d in brain._recorder.events if k == "stt_held_segment_discarded"]
        assert discarded and discarded[-1]["reason"] == "stale_new_reply"
        assert brain._held_multiword == [] and handled == []

    async def test_another_unconfirmed_snippet_during_bot_audio_does_not_release_held_text(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await say(brain, gate, "सीधा काउंट पे आना होगा", during_bot=True)
        await say(brain, gate, "हाँ", during_bot=True, segment_ms=300.0)   # one-word ack, existing behaviour
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await settle_turn()
        assert handled == ["हाँ"]                       # the ack dispatches as today
        assert len(brain._held_multiword) == 1          # the sentence stays held

    async def test_two_word_acknowledgement_keeps_existing_behaviour(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await say(brain, gate, "हाँ जी", during_bot=True, segment_ms=400.0)
        await settle_turn()
        assert "stt_segment_held_during_bot_audio" in kinds(brain)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await settle_turn()
        assert handled == ["हाँ जी"]

    async def test_confirmed_barge_in_is_processed_normally(self):
        brain, gate, _ = make_brain()
        handled, started = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)   # word gate confirmed
        await say(brain, gate, "एक मिनट रुकिए मेरी बात सुनिए", during_bot=True)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await asyncio.wait_for(started.wait(), 1)
        assert handled == ["एक मिनट रुकिए मेरी बात सुनिए"]
        assert brain._held_multiword == []

    async def test_hangup_phrase_during_bot_audio_is_still_immediate(self):
        brain, gate, _ = make_brain()
        stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await say(brain, gate, "फोन काट दो", during_bot=True)
        assert brain._closing is True and brain._held_multiword == []

    async def test_late_final_after_bot_stopped_but_captured_during_bot_audio_is_held(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await say(brain, gate, "यहां पर ग्रीन कोऑर्डिनेट कर रहे हैं", during_bot=True)
        await settle_turn()
        assert handled == [] and len(brain._held_multiword) == 1
        assert brain._held_continuation_task is not None and brain._silence_task is not None

    async def test_guard_off_keeps_todays_dispatch_at_bot_stop(self):
        brain, gate, _ = make_brain(guard=False)
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await say(brain, gate, "यहां पर ग्रीन कोऑर्डिनेट कर रहे हैं", during_bot=True)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await settle_turn()
        assert handled == ["यहां पर ग्रीन कोऑर्डिनेट कर रहे हैं"]

    async def test_multiword_with_bot_quiet_is_never_held(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await say(brain, gate, "मुझे पेमेंट के बारे में बताइए")
        await settle_turn()
        assert handled == ["मुझे पेमेंट के बारे में बताइए"]


class TestTrustedBootstrap:
    async def _dispatch(self, brain, gate, text, **kw):
        await say(brain, gate, text, **kw)
        await settle_turn()

    async def test_identity_confirmed_seeds_an_untrusted_baseline(self):
        brain, gate, baseline = make_brain()
        handled, _ = stub_turn_handler(brain)
        await self._dispatch(brain, gate, "हाँ मैं ही बोल रहा हूँ", level=-28.0)
        assert not baseline.established
        brain._note_trusted_turn("identity_confirmed")
        assert baseline.established and baseline.baseline_dbfs == -28.0
        events = [d for k, d in brain._recorder.events if k == "caller_baseline_trusted"]
        assert events[-1]["reason"] == "identity_confirmed" and events[-1]["seeded"] is True
        # Later quiet speech is now judged against the caller's own level (shadow).
        await self._dispatch(brain, gate, "खाना बन गया क्या", level=-46.0)
        assert level_events(brain)[-1]["label"] == "background_suspect"

    async def test_identifier_validation_hook_seeds(self):
        brain, gate, baseline = make_brain()
        stub_turn_handler(brain)
        await self._dispatch(brain, gate, "सात शून्य शून्य एक", level=-27.0)
        brain._identifier_capture = SimpleNamespace(workflow="wf", node="n", variable="order_id")
        brain._end_identifier_capture(validated=True)
        assert baseline.established and baseline.baseline_dbfs == -27.0
        assert "identifier_validated" in kinds(brain) and "caller_baseline_trusted" in kinds(brain)

    async def test_workflow_advance_needs_two_words_and_refreshes_when_trusted(self):
        brain, gate, baseline = make_brain()
        stub_turn_handler(brain)
        await self._dispatch(brain, gate, "हाँ", level=-25.0, segment_ms=500.0)
        brain._note_trusted_turn("workflow_advanced", min_words=2)
        assert not baseline.established  # a bare yes matches any yes/no edge
        await self._dispatch(brain, gate, "हाँ सही है", level=-29.0)
        brain._note_trusted_turn("workflow_advanced", min_words=2)
        assert baseline.established and baseline.baseline_dbfs == -29.0
        await self._dispatch(brain, gate, "कल कर दूँगा पक्का", level=-31.0)
        brain._note_trusted_turn("workflow_advanced", min_words=2)
        assert baseline.trusted_segments == 2 and baseline.rebased == 1

    async def test_bot_audio_and_rescued_segments_never_seed(self):
        brain, gate, baseline = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await say(brain, gate, "हाँ जी", during_bot=True, segment_ms=600.0)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await settle_turn()
        assert handled == ["हाँ जी"]
        brain._note_trusted_turn("identity_confirmed")
        assert not baseline.established and baseline.trusted_segments == 0
        # A too-short sample does not seed either.
        await self._dispatch(brain, gate, "हाँ", level=-25.0, segment_ms=200.0)
        brain._note_trusted_turn("identity_confirmed")
        assert not baseline.established

    async def test_each_turn_seeds_once(self):
        brain, gate, baseline = make_brain()
        stub_turn_handler(brain)
        await self._dispatch(brain, gate, "हाँ मैं ही हूँ", level=-28.0)
        brain._note_trusted_turn("identity_confirmed")
        brain._note_trusted_turn("workflow_advanced", min_words=2)
        assert baseline.trusted_segments == 1

    async def test_without_a_gate_trusted_hooks_are_inert(self):
        brain, _, _ = make_brain(with_gate=False)
        stub_turn_handler(brain)
        await self._dispatch(brain, None, "हाँ मैं ही हूँ")
        brain._note_trusted_turn("identity_confirmed")
        assert "caller_baseline_trusted" not in kinds(brain)
