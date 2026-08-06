"""Turn taking and hang-up in the brain.

Turn taking: STT finals are per SEGMENT (Sarvam finalizes on every VAD flush,
~0.2 s of silence), not per utterance — the brain must buffer them and only
run the LLM when the turn controller closes the user's turn
(UserStoppedSpeakingFrame). A caller pausing mid-sentence must never be
answered mid-thought; a transcript with no open turn (quiet utterance the VAD
missed, or STT finalizing late) runs immediately; barge-in still cancels
in-flight generation.

Hang-up: deterministic Hindi/Hinglish/English detection acts on the segment
itself — current audio is interrupted, one short acknowledgement in the
caller's language plays, the worker ends, and no later STT event may produce
another response.
"""

import asyncio

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndWorkerFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.phrases import canned
from voice_runtime.brain import ConversationBrain
from voice_runtime.pipeline import (
    TURN_DETECTION_DEFAULTS,
    resolve_turn_detection,
)


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-test"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0}
        self.turns = []
        self.language = "hi-IN"

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        self.turns.append(turn)

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))

    def event_kinds(self):
        return [kind for kind, _ in self.events]


GRACE = 0.05  # fast end-of-turn debounce for tests


def make_brain(language="hi-IN", languages=("hi-IN",)) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language=language, languages=list(languages),
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(), finalize_grace=GRACE
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify

    # Unit harness: the pipeline task manager is not set up, so create_task /
    # cancel_task are backed by the plain event loop.
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
    return brain


def stub_turn_handler(brain, *, block: bool = False):
    """Replace _handle_turn with a recorder; block=True keeps it in-flight."""
    handled = []
    started = asyncio.Event()

    async def _handle(text):
        handled.append(text)
        started.set()
        if block:
            await asyncio.sleep(30)

    brain._handle_turn = _handle
    return handled, started


def transcript(text, language="hi-IN"):
    return TranscriptionFrame(text=text, user_id="u", timestamp="t", language=language)


async def settle():
    """Let created tasks run to their first await."""
    for _ in range(3):
        await asyncio.sleep(0)


async def settle_turn():
    """Let the end-of-turn debounce elapse and the turn task start."""
    await asyncio.sleep(GRACE * 2)
    await settle()


