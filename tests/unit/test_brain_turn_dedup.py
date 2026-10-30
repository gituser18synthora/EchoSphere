"""Turn commit discipline: one spoken utterance = one committed user turn.

Pins the duplicate-transcript fixes and the provider-authoritative
(end-of-turn) dispatch path:

- an interim transcript never dispatches a turn;
- a duplicate final (same provider identity) never dispatches twice;
- a cumulative re-emission — the previous turn's text re-delivered as the
  prefix of a longer final ("नहीं नहीं करूँगा ना बोल दिया" →
  "नहीं नहीं करूँगा ना बोल दिया Hello") — answers only the unanswered tail;
- with authoritative end-of-turn (Deepgram Flux) the final dispatches
  immediately, without the debounce/pause window;
- eager end-of-turn starts SPECULATIVE decision work only, TurnResumed
  discards it, and the committed turn consumes a matching prefetch;
- short utterances ("No", "हाँ") still work as complete turns.
"""

import asyncio

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from voice_runtime.brain import ConversationBrain
from voice_runtime.frames import STTEagerEndOfTurnFrame, STTTurnResumedFrame

from tests.unit.test_brain_collection_policy import (
    GRACE,
    _RecorderStub,
    _StreamingLLMStub,
)

DOWN = FrameDirection.DOWNSTREAM


