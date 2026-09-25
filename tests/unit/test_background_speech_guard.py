"""Background-speech guard in the brain (voice_runtime.caller_level policy).

A final that passed the transcript gate but sits far below the caller's own
established speech level is another speaker's: it is HELD (never a turn on
its own), never dispatched when the bot stops speaking, and never allowed to
hang up or steer the call. The caller repeating themselves at the quieter
level reconfirms it; caller-level speech discards it. Before a baseline
exists, and with the guard off (shadow mode), behaviour is unchanged.
"""

import asyncio

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from tests.unit.test_brain_turn_taking import (
    GRACE,
    _RecorderStub,
    settle_turn,
    stub_turn_handler,
)
from voice_runtime.brain import ConversationBrain
from voice_runtime.caller_level import CallerLevelBaseline


class FakeGate:
    """Stands in for CallerAudioGate: the brain only reads its snapshot."""

    def __init__(self):
        self.snapshot = None
        self.live_speech_ms = 0.0
        self.segments_started = 0

    def speech_snapshot(self):
        return self.snapshot

    def take_retained_audio(self):
        return None

    def clear_retained_audio(self):
        pass

    def enable_utterance_retention(self, max_seconds=30.0):
        pass

    def disable_utterance_retention(self):
        pass

    def begin_backchannel_window(self):
        pass

    def end_backchannel_window(self):
        pass