class TestTurnGating:
    async def test_segment_during_open_turn_does_not_trigger_llm(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("नहीं, मेरे पास"), FrameDirection.DOWNSTREAM)
        await settle()
        assert handled == []  # partial thought — the LLM must not run yet

    async def test_mid_sentence_pause_merges_segments_on_turn_end(self):
        # The caller pauses mid-sentence: VAD flush finalizes segment 1, the
        # caller resumes within the pause window (turn never closes), segment
        # 2 arrives, and only the CLOSED turn runs — with the full sentence.
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("नहीं, मेरे पास"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("अभी पैसा नहीं है"), FrameDirection.DOWNSTREAM)
        await settle()
        assert handled == []
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle()
        assert handled == []  # debounce still waiting for straggler finals
        await settle_turn()
        assert handled == ["नहीं, मेरे पास अभी पैसा नहीं है"]

    async def test_transcript_without_open_turn_runs_after_grace(self):
        # A quiet "हाँ" the VAD missed runs after only the short debounce —
        # the caller is already silent, no turn-close signal will come.
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(transcript("हाँ बोलिए"), FrameDirection.DOWNSTREAM)
        await settle()
        assert handled == []
        await settle_turn()
        assert handled == ["हाँ बोलिए"]

    async def test_late_final_after_turn_close_runs_after_grace(self):
        # STT slower than the turn policy: turn closes with an empty buffer,
        # the transcript lands moments later and runs after the debounce.
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert handled == []
        await brain.process_frame(transcript("हाँ ठीक है"), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert handled == ["हाँ ठीक है"]

    async def test_empty_turn_produces_nothing(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle()
        assert handled == []

    async def test_barge_in_cancels_generation_and_forwards_interruption(self):
        brain = make_brain()
        handled, started = stub_turn_handler(brain, block=True)
        await brain.process_frame(transcript("पेमेंट कब तक करना है"), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 1)
        generation = brain._generation
        assert generation is not None and not generation.done()

        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle()
        assert generation.cancelled()
        assert brain._generation is None
        # The interruption frame itself must keep flowing to the TTS/transport
        # so playing audio is cleared (barge-in stays supported).
        assert any(isinstance(f, UserStartedSpeakingFrame) for f in brain._pushed)
        assert "generation_cancelled" in brain._recorder.event_kinds()

    async def test_new_turn_replaces_in_flight_generation(self):
        brain = make_brain()
        handled, started = stub_turn_handler(brain, block=True)
        await brain.process_frame(transcript("पहला सवाल यह है"), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 1)
        started.clear()
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("दूसरा सवाल यह है"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 1)
        assert handled == ["पहला सवाल यह है", "दूसरा सवाल यह है"]


class TestSegmentsHeldDuringBotAudio:
    """Orphan finals while the bot is audibly speaking must not cut the reply.

    The turn controller's word gate declined to interrupt for these segments
    (backchannel or noise below the barge-in threshold); if the brain then ran
    a turn for them anyway, `_consume_pending_turn` would cancel the audible
    generation and recreate the mid-sentence chop the gate exists to prevent.
    """

    async def test_segment_during_bot_audio_waits_for_reply_end(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("मुझे कुछ पूछना है"), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert handled == []  # held: the reply is still playing
        assert "stt_segment_held_during_bot_audio" in brain._recorder.event_kinds()
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert handled == ["मुझे कुछ पूछना है"]

    async def test_held_segment_does_not_cancel_playing_reply(self):
        brain = make_brain()
        handled, started = stub_turn_handler(brain, block=True)
        await brain.process_frame(transcript("पेमेंट कब तक करना है"), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 1)
        generation = brain._generation
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("हाँ ठीक है"), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert not generation.cancelled()  # backchannel never chops the reply

    async def test_held_segment_joins_confirmed_barge_in_turn(self):
        # The word gate confirms a real interruption: the controller opens the
        # turn (UserStartedSpeakingFrame), the reply is cancelled, and the held
        # segment runs merged with the rest of the caller's utterance.
        brain = make_brain()
        handled, started = stub_turn_handler(brain, block=True)
        await brain.process_frame(transcript("पेमेंट कब तक करना है"), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 1)
        started.clear()
        generation = brain._generation
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("एक मिनट"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle()
        assert generation.cancelled()
        await brain.process_frame(transcript("मेरी बात सुनिए"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 1)
        assert handled[-1] == "एक मिनट मेरी बात सुनिए"

    async def test_hangup_during_bot_audio_is_still_immediate(self):
        # The word gate must never delay a hang-up: the brain acts on the
        # segment itself, before any hold/buffer logic.
        brain = make_brain()
        handled, started = stub_turn_handler(brain, block=True)
        await brain.process_frame(transcript("पेमेंट कब तक करना है"), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 1)
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("फोन काट दो"), FrameDirection.DOWNSTREAM)
        await settle()
        assert any(isinstance(f, EndWorkerFrame) for f in brain._pushed)


class TestHangup:
    async def hangup_via(self, brain, text, language="hi-IN"):
        await brain.process_frame(transcript(text, language), FrameDirection.DOWNSTREAM)
        await settle()

    def spoken(self, brain):
        return [f.text for f in brain._pushed if isinstance(f, TextFrame)
                and not isinstance(f, TranscriptionFrame)]

    async def test_hindi_hangup_interrupts_acks_and_ends_worker(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await self.hangup_via(brain, "फोन कट करो")

        assert handled == []  # never reaches workflow/LLM
        assert brain._closing is True
        pushed = brain._pushed
        interrupt_at = next(i for i, f in enumerate(pushed)
                            if isinstance(f, InterruptionFrame))
        end_at = next(i for i, f in enumerate(pushed)
                      if isinstance(f, EndWorkerFrame))
        ack_at = next(i for i, f in enumerate(pushed) if isinstance(f, TextFrame))
        # Playback stops first, the short ack plays, then the worker ends.
        assert interrupt_at < ack_at < end_at
        assert pushed[end_at].reason == "caller_hangup_request"
        assert self.spoken(brain) == [canned("hangup_ack", "hi-IN")]
        assert ("call_control", {"action": "hangup"}) in brain._recorder.events

    async def test_hangup_ack_follows_conversation_language(self):
        brain = make_brain(language="en-IN", languages=("en-IN", "hi-IN"))
        stub_turn_handler(brain)
        await self.hangup_via(brain, "please hang up the call", language="en-IN")
        assert self.spoken(brain) == [canned("hangup_ack", "en-IN")]

    async def test_hinglish_and_english_phrases_hang_up(self):
        for phrase in ("Phone cut karo", "Call band karo", "cut karu",
                       "Disconnect the call", "बस, कॉल खत्म करो"):
            brain = make_brain()
            handled, _ = stub_turn_handler(brain)
            await self.hangup_via(brain, phrase)
            assert brain._closing, phrase
            assert handled == [], phrase
            assert any(isinstance(f, EndWorkerFrame) for f in brain._pushed), phrase

    async def test_negated_hangup_is_a_normal_turn(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await self.hangup_via(brain, "फोन मत काटो, मैं बात करना चाहता हूं")
        await settle_turn()
        assert brain._closing is False
        assert handled  # processed as a normal turn (no open VAD turn)

    async def test_hangup_cancels_in_flight_reply(self):
        brain = make_brain()
        handled, started = stub_turn_handler(brain, block=True)
        await brain.process_frame(transcript("मुझे जानकारी चाहिए"), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 1)
        generation = brain._generation
        await self.hangup_via(brain, "अब फोन रख दो")
        assert generation.cancelled()
        assert any(isinstance(f, EndWorkerFrame) for f in brain._pushed)

    async def test_no_response_after_hangup_started(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await self.hangup_via(brain, "फोन कट करो")
        frames_after = len(brain._pushed)

        # Later STT events, barge-ins and turn boundaries must all be inert.
        await brain.process_frame(transcript("cut karu"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle()
        assert handled == []
        assert len(brain._pushed) == frames_after  # nothing new on the wire
        assert self.spoken(brain) == [canned("hangup_ack", "hi-IN")]  # exactly one ack
        assert "post_hangup_transcript_dropped" in brain._recorder.event_kinds()

    async def test_second_hangup_request_is_idempotent(self):
        brain = make_brain()
        stub_turn_handler(brain)
        await self.hangup_via(brain, "फोन कट करो")
        await self.hangup_via(brain, "call band karo")
        assert sum(isinstance(f, EndWorkerFrame) for f in brain._pushed) == 1

    async def test_router_detected_english_hangup_uses_same_flow(self):
        # No fast-path stub here: the real _handle_turn routes "hang up" to
        # CALL_CONTROL and must end via _begin_hangup (self-cancel safe).
        brain = make_brain(language="en-IN", languages=("en-IN",))
        await brain.process_frame(transcript("hang up", "en-IN"), FrameDirection.DOWNSTREAM)
        for _ in range(20):
            await asyncio.sleep(0)
        assert brain._closing is True
        assert any(isinstance(f, EndWorkerFrame) for f in brain._pushed)
        assert canned("hangup_ack", "en-IN") in self.spoken(brain)

    async def test_hangup_drops_pending_transfer_controls(self):
        brain = make_brain()
        stub_turn_handler(brain)
        brain._queue_control({"type": "telephony_control", "event": "transfer"})
        await self.hangup_via(brain, "फोन कट करो")
        assert brain._pending_controls == []
        assert not any(n.get("event") == "transfer" for n in brain._notified)


class TestCleanup:
    async def test_cleanup_cancels_generation(self):
        brain = make_brain()
        handled, started = stub_turn_handler(brain, block=True)
        await brain.process_frame(transcript("एक सवाल है"), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 1)
        generation = brain._generation
        await brain.cleanup()
        assert generation.cancelled()
        assert brain._generation is None


class TestTurnDetectionConfig:
    def _config(self, turn_detection=None):
        return ResolvedBotConfig(
            tenant_id="t", bot_id="b", bot_name="n", version="1", published=True,
            stt={"provider": "sarvam",
                 "settings": {"turn_detection": turn_detection} if turn_detection else {}},
        )

    def test_transport_aware_defaults(self):
        browser = resolve_turn_detection(self._config(), "browser")
        telephony = resolve_turn_detection(self._config(), "telephony")
        assert browser == TURN_DETECTION_DEFAULTS["browser"]
        assert telephony == TURN_DETECTION_DEFAULTS["telephony"]
        # Telephony (quiet 8 kHz PSTN audio) must be more permissive so short
        # low-energy words still trip the VAD and flush the STT.
        assert telephony["min_volume"] < browser["min_volume"]
        assert telephony["confidence"] <= browser["confidence"]

    def test_endpoint_tolerates_normal_pauses(self):
        # The silence a caller gets before the bot takes the turn is
        # stop_secs + user_speech_timeout — it must comfortably exceed the
        # ~0.2 s VAD flush interval that produces per-segment STT finals. The
        # browser needs extra room for natural conversational pauses.
        for kind in ("browser", "telephony"):
            turn = resolve_turn_detection(self._config(), kind)
            assert turn["stop_secs"] + turn["user_speech_timeout"] >= 0.8
        browser = resolve_turn_detection(self._config(), "browser")
        assert browser["stop_secs"] + browser["user_speech_timeout"] >= 1.4

    def test_bot_overrides_apply(self):
        turn = resolve_turn_detection(
            self._config({"user_speech_timeout": 1.2, "min_volume": 0.3}),
            "telephony",
        )
        assert turn["user_speech_timeout"] == 1.2
        assert turn["min_volume"] == 0.3

    def test_barge_in_word_gate_defaults_on_and_can_be_disabled(self):
        # Default: interruptions while the bot speaks need a 2-word transcript.
        for kind in ("browser", "telephony"):
            assert resolve_turn_detection(self._config(), kind)["barge_in_min_words"] == 2.0
        # 0 is a legitimate override (pure-VAD barge-in); above-bounds clamps.
        assert resolve_turn_detection(
            self._config({"barge_in_min_words": 0}), "browser"
        )["barge_in_min_words"] == 0.0
        assert resolve_turn_detection(
            self._config({"barge_in_min_words": 99}), "browser"
        )["barge_in_min_words"] == 10.0

    def test_overrides_are_clamped_and_typo_safe(self):
        turn = resolve_turn_detection(
            self._config({
                "user_speech_timeout": 30,   # would make the bot unusable
                "stop_secs": 0,              # would never detect an endpoint
                "confidence": "high",        # not a number
            }),
            "browser",
        )
        assert turn["user_speech_timeout"] == 3.0
        assert turn["stop_secs"] == 0.1
        assert turn["confidence"] == TURN_DETECTION_DEFAULTS["browser"]["confidence"]
