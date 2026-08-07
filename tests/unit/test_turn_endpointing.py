"""Adaptive endpointing, turn aggregation, idempotency and latency.

Covers the two production complaints end to end at the brain level:

1. **slow replies** — the turn-detection dead time between the caller finishing
   and the LLM being asked, across Hindi, English and Hinglish, for complete
   utterances, mid-sentence pauses and quick short replies;
2. **false speech** — noise, hallucinations and bot echo must not become
   customer turns, must not reach the LLM, and must not appear in Conversation
   Review.

Timing tests use short (but real) windows and assert ORDERING and BOUNDS rather
than exact durations, so they measure the policy rather than the host's load.
"""

import asyncio
import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from shared.turn_detection import TURN_DETECTION_BOUNDS, TURN_DETECTION_DEFAULTS
from voice_runtime.brain import ConversationBrain
from voice_runtime.endpointing import (
    ends_with_continuation_cue,
    is_short_complete_reply,
    utterance_looks_complete,
)
from voice_runtime.pipeline import resolve_turn_detection
from voice_runtime.turn_metrics import TurnLatencyTracker, VADLatencyProbe

# Real timers, kept small. COMPLETE < SETTLE-window < GRACE so the three paths
# are distinguishable in the assertions below.
GRACE = 0.12
SETTLE = 0.03
COMPLETE = 0.05


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

    def event(self, kind):
        return next(data for name, data in self.events if name == kind)


class _GateStub:
    """Stands in for CallerAudioGate as a source of speech evidence."""

    def __init__(self, snr_db=25.0, during_bot_audio=False):
        self._snapshot = {"snr_db": snr_db, "during_bot_audio": during_bot_audio}

    def speech_snapshot(self):
        return self._snapshot

    def stats(self):
        return {"opens": 1, "suppressed_ms": 0.0}


def make_brain(language="hi-IN", languages=("hi-IN",), *, gate=None,
               latency=None, complete_endpoint=COMPLETE,
               short_reply_endpoint=None):
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language=language, languages=list(languages),
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(),
        finalize_grace=GRACE, finalize_settle=SETTLE,
        complete_endpoint=complete_endpoint,
        short_reply_endpoint=(
            complete_endpoint if short_reply_endpoint is None
            else short_reply_endpoint
        ),
        latency=latency, audio_gate=gate,
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
    return brain


def stub_turn_handler(brain, *, block: bool = False):
    handled = []
    started = asyncio.Event()

    async def _handle(text):
        handled.append(text)
        started.set()
        if block:
            await asyncio.sleep(30)

    brain._handle_turn = _handle
    return handled, started


_seq = iter(range(1, 10_000))


def transcript(text, language="hi-IN", *, request_id=None, timestamp=None,
               metrics=None, **quality):
    """A final STT segment shaped like a real Sarvam streaming result.

    Sarvam's ``request_id`` identifies the socket CONNECTION and is shared by
    every final on it, so it defaults to a fixed value here — a per-utterance
    id would not exercise the real dedup path. Segment identity comes from the
    metrics the provider measured for that segment.
    """
    data = {"is_final": True, "request_id": request_id or "conn-1", **quality}
    data["metrics"] = metrics if metrics is not None else {
        "audio_duration": 1.0 + next(_seq) / 1000,
        "processing_latency": 0.1 + next(_seq) / 10_000,
    }
    frame = TranscriptionFrame(
        text=text, user_id="caller",
        timestamp=timestamp or f"t-{next(_seq)}", language=language,
        result={"type": "data", "data": data, "provider": "sarvam"},
    )
    frame.finalized = True
    return frame


def replay_of(frame):
    """A provider re-delivery of the same final.

    A real replay arrives as a NEW pipecat frame (fresh timestamp, because the
    service stamps it on arrival) carrying the IDENTICAL provider payload —
    which is why identity has to come from the payload, not the timestamp.
    """
    replayed = TranscriptionFrame(
        text=frame.text, user_id="caller",
        timestamp=f"t-{next(_seq)}",           # deliberately different
        language=frame.language,
        result=frame.result,
    )
    replayed.finalized = True
    return replayed