def make_brain(*, margin=10.0, allowance=0.0, enforce=True, min_segments=3):
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
    )
    gate = FakeGate()
    baseline = CallerLevelBaseline(
        margin_db=margin, min_segments=min_segments, enforce=enforce,
        bot_audio_allowance_db=allowance,
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(), finalize_grace=GRACE,
        audio_gate=gate, caller_level=baseline,
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify

    def _create_task(coro, name=None):
        return asyncio.get_event_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.create_task = _create_task
    brain.cancel_task = _cancel_task
    return brain, gate, baseline


async def say(brain, gate, text, level, *, segment_ms=1500.0, during_bot=False):
    """One final STT segment whose gate segment measured ``level`` dBFS."""
    gate.snapshot = {
        "snr_db": 30.0, "speech_dbfs": level, "during_bot_audio": during_bot,
        "segment_ms": segment_ms, "live": False,
    }
    frame = TranscriptionFrame(text=text, user_id="u", timestamp="t")
    await brain.process_frame(frame, FrameDirection.DOWNSTREAM)


def level_events(brain):
    return [d for k, d in brain._recorder.events if k == "caller_level_segment"]


async def establish(brain, gate, handled, level=-30.0):
    """A caller turn the call vouches for (the workflow advanced on it) seeds
    the trusted baseline; two more agreeing accepted turns refine it."""
    await say(brain, gate, "हाँ मैं बोल रहा हूँ", level)
    await settle_turn()
    assert not brain._caller_level.established
    brain._note_trusted_turn("workflow_advanced", min_words=2)
    assert brain._caller_level.established
    await say(brain, gate, "मुझे पेमेंट के बारे में बताइए", level + 1.0)
    await settle_turn()
    await say(brain, gate, "कल तक कर दूँगा पक्का", level - 1.0)
    await settle_turn()
    assert len(handled) == 3
    assert brain._caller_level.established
    assert brain._caller_level.baseline_dbfs == level


class TestBaselineLearning:
    async def test_nothing_changes_before_a_baseline_exists(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await say(brain, gate, "यहां पर ग्रीन कोऑर्डिनेट कर रहे हैं", -55.0)
        await settle_turn()
        assert handled == ["यहां पर ग्रीन कोऑर्डिनेट कर रहे हैं"]
        event = level_events(brain)[0]
        assert event["label"] == "unknown" and event["action"] == "accepted"

    async def test_baseline_trains_only_on_reliable_segments(self):
        brain, gate, baseline = make_brain()
        handled, _ = stub_turn_handler(brain)
        await say(brain, gate, "हाँ", -30.0, segment_ms=400.0)  # short: no training
        await settle_turn()
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await say(brain, gate, "हाँ जी सुन रहा हूँ", -30.0, during_bot=True)  # bot audio: no training
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert baseline.segments == 0
        assert [e["trained"] for e in level_events(brain)] == [False, False]
        await say(brain, gate, "मुझे पेमेंट के बारे में बताइए", -30.0)
        await settle_turn()
        assert baseline.segments == 1 and level_events(brain)[-1]["trained"] is True

    async def test_every_segment_is_recorded_with_its_evidence(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        await say(brain, gate, "ठीक है", -33.0, segment_ms=700.0)
        await settle_turn()
        event = level_events(brain)[-1]
        assert event["speech_dbfs"] == -33.0 and event["baseline_dbfs"] == -30.0
        assert event["delta_db"] == -3.0 and event["label"] == "caller"
        assert event["action"] == "accepted" and event["enforced"] is True
        assert event["segment_ms"] == 700.0 and event["bot_speaking"] is False


class TestDuringBotAudio:
    async def test_background_segment_is_held_and_not_dispatched_when_bot_stops(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await say(brain, gate, "यहां पर ग्रीन कोऑर्डिनेट कर रहे हैं", -48.0, during_bot=True)
        await settle_turn()
        event = level_events(brain)[-1]
        assert event["action"] == "held" and event["reason"] == "background_during_bot_audio"
        assert brain._pending_segments == []
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert len(handled) == 3  # the background sentence never became a turn
        kinds = brain._recorder.event_kinds()
        assert "background_suspect_not_dispatched" in kinds
        assert "stt_segment_held_during_bot_audio" not in kinds[-3:]
        # The caller has not shown up: the no-response ladder is running.
        assert brain._silence_task is not None

    async def test_caller_level_segment_during_bot_audio_still_dispatches_after_reply(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await say(brain, gate, "मुझे कुछ पूछना है", -31.0, during_bot=True)
        await settle_turn()
        assert "stt_segment_held_during_bot_audio" in brain._recorder.event_kinds()
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert handled[-1] == "मुझे कुछ पूछना है"

    async def test_bot_audio_allowance_protects_echo_cancelled_barge_ins(self):
        # 15 dB below the caller's quiet-bot level: background if the bot were
        # quiet, but the same caller ducked by their echo canceller while the
        # bot speaks — dispatched as before once the reply ends.
        brain, gate, _ = make_brain(margin=10.0, allowance=12.0)
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await say(brain, gate, "एक मिनट रुकिए ज़रा", -45.0, during_bot=True)
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert handled[-1] == "एक मिनट रुकिए ज़रा"
        assert level_events(brain)[-1]["margin_db"] == 22.0

    async def test_interrupted_reply_resumes_when_the_only_speech_was_background(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        spoken = []

        async def _transient(text, **kwargs):
            spoken.append(text)

        brain._speak_transient = _transient
        brain._last_bot_reply = "आपका बकाया दो हज़ार रुपये है।"
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        # A barge-in confirmed before the level verdict: the reply was cut.
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await say(brain, gate, "टीवी पर समाचार चल रहा है आज", -50.0)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert len(handled) == 3
        assert spoken == ["आपका बकाया दो हज़ार रुपये है।"]
        assert "bot_reply_resumed_after_announcement" in brain._recorder.event_kinds()


class TestBotQuiet:
    async def test_background_segment_with_bot_quiet_is_held(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        await say(brain, gate, "खाना बन गया क्या", -46.0)
        await settle_turn()
        assert len(handled) == 3
        event = level_events(brain)[-1]
        assert event["action"] == "held" and event["reason"] == "background_quiet"
        assert brain._suspect_segments[-1]["text"] == "खाना बन गया क्या"
        assert brain._silence_task is not None  # ladder armed, not disarmed by junk

    async def test_repeated_quiet_speech_is_held_twice_then_fails_open(self):
        # Repetition is not evidence of who spoke: the first two quiet
        # suspects are held (no reconfirmation, no re-basing) and the
        # no-response ladder runs. The level verdict must not lock the
        # caller out for good, though: the third is dispatched anyway.
        brain, gate, baseline = make_brain()
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        for text in ("हाँ जी मैं ही हूँ", "हाँ हाँ मैं ही बोल रहा हूँ"):
            await say(brain, gate, text, -44.0)
            await settle_turn()
        assert len(handled) == 3
        assert all(e["action"] == "held" for e in level_events(brain)[-2:])
        assert brain._silence_task is not None
        await say(brain, gate, "सुन रहे हैं आप मुझे", -44.0)
        await settle_turn()
        assert handled[-1] == "सुन रहे हैं आप मुझे"
        event = level_events(brain)[-1]
        assert event["action"] == "accepted" and event["reason"] == "background_quiet_failopen"
        assert event["trained"] is False
        # Still no level-based re-basing: the baseline is the trusted one.
        assert baseline.baseline_dbfs == -30.0 and baseline.rebased == 1
        assert brain._suspect_segments == []

    async def test_fail_open_turn_the_call_vouches_for_rebases_the_baseline(self):
        # The caller moved away from the handset: held twice, dispatched on
        # the third turn, and once the workflow advances on that turn the
        # baseline follows the caller's new level.
        brain, gate, baseline = make_brain()
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        for text in ("सुनिए मैं यहाँ हूँ", "हेलो सुन रहे हैं आप", "मैं स्पीकर पर बोल रहा हूँ"):
            await say(brain, gate, text, -44.0)
            await settle_turn()
        assert len(handled) == 4, handled
        brain._note_trusted_turn("workflow_advanced", min_words=2)
        assert baseline.rebased == 2 and baseline.baseline_dbfs == -44.0
        await say(brain, gate, "हाँ जी ठीक है समझ गया", -44.0)
        await settle_turn()
        assert len(handled) == 5
        assert level_events(brain)[-1]["label"] == "caller"

    async def test_bot_audio_holds_do_not_count_toward_fail_open(self):
        brain, gate, baseline = make_brain()
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        for text in ("यहां पर लाइट बंद कर दो", "खाना बन गया क्या", "टीवी की आवाज़ कम करो", "बच्चों को बुला लो"):
            await say(brain, gate, text, -44.0, during_bot=True)
            await settle_turn()
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert len(handled) == 3
        reasons = [e["reason"] for e in level_events(brain)[-4:]]
        assert reasons == ["background_during_bot_audio"] * 4, reasons

    async def test_caller_speech_discards_held_background(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        await say(brain, gate, "खाना बन गया क्या", -46.0)
        await settle_turn()
        await say(brain, gate, "नहीं मैं कल पेमेंट करूँगा", -30.0)
        await settle_turn()
        assert handled[-1] == "नहीं मैं कल पेमेंट करूँगा"  # background words never merged
        assert brain._suspect_segments == []
        discarded = [d for k, d in brain._recorder.events if k == "background_suspect_discarded"]
        assert discarded and discarded[-1]["reason"] == "caller_spoke"

    async def test_background_hangup_phrase_does_not_end_the_call(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        await say(brain, gate, "फोन काट दो", -48.0)
        await settle_turn()
        assert brain._closing is False
        assert level_events(brain)[-1]["action"] == "held"

    async def test_background_segment_does_not_steer_the_language(self):
        brain, gate, _ = make_brain()
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        frame = TranscriptionFrame(
            text="the weather is nice today", user_id="u", timestamp="t",
        )
        frame.language = "en-IN"
        gate.snapshot = {"speech_dbfs": -50.0, "during_bot_audio": False, "segment_ms": 1500.0}
        await brain.process_frame(frame, FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert brain._pending_language is None
        assert brain._conversation_language == "hi-IN"


class TestShadowMode:
    async def test_guard_off_records_verdicts_without_acting(self):
        brain, gate, _ = make_brain(enforce=False)
        handled, _ = stub_turn_handler(brain)
        await establish(brain, gate, handled)
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await say(brain, gate, "यहां पर ग्रीन कोऑर्डिनेट कर रहे हैं", -50.0, during_bot=True)
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert handled[-1] == "यहां पर ग्रीन कोऑर्डिनेट कर रहे हैं"  # pre-guard behaviour
        event = level_events(brain)[-1]
        assert event["label"] == "background_suspect"
        assert event["action"] == "accepted" and event["reason"] == "background_suspect_shadow"
        assert event["enforced"] is False


class TestNoGate:
    async def test_brain_without_gate_or_baseline_is_untouched(self):
        config = ResolvedBotConfig(
            tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
            published=True, language="hi-IN", languages=["hi-IN"],
            stt={"provider": "sarvam"}, system_prompt="You are Test.",
        )
        brain = ConversationBrain(
            config=config, llm=None, recorder=_RecorderStub(), finalize_grace=GRACE,
        )
        brain._pushed = []

        async def _push(frame, direction=None):
            brain._pushed.append(frame)

        brain.push_frame = _push
        brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)
        handled, _ = stub_turn_handler(brain)
        frame = TranscriptionFrame(text="हाँ बोल रहा हूँ", user_id="u", timestamp="t")
        await brain.process_frame(frame, FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert handled == ["हाँ बोल रहा हूँ"]
        assert level_events(brain) == []


class _FakeSpeaker:
    """Records calls; attribution alternates so both branches are exercised."""

    def __init__(self):
        self.scored = []
        self.references = []
        self.reference_available = False
        self.reference_seconds = 0.0
        self.mode = "shadow"

    def score(self, pcm, rate, *, seconds, context=None):
        from voice_runtime.speaker_consistency import SpeakerEvidence

        self.scored.append((len(pcm), rate, seconds, dict(context or {})))
        attribution = "caller" if self.reference_available else "unknown"
        return SpeakerEvidence(attribution, self.reference_available, self.reference_seconds, 1 if self.reference_available else 0,
                               seconds, 0.2 if self.reference_available else None, None, 0.35, 0.45, 1.0, "fake", "shadow", False,
                               "scored" if self.reference_available else "no_reference", dict(context or {}))

    def add_reference(self, pcm, rate, *, seconds, reason, during_bot_audio=False):
        from voice_runtime.speaker_consistency import ReferenceUpdate

        self.references.append((len(pcm), seconds, reason, during_bot_audio))
        self.reference_available = True
        self.reference_seconds += seconds
        return ReferenceUpdate(True, "added", self.reference_seconds, len(self.references), None, 1.0)


class _FakeTap:
    def __init__(self, seconds=1.5):
        self.seconds = seconds

    def take_recent(self, max_seconds):
        s = min(self.seconds, max_seconds)
        return bytes(int(s * 8000) * 2), 8000, s


async def _drain(brain):
    for _ in range(6):
        await asyncio.sleep(0)
    for t in list(brain._speaker_tasks):
        await t


class TestSpeakerConsistencyShadow:
    async def test_segments_are_scored_and_vouched_turns_seed_the_reference_without_changing_behaviour(self):
        brain, gate, _ = make_brain(enforce=False)
        spk = _FakeSpeaker()
        brain._speaker = spk
        brain._speaker_tap = _FakeTap(1.5)
        handled, _ = stub_turn_handler(brain)
        await say(brain, gate, "हाँ मैं बोल रहा हूँ", -30.0)
        await settle_turn()
        await _drain(brain)
        # Scored off-path with conversational context, before any reference: unknown.
        assert len(spk.scored) == 1 and spk.scored[0][2] == 1.5
        assert spk.scored[0][3]["question_open"] is False and spk.scored[0][3]["during_bot_audio"] is False
        assert brain._last_speaker_attribution == "unknown"   # no reference yet
        assert handled == ["हाँ मैं बोल रहा हूँ"]        # the turn was handled exactly as before
        # The call vouches for that turn: its audio becomes the reference.
        brain._note_trusted_turn("workflow_advanced", min_words=2)
        await _drain(brain)
        assert spk.references and spk.references[0][2] == "workflow_advanced" and spk.references[0][3] is False
        assert brain._open_turn_audio == []
        # The next turn is attributed against it; still no behavioural effect.
        await say(brain, gate, "मुझे पेमेंट के बारे में बताइए", -31.0)
        await settle_turn()
        await _drain(brain)
        assert handled[-1] == "मुझे पेमेंट के बारे में बताइए"
        assert brain._last_speaker_attribution == "caller"
        ctx = [d for k, d in brain._recorder.events if k == "speaker_turn_context"]
        assert not ctx or ctx[-1]["reference_available"] is True   # emitted at decision time when routing runs

    async def test_bot_audio_segments_never_seed_the_reference(self):
        brain, gate, _ = make_brain(enforce=False)
        spk = _FakeSpeaker()
        brain._speaker = spk
        brain._speaker_tap = _FakeTap(2.0)
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await say(brain, gate, "हाँ जी सुन रहा हूँ", -30.0, during_bot=True)
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        await _drain(brain)
        brain._note_trusted_turn("workflow_advanced", min_words=2)
        await _drain(brain)
        assert spk.references == []
        assert spk.scored and spk.scored[0][3]["during_bot_audio"] is True

    async def test_without_a_component_nothing_changes(self):
        brain, gate, _ = make_brain(enforce=False)
        handled, _ = stub_turn_handler(brain)
        await say(brain, gate, "हाँ मैं बोल रहा हूँ", -30.0)
        await settle_turn()
        assert handled == ["हाँ मैं बोल रहा हूँ"]
        assert "speaker_consistency" not in brain._recorder.event_kinds()