def make_brain(*, authoritative=False, llm=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["en-IN", "hi-IN"],
        stt={"provider": "deepgram" if authoritative else "sarvam"},
        system_prompt="You are Test Bot.",
    )
    brain = ConversationBrain(
        config=config, llm=llm or _StreamingLLMStub(),
        recorder=_RecorderStub(),
        finalize_grace=GRACE, finalize_settle=GRACE, complete_endpoint=GRACE,
        short_reply_endpoint=GRACE,
        authoritative_eot=authoritative,
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
    return brain


def flux_final(text, *, turn_index, language="hi", request_id="req-1"):
    return TranscriptionFrame(
        text=text, user_id="u", timestamp=f"t{turn_index}",
        result={
            "type": "TurnInfo", "event": "EndOfTurn",
            "request_id": request_id, "turn_index": turn_index,
            "transcript": text, "words": [], "languages": [language],
            "audio_window_start": 0.0, "audio_window_end": 2.0,
        },
        finalized=True,
    )


def sarvam_final(text, *, stamp):
    return TranscriptionFrame(text=text, user_id="u", timestamp=stamp)


async def settle(brain, factor=4):
    await asyncio.sleep(GRACE * factor)
    for _ in range(12):
        await asyncio.sleep(0)


def user_turns(brain):
    return [t.text for t in brain._recorder.turns if t.role == "user"]


class TestInterimAndDuplicates:
    async def test_interim_transcript_never_dispatches_a_turn(self):
        brain = make_brain()
        await brain.process_frame(
            InterimTranscriptionFrame("नहीं नहीं", "u", "t0"), DOWN
        )
        await settle(brain)
        assert user_turns(brain) == []

    async def test_duplicate_final_does_not_dispatch_twice(self):
        brain = make_brain(authoritative=True)
        frame = flux_final("नहीं मैं नहीं करूँगा", turn_index=1)
        await brain.process_frame(frame, DOWN)
        await settle(brain)
        replay = flux_final("नहीं मैं नहीं करूँगा", turn_index=1)
        await brain.process_frame(replay, DOWN)
        await settle(brain)
        assert user_turns(brain) == ["नहीं मैं नहीं करूँगा"]

    async def test_cumulative_reemission_answers_only_the_tail(self):
        # The live-call bug: one utterance re-delivered with a longer tail
        # became a second full LLM turn repeating the answered words.
        brain = make_brain(authoritative=True)
        await brain.process_frame(
            flux_final("नहीं नहीं करूँगा ना बोल दिया", turn_index=1), DOWN
        )
        await settle(brain)
        await brain.process_frame(
            flux_final("नहीं नहीं करूँगा ना बोल दिया Hello", turn_index=2), DOWN
        )
        await settle(brain)
        assert user_turns(brain) == ["नहीं नहीं करूँगा ना बोल दिया", "Hello"]

    async def test_cumulative_buffer_merge_never_duplicates_text(self):
        # Sarvam path: two finals for the SAME open utterance where the second
        # contains the first (provider re-emitted cumulatively) must produce
        # one turn with the text once.
        brain = make_brain()
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(sarvam_final("मैं कल पेमेंट", stamp="a"), DOWN)
        await brain.process_frame(
            sarvam_final("मैं कल पेमेंट कर दूंगा", stamp="b"), DOWN
        )
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await settle(brain)
        assert user_turns(brain) == ["मैं कल पेमेंट कर दूंगा"]

    async def test_genuine_repeat_is_still_answered(self):
        # A caller genuinely repeating the same words is a real second turn —
        # only STRICT prefix-with-tail re-emissions are folded.
        brain = make_brain(authoritative=True)
        await brain.process_frame(flux_final("हाँ ठीक है", turn_index=1), DOWN)
        await settle(brain)
        await brain.process_frame(flux_final("हाँ ठीक है", turn_index=2), DOWN)
        await settle(brain)
        assert user_turns(brain) == ["हाँ ठीक है", "हाँ ठीक है"]


class TestAuthoritativeEndOfTurn:
    async def test_final_dispatches_immediately_without_debounce(self):
        brain = make_brain(authoritative=True)
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(
            flux_final("Yes, I am speaking.", turn_index=1, language="en"), DOWN
        )
        # No UserStoppedSpeakingFrame yet, no grace sleep: the provider's
        # EndOfTurn IS the endpoint decision.
        for _ in range(12):
            await asyncio.sleep(0)
        assert user_turns(brain) == ["Yes, I am speaking."]

    async def test_short_utterances_are_complete_turns(self):
        brain = make_brain(authoritative=True)
        await brain.process_frame(flux_final("No", turn_index=1, language="en"), DOWN)
        await settle(brain)
        await brain.process_frame(flux_final("हाँ", turn_index=2), DOWN)
        await settle(brain)
        assert user_turns(brain) == ["No", "हाँ"]

    async def test_backchannel_during_bot_audio_is_held(self):
        from pipecat.frames.frames import (
            BotStartedSpeakingFrame,
            BotStoppedSpeakingFrame,
        )

        brain = make_brain(authoritative=True)
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(flux_final("हाँ", turn_index=1), DOWN)
        for _ in range(12):
            await asyncio.sleep(0)
        assert user_turns(brain) == []  # held while the bot is speaking
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await settle(brain)
        assert user_turns(brain) == ["हाँ"]


class TestSpeculativeDecisions:
    async def test_eager_eot_starts_prefetch_and_turn_resumed_discards_it(self):
        brain = make_brain(authoritative=True)
        started = []

        def _prefetch(text_override=None):
            started.append(text_override)

        brain._start_decision_prefetch = _prefetch
        await brain.process_frame(
            STTEagerEndOfTurnFrame(text="Yes, I am", language="en"), DOWN
        )
        assert started == ["Yes, I am"]
        # The prediction did not hold: speculation must be discarded.
        discarded = []
        brain._discard_decision_prefetch = lambda reason="": discarded.append(reason)
        await brain.process_frame(STTTurnResumedFrame(), DOWN)
        assert discarded == ["turn_resumed"]

    async def test_matching_prefetch_is_consumed_by_the_committed_turn(self):
        brain = make_brain(authoritative=True)

        async def _decision():
            return None

        task = asyncio.get_event_loop().create_task(_decision())
        brain._decision_prefetch = ("Yes, I am speaking.", task)
        consumed = await brain._take_decision("Yes, I am speaking.")
        assert consumed is None  # the prefetch's (stubbed) result
        assert brain._decision_prefetch is None

    async def test_stale_prefetch_for_different_text_is_discarded(self):
        brain = make_brain(authoritative=True)

        async def _decision():
            return "STALE"

        task = asyncio.get_event_loop().create_task(_decision())
        brain._decision_prefetch = ("Yes, I am", task)

        fresh = []

        async def _decide(text, mark=True):
            fresh.append(text)
            return None

        brain._decide_turn = _decide
        await brain._take_decision("Yes, I am speaking.")
        assert fresh == ["Yes, I am speaking."]
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()