async def settle():
    for _ in range(4):
        await asyncio.sleep(0)


async def wait_for(handled, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not handled and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    return bool(handled)


# ── endpointing heuristics ───────────────────────────────────────────────────


class TestCompletenessHeuristics:
    def test_short_replies_are_complete_in_all_three_languages(self):
        for text in ("haan", "nahi", "ji", "yes", "no", "ok", "theek hai",
                     "हाँ", "नहीं", "जी", "ठीक है", "bilkul", "acha"):
            assert is_short_complete_reply(text), text
            assert utterance_looks_complete(text), text

    def test_trailing_conjunctions_are_not_complete(self):
        for text in ("नहीं मेरे पास अभी", "main payment karunga lekin",
                     "मुझे बताइए कि", "i can pay but", "haan aur",
                     "paisa hai magar", "kal karunga because"):
            assert not utterance_looks_complete(text), text

    def test_trailing_numbers_are_treated_as_unfinished(self):
        # A caller reading out an amount or account number pauses between
        # groups; cutting in there is the worst possible moment.
        assert ends_with_continuation_cue("mera account number 4521")
        assert ends_with_continuation_cue("amount is 12,500")
        assert not utterance_looks_complete("mera account number 4521")

    def test_terminal_punctuation_closes_a_sentence(self):
        assert utterance_looks_complete("मैं कल पेमेंट कर दूंगा.")
        assert utterance_looks_complete("I will pay tomorrow.")
        assert utterance_looks_complete("Kitna amount pending hai?")

    def test_terminal_punctuation_beats_a_trailing_cue_word(self):
        assert not ends_with_continuation_cue("theek hai aur.")

    def test_unpunctuated_long_sentence_is_not_assumed_complete(self):
        # No positive evidence -> keep the conservative window.
        assert not utterance_looks_complete("मैं कल पेमेंट कर दूंगा")
        assert not utterance_looks_complete("i will pay you tomorrow morning")

    def test_trailing_comma_or_filler_is_not_complete(self):
        assert not utterance_looks_complete("नहीं,")
        assert not utterance_looks_complete("so umm")
        assert not utterance_looks_complete("matlab")


# ── adaptive endpointing in the brain ────────────────────────────────────────


class TestAdaptiveEndpointing:
    async def test_short_reply_answers_without_waiting_for_turn_close(self):
        # "हाँ" is self-contained: the caller has paused (that pause is what
        # produced the final) so there is nothing to gain from waiting out the
        # rest of the pause window.
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("हाँ"), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled), "short reply must not wait for turn close"
        assert handled == ["हाँ"]
        assert brain._turn_active is True  # answered while the turn was open

    async def test_english_and_hinglish_short_replies_also_fast_path(self):
        for text, language in (("yes", "en-IN"), ("theek hai", "hi-IN"),
                               ("ok", "en-IN")):
            brain = make_brain(language=language, languages=("hi-IN", "en-IN"))
            handled, _ = stub_turn_handler(brain)
            await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            await brain.process_frame(transcript(text, language), FrameDirection.DOWNSTREAM)
            assert await wait_for(handled), text
            assert handled == [text]

    async def test_mid_sentence_pause_is_not_cut_off(self):
        # The caller pauses after "नहीं मेरे पास अभी" -- a trailing cue means
        # more is coming, so the full window must be honoured.
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("नहीं मेरे पास अभी"), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(COMPLETE * 4)
        assert handled == [], "mid-thought utterance must not be answered early"
        # The caller resumes and finishes: ONE turn, with the whole sentence.
        await brain.process_frame(transcript("पैसा नहीं है"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        assert handled == ["नहीं मेरे पास अभी पैसा नहीं है"]

    async def test_settled_utterance_skips_the_debounce_on_turn_close(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("मुझे जानकारी चाहिए और"), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(SETTLE * 3)          # stragglers have stopped
        started = time.monotonic()
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        elapsed = time.monotonic() - started
        assert elapsed < GRACE, f"settled turn waited {elapsed * 1000:.0f}ms"

    async def test_unsettled_utterance_still_waits_for_stragglers(self):
        # A final that landed just now means the STT is still delivering: the
        # debounce must still run so the tail joins the same turn.
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("मुझे जानकारी चाहिए और"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle()
        assert handled == []
        await brain.process_frame(transcript("payment ke baare mein"), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        assert handled == ["मुझे जानकारी चाहिए और payment ke baare mein"]

    async def test_turn_close_never_delays_an_armed_fast_endpoint(self):
        # Regression: re-arming the debounce on turn close used to replace an
        # already-armed short endpoint with a LONGER one.
        brain = make_brain(complete_endpoint=0.25)
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("हाँ"), FrameDirection.DOWNSTREAM)
        armed = brain._finalize_task
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert brain._finalize_task is armed, "the armed endpoint was replaced"
        assert await wait_for(handled)

    async def test_bare_acknowledgement_uses_the_tighter_window(self):
        # "haan" cannot be the first half of a longer thought the way a closed
        # sentence can, so it is dispatched on short_reply_endpoint.
        brain = make_brain(complete_endpoint=0.5, short_reply_endpoint=0.02)
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        started = time.monotonic()
        await brain.process_frame(transcript("हाँ"), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        # Well inside complete_endpoint: the short window was the one used.
        assert time.monotonic() - started < 0.3

    async def test_closed_sentence_keeps_the_complete_endpoint(self):
        # The counterpart: a full sentence still gets the conservative window,
        # because the caller may be pausing between two sentences.
        brain = make_brain(complete_endpoint=0.3, short_reply_endpoint=0.02)
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(
            transcript("हाँ, मैं बोल रहा हूँ।"), FrameDirection.DOWNSTREAM
        )
        await asyncio.sleep(0.1)  # inside complete_endpoint, past the short one
        assert handled == [], "a closed sentence fired on the short-reply window"
        assert await wait_for(handled)

    async def test_early_endpoint_is_rolled_back_if_the_caller_continues(self):
        # The safety net for an optimistic endpoint: the reply in flight is
        # cancelled, the partial user turn rewound, and the COMBINED utterance
        # re-run as one turn -- never a talk-over, never two LLM turns.
        brain = make_brain()
        handled, started = stub_turn_handler(brain, block=True)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("हाँ"), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 2)
        brain._open_turn_text = "हाँ"           # as _handle_turn would mark it
        started.clear()
        await brain.process_frame(transcript("मैं कल कर दूंगा"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 2)
        assert handled == ["हाँ", "हाँ मैं कल कर दूंगा"]
        assert "turn_merged_late_final" in brain._recorder.event_kinds()

    async def test_resuming_before_any_reply_audio_merges_into_one_turn(self):
        # The adaptive endpoint answered "हाँ" and the caller then carries on.
        # No reply audio has reached them, so they are not interrupting -- the
        # fragment must be rewound and the finished utterance run ONCE.
        brain = make_brain()
        handled, started = stub_turn_handler(brain, block=True)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("हाँ"), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 2)
        brain._open_turn_text = "हाँ"           # as _handle_turn marks it
        brain._history.append({"role": "user", "content": "हाँ"})
        started.clear()

        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert brain._pending_segments == ["हाँ"]   # rewound, not abandoned
        assert brain._history == []                  # fragment left no history
        await brain.process_frame(transcript("मैं कल कर दूंगा"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 2)
        assert handled == ["हाँ", "हाँ मैं कल कर दूंगा"]

    async def test_interrupting_audible_speech_is_a_real_barge_in(self):
        # Once the reply has been heard, resuming IS an interruption: the turn
        # stands and the new speech becomes its own turn.
        brain = make_brain()
        handled, started = stub_turn_handler(brain, block=True)
        await brain.process_frame(transcript("मुझे जानकारी चाहिए"), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 2)
        brain._open_turn_text = "मुझे जानकारी चाहिए"
        brain._history.append({"role": "user", "content": "मुझे जानकारी चाहिए"})
        # The caller heard the reply: its first audio reached the wire.
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        started.clear()

        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert brain._pending_segments == []         # nothing rewound
        assert brain._history == [{"role": "user", "content": "मुझे जानकारी चाहिए"}]
        assert "generation_cancelled" in brain._recorder.event_kinds()

    async def test_barge_in_cancels_an_armed_fast_endpoint(self):
        brain = make_brain(complete_endpoint=0.4)
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("हाँ"), FrameDirection.DOWNSTREAM)
        # The caller keeps going: the resumed speech must hold the buffer.
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(0.1)
        assert handled == []
        assert brain._pending_segments == ["हाँ"]


# ── one utterance, one LLM turn ──────────────────────────────────────────────


class TestSegmentAggregation:
    async def test_many_finals_for_one_utterance_make_one_turn(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        for part in ("नमस्ते", "मैं बात कर रहा हूं", "अपने लोन के बारे में"):
            await brain.process_frame(transcript(part), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        assert handled == ["नमस्ते मैं बात कर रहा हूं अपने लोन के बारे में"]

    async def test_hinglish_segments_aggregate_into_one_turn(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("haan bhai main"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("kal payment kar dunga"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        assert handled == ["haan bhai main kal payment kar dunga"]


# ── idempotency ──────────────────────────────────────────────────────────────


class TestDuplicateFinals:
    async def test_distinct_finals_sharing_a_connection_id_all_count(self):
        # THE regression that matters: Sarvam sends one request_id per socket
        # connection, shared by every final on it. Keying dedup on that id
        # silenced the bot after the first utterance of each connection.
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        for text in ("नमस्ते", "मुझे बात करनी है", "अपने लोन के बारे में"):
            await brain.process_frame(
                transcript(text, request_id="conn-shared"), FrameDirection.DOWNSTREAM
            )
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        assert handled == ["नमस्ते मुझे बात करनी है अपने लोन के बारे में"]
        assert "stt_duplicate_final_dropped" not in brain._recorder.event_kinds()

    async def test_replayed_provider_payload_is_dropped(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        original = transcript("मुझे समय चाहिए")
        await brain.process_frame(original, FrameDirection.DOWNSTREAM)
        await brain.process_frame(replay_of(original), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        assert handled == ["मुझे समय चाहिए"]  # not doubled
        assert "stt_duplicate_final_dropped" in brain._recorder.event_kinds()

    async def test_replay_after_the_turn_ran_does_not_open_a_second_turn(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        original = transcript("हाँ ठीक है")
        await brain.process_frame(original, FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        await brain.process_frame(replay_of(original), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(GRACE * 2)
        assert handled == ["हाँ ठीक है"]

    async def test_identical_frame_without_request_id_is_deduped_by_timestamp(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        frame = TranscriptionFrame(text="हाँ जी", user_id="c", timestamp="ts-fixed",
                                   language="hi-IN")
        await brain.process_frame(frame, FrameDirection.DOWNSTREAM)
        await brain.process_frame(frame, FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        assert handled == ["हाँ जी"]

    async def test_genuine_repetition_is_kept(self):
        # A caller really saying "हाँ… हाँ" carries distinct provider ids and
        # must NOT be collapsed.
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(
            transcript("हाँ", request_id="a"), FrameDirection.DOWNSTREAM
        )
        await brain.process_frame(
            transcript("हाँ", request_id="b"), FrameDirection.DOWNSTREAM
        )
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        assert handled == ["हाँ हाँ"]


# ── noise, echo and hallucinations never become turns ────────────────────────


class TestNoiseNeverBecomesATurn:
    async def test_unsupported_language_hallucination_is_rejected(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(
            transcript("என்ன சொல்கிறீர்கள்", "ta-IN"), FrameDirection.DOWNSTREAM
        )
        await asyncio.sleep(GRACE * 2)
        assert handled == []
        assert brain._recorder.turns == []
        assert "stt_segment_rejected" in brain._recorder.event_kinds()

    async def test_low_snr_single_token_is_rejected_by_combined_evidence(self):
        # Neither signal rejects alone; together they do. A lone token at the
        # noise floor is the classic "STT invented a word out of hiss".
        brain = make_brain(gate=_GateStub(snr_db=2.0))
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(transcript("क्या"), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(GRACE * 2)
        assert handled == []
        reason = brain._recorder.event("stt_segment_rejected")["reason"]
        assert reason.startswith("weak_signal:")
        assert "low_snr" in reason and "single_token" in reason

    async def test_same_token_at_healthy_snr_is_accepted(self):
        brain = make_brain(gate=_GateStub(snr_db=25.0))
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(transcript("क्या"), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        assert handled == ["क्या"]

    async def test_bot_echo_transcript_is_rejected(self):
        # Captured while the bot was speaking AND barely above the floor: the
        # transcript is the bot's own words coming back.
        brain = make_brain(gate=_GateStub(snr_db=3.0, during_bot_audio=True))
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(
            transcript("आपका पेमेंट बाकी है"), FrameDirection.DOWNSTREAM
        )
        await asyncio.sleep(GRACE * 2)
        assert handled == []
        assert brain._recorder.event("stt_segment_rejected")["during_bot_audio"] is True

    async def test_real_barge_in_during_bot_audio_is_accepted(self):
        # Loud speech during bot audio is a genuine interruption: one weak
        # signal (overlap) is not enough to reject it.
        brain = make_brain(gate=_GateStub(snr_db=22.0, during_bot_audio=True))
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(
            transcript("रुकिए मुझे बात करनी है"), FrameDirection.DOWNSTREAM
        )
        assert await wait_for(handled)
        assert handled == ["रुकिए मुझे बात करनी है"]

    async def test_short_reply_survives_a_noisy_line(self):
        # "haan" on a bad handset trips low_snr AND single_token, but a known
        # short reply must never be dropped by the combination rule.
        brain = make_brain(gate=_GateStub(snr_db=1.0))
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(transcript("हाँ"), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        assert handled == ["हाँ"]

    async def test_rejected_segments_are_absent_from_conversation_review(self):
        # Conversation Review renders recorder TURNS; rejections are events.
        brain = make_brain(gate=_GateStub(snr_db=1.5))
        stub_turn_handler(brain)
        for text, language in (("என்ன", "ta-IN"), ("क्या", "hi-IN"),
                               ("ザ", "ja-JP")):
            await brain.process_frame(transcript(text, language), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(GRACE * 2)
        assert [t.text for t in brain._recorder.turns] == []
        rejected = [d for k, d in brain._recorder.events if k == "stt_segment_rejected"]
        assert len(rejected) == 3
        # A diagnostic record exists, with a reason, and carries no audio.
        assert all(d.get("reason") for d in rejected)
        assert all("audio" not in d for d in rejected)


# ── partial transcripts ──────────────────────────────────────────────────────


class TestPartialTranscripts:
    async def test_partials_reach_the_client_but_never_the_llm_or_history(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        for text in ("मैं", "मैं कल", "मैं कल पेमेंट"):
            await brain.process_frame(
                InterimTranscriptionFrame(text=text, user_id="c", timestamp="t",
                                          language="hi-IN"),
                FrameDirection.DOWNSTREAM,
            )
        await asyncio.sleep(GRACE * 2)
        assert handled == []
        assert brain._recorder.turns == []
        assert brain._history == []
        partials = [n for n in brain._notified if n.get("type") == "partial_transcript"]
        assert [p["text"] for p in partials] == ["मैं", "मैं कल", "मैं कल पेमेंट"]

    async def test_final_covering_the_partials_is_the_only_turn(self):
        brain = make_brain()
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(
            InterimTranscriptionFrame(text="मैं कल", user_id="c", timestamp="t",
                                      language="hi-IN"),
            FrameDirection.DOWNSTREAM,
        )
        await brain.process_frame(
            transcript("मैं कल पेमेंट कर दूंगा"), FrameDirection.DOWNSTREAM
        )
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await wait_for(handled)
        assert handled == ["मैं कल पेमेंट कर दूंगा"]

    async def test_final_agreeing_with_its_partials_is_not_penalised(self):
        brain = make_brain(gate=_GateStub(snr_db=5.0))
        handled, _ = stub_turn_handler(brain)
        await brain.process_frame(
            InterimTranscriptionFrame(text="क्या", user_id="c", timestamp="t",
                                      language="hi-IN"),
            FrameDirection.DOWNSTREAM,
        )
        # low_snr + single_token would reject, but the partials corroborate the
        # final, so agreement is high and unstable_transcript does not fire.
        await brain.process_frame(transcript("क्या"), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(GRACE * 2)
        reasons = [d["reason"] for k, d in brain._recorder.events
                   if k == "stt_segment_rejected"]
        assert all("unstable_transcript" not in r for r in reasons)


# ── latency instrumentation ──────────────────────────────────────────────────


class TestLatencyInstrumentation:
    def test_spans_are_computed_from_the_marks(self):
        tracker = TurnLatencyTracker(session_id="s")
        now = time.monotonic()
        tracker.bot_stop_gap_ms = 1000.0
        tracker.speech_started_at = now + 1.0
        tracker.speech_stopped_at = now + 2.5
        tracker.last_final_at = now + 2.8
        tracker.dispatched_at = now + 3.0
        tracker.llm_first_token_at = now + 3.6
        tracker.bot_started_at = now + 4.0
        spans = tracker.snapshot()
        assert spans["bot_stop_to_speech"] == 1000.0
        assert spans["speech"] == 1500.0
        assert spans["stt_final"] == 300.0
        assert spans["endpoint"] == 500.0
        assert spans["llm_first_token"] == 600.0
        assert spans["tts_first_audio"] == 400.0
        assert spans["response"] == 1500.0

    def test_stage_spans_attribute_the_think_time(self):
        """llm_first_token/tts_first_audio must decompose, not just bracket."""
        tracker = TurnLatencyTracker(session_id="s")
        now = time.monotonic()
        tracker.speech_stopped_at = now + 2.5
        tracker.dispatched_at = now + 3.0
        tracker.classified_at = now + 3.2          # classify 200ms
        tracker.tool_done_at = now + 3.3           # tool 100ms
        tracker.llm_request_at = now + 3.3
        tracker.llm_first_token_at = now + 3.6     # llm_ttft 300ms
        tracker.tts_request_at = now + 3.7         # tts_queue 100ms
        tracker.tts_first_byte_at = now + 3.95     # tts_ttfb 250ms
        tracker.bot_started_at = now + 4.0         # playout 50ms
        spans = tracker.snapshot()

        assert spans["classify"] == 200.0
        assert spans["tool"] == 100.0
        assert spans["llm_ttft"] == 300.0
        assert spans["tts_queue"] == 100.0
        assert spans["tts_ttfb"] == 250.0
        assert spans["playout"] == 50.0
        # The parts account for the brackets they sit inside.
        assert (
            spans["classify"] + spans["tool"] + spans["llm_ttft"]
            == spans["llm_first_token"]
        )
        assert (
            spans["tts_queue"] + spans["tts_ttfb"] + spans["playout"]
            == spans["tts_first_audio"]
        )

    def test_stage_spans_absent_when_the_stage_did_not_run(self):
        """A deterministic route skips classification; nothing is invented."""
        tracker = TurnLatencyTracker()
        tracker.mark_dispatched()
        tracker.mark_llm_request()
        tracker.mark_llm_first_token()
        spans = tracker.snapshot()

        assert "classify" not in spans
        assert "tool" not in spans
        assert "llm_ttft" in spans

    def test_llm_request_mark_follows_a_pre_first_token_retry(self):
        """A retry re-sends the request; ttft belongs to the LAST attempt."""
        tracker = TurnLatencyTracker()
        tracker.mark_llm_request()
        first = tracker.llm_request_at
        tracker.mark_llm_request()
        assert tracker.llm_request_at != first

    def test_tts_marks_keep_the_first_dispatch_of_the_turn(self):
        """Later sentences stream inside the same turn — the first one counts."""
        tracker = TurnLatencyTracker()
        tracker.mark_tts_request()
        first_request = tracker.tts_request_at
        tracker.mark_tts_first_byte()
        first_byte = tracker.tts_first_byte_at
        tracker.mark_tts_request()
        tracker.mark_tts_first_byte()
        assert tracker.tts_request_at == first_request
        assert tracker.tts_first_byte_at == first_byte

    def test_absent_marks_are_omitted_never_guessed(self):
        tracker = TurnLatencyTracker()
        assert tracker.snapshot() == {}
        tracker.mark_speech_started()
        tracker.mark_speech_stopped()
        assert set(tracker.snapshot()) == {"speech"}

    def test_repeated_marks_do_not_move_a_measured_boundary(self):
        tracker = TurnLatencyTracker()
        tracker.mark_llm_first_token()
        first = tracker.llm_first_token_at
        tracker.mark_llm_first_token()
        assert tracker.llm_first_token_at == first
        tracker.mark_bot_started_speaking()
        started = tracker.bot_started_at
        tracker.mark_bot_started_speaking()
        assert tracker.bot_started_at == started

    def test_new_utterance_resolves_the_gap_and_clears_stale_marks(self):
        tracker = TurnLatencyTracker()
        tracker.mark_bot_stopped_speaking()
        tracker.mark_dispatched()
        tracker.mark_speech_started()
        assert tracker.bot_stop_gap_ms is not None  # resolved at speech start
        assert tracker.dispatched_at is None        # previous turn's marks cleared

    def test_bot_speaking_clears_the_stale_response_gap_reference(self):
        # A caller who cuts in while the bot talks is barging in, not replying:
        # reporting the gap since the PREVIOUS reply ended would be misleading.
        tracker = TurnLatencyTracker()
        tracker.mark_bot_stopped_speaking()
        tracker.mark_bot_started_speaking()
        assert tracker.bot_stopped_at is None
        tracker.mark_speech_started()
        tracker.mark_speech_stopped()
        assert "bot_stop_to_speech" not in tracker.snapshot()

    async def test_vad_probe_stamps_speech_boundaries_and_forwards_frames(self):
        tracker = TurnLatencyTracker()
        probe = VADLatencyProbe(tracker)
        pushed = []

        async def _push(frame, direction=None):
            pushed.append(frame)

        probe.push_frame = _push
        start = VADUserStartedSpeakingFrame()
        stop = VADUserStoppedSpeakingFrame(stop_secs=0.2)
        await probe.process_frame(start, FrameDirection.DOWNSTREAM)
        await probe.process_frame(stop, FrameDirection.DOWNSTREAM)
        assert tracker.speech_started_at is not None
        assert tracker.speech_stopped_at is not None
        assert pushed == [start, stop]  # a probe must never swallow a frame

    async def test_brain_reports_end_of_speech_to_bot_audio(self):
        tracker = TurnLatencyTracker(session_id="s-test")
        brain = make_brain(latency=tracker)
        stub_turn_handler(brain)
        tracker.mark_speech_started()
        tracker.mark_speech_stopped()
        await brain.process_frame(transcript("हाँ"), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(COMPLETE * 3)
        tracker.mark_llm_first_token()
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        spans = brain._recorder.event("turn_latency")
        assert spans["response"] > 0
        assert spans["endpoint"] > 0
        assert spans["stt_final"] >= 0

    async def test_latency_is_reported_once_per_turn(self):
        tracker = TurnLatencyTracker(session_id="s")
        brain = make_brain(latency=tracker)
        stub_turn_handler(brain)
        tracker.mark_speech_started()
        tracker.mark_speech_stopped()
        tracker.mark_dispatched()
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        assert brain._recorder.event_kinds().count("turn_latency") == 1

    async def test_bot_stop_to_speech_gap_is_measured(self):
        tracker = TurnLatencyTracker(session_id="s")
        brain = make_brain(latency=tracker)
        stub_turn_handler(brain)
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        tracker.mark_speech_started()
        tracker.mark_speech_stopped()
        tracker.mark_dispatched()
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        assert brain._recorder.event("turn_latency")["bot_stop_to_speech"] >= 40

    async def test_latency_lands_on_the_bot_turn_for_review(self):
        from voice_runtime.recording import TurnRecord

        tracker = TurnLatencyTracker(session_id="s")
        brain = make_brain(latency=tracker)
        record = TurnRecord(role="bot", text="ठीक है", latency_ms={"retrieval": 0.0})
        brain._recorder.add_turn(record)
        brain._pending_latency_record = record
        tracker.mark_speech_started()
        tracker.mark_speech_stopped()
        tracker.mark_dispatched()
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        assert "response" in record.latency_ms
        assert record.latency_ms["retrieval"] == 0.0  # existing keys preserved


# ── configuration ────────────────────────────────────────────────────────────


class TestEndpointConfiguration:
    def _config(self, turn_detection=None):
        return ResolvedBotConfig(
            tenant_id="t", bot_id="b", bot_name="n", version="1", published=True,
            stt={"provider": "sarvam",
                 "settings": {"turn_detection": turn_detection} if turn_detection else {}},
        )

    def test_new_parameters_have_transport_defaults_within_bounds(self):
        for transport in ("browser", "telephony"):
            turn = resolve_turn_detection(self._config(), transport)
            for key in ("finalize_settle", "complete_endpoint",
                        "short_reply_endpoint"):
                low, high = TURN_DETECTION_BOUNDS[key]
                assert low <= turn[key] <= high, f"{transport}.{key}"

    def test_short_reply_endpoint_is_the_tightest_window(self):
        # "haan" must not wait as long as a closed sentence, which in turn must
        # not wait as long as a pause.
        for transport in ("browser", "telephony"):
            d = TURN_DETECTION_DEFAULTS[transport]
            assert (
                d["short_reply_endpoint"]
                < d["complete_endpoint"]
                < d["user_speech_timeout"]
            ), transport

    def test_complete_endpoint_is_shorter_than_the_full_window(self):
        # The whole point: a finished thought is answered sooner than a pause.
        for transport in ("browser", "telephony"):
            d = TURN_DETECTION_DEFAULTS[transport]
            assert d["complete_endpoint"] < d["user_speech_timeout"]

    def test_pause_tolerance_is_unchanged_by_this_work(self):
        # The window a caller gets to resume mid-sentence must not shrink.
        for transport in ("browser", "telephony"):
            turn = resolve_turn_detection(self._config(), transport)
            assert turn["stop_secs"] + turn["user_speech_timeout"] >= 0.8
        browser = resolve_turn_detection(self._config(), "browser")
        assert browser["stop_secs"] + browser["user_speech_timeout"] >= 1.4

    def test_telephony_defaults_are_the_tuned_values(self):
        # The 2026-08 latency work: every value here is caller-audible dead
        # time appended to EVERY telephony turn. Change deliberately.
        d = TURN_DETECTION_DEFAULTS["telephony"]
        assert d["user_speech_timeout"] == 0.7
        assert d["finalize_grace"] == 0.12
        assert d["finalize_settle"] == 0.1
        assert d["complete_endpoint"] == 0.2
        assert d["short_reply_endpoint"] == 0.1

    def test_browser_defaults_stay_conservative(self):
        # Browser endpoints were NOT tightened with telephony: browser tests
        # showed noise-triggered early endpoints are costlier there.
        b = TURN_DETECTION_DEFAULTS["browser"]
        assert b["user_speech_timeout"] == 1.2
        assert b["finalize_grace"] == 0.3
        assert b["finalize_settle"] == 0.15
        assert b["complete_endpoint"] == 0.35
        for key in ("user_speech_timeout", "finalize_grace",
                    "finalize_settle", "complete_endpoint"):
            assert b[key] >= TURN_DETECTION_DEFAULTS["telephony"][key], key

    def test_incomplete_utterances_keep_a_longer_window_than_short_replies(self):
        # The safety rule the tuning must preserve: mid-thought speech waits
        # the FULL pause window (stop_secs + user_speech_timeout); a short
        # complete reply answers on the much tighter short_reply_endpoint.
        for transport in ("browser", "telephony"):
            d = TURN_DETECTION_DEFAULTS[transport]
            full_window = d["stop_secs"] + d["user_speech_timeout"]
            assert full_window >= 4 * d["short_reply_endpoint"], transport
            assert full_window >= 2 * d["complete_endpoint"], transport

    def test_overrides_are_clamped(self):
        turn = resolve_turn_detection(
            self._config({"complete_endpoint": 99, "finalize_settle": -5}),
            "telephony",
        )
        assert turn["complete_endpoint"] == TURN_DETECTION_BOUNDS["complete_endpoint"][1]
        assert turn["finalize_settle"] == TURN_DETECTION_BOUNDS["finalize_settle"][0]
