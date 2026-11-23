"""Recorded short-reply failures: preserve the caller's floor and question.

The 23 November Haier audit found held yes/no finals being dropped, while
pure hums could interrupt by duration alone. These are strategy replays of
those frame patterns, not a claim to reproduce STT or far-end acoustics.
"""

from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult

import voice_runtime.barge_in as module
from voice_runtime.barge_in import WordConfirmedBargeInStrategy
from voice_runtime.caller_level import LevelVerdict


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    return now


def final(text):
    return TranscriptionFrame(text=text, user_id="caller", timestamp="t")


def interim(text):
    return InterimTranscriptionFrame(text=text, user_id="caller", timestamp="t")


def make(**options):
    strategy = WordConfirmedBargeInStrategy(
        min_words=2, provisional_duck=True, commit_secs=1.0,
        **options,
    )
    strategy.trigger_user_turn_started = AsyncMock()
    strategy._on_provisional = AsyncMock()
    return strategy


async def overlap(strategy):
    await strategy.process_frame(BotStartedSpeakingFrame())
    await strategy.process_frame(VADUserStartedSpeakingFrame())


@pytest.mark.parametrize("text", ["हाँ।", "नहीं।", "haan", "No."])
async def test_one_word_final_to_heard_question_gets_the_floor(clock, text):
    s = make(short_final_context=lambda _: "answer")
    await overlap(s)
    clock[0] += .25
    assert await s.process_frame(final(text)) == ProcessFrameResult.STOP
    s.trigger_user_turn_started.assert_awaited_once()
    # A commit never resumes stale reply audio.
    assert s._on_provisional.await_args_list[0].args == (True,)
    assert s._on_provisional.await_count == 1


async def test_hello_requests_attention_without_becoming_an_answer(clock):
    reasons = []
    s = make(short_final_context=lambda _: "attention",
             on_confirmed=lambda reason, _: reasons.append(reason))
    await overlap(s)
    clock[0] += .3
    assert await s.process_frame(final("Hello.")) == ProcessFrameResult.STOP
    assert reasons == ["contextual short final (attention)"]


@pytest.mark.parametrize("context", [None, "unheard", "unknown"])
async def test_unknown_or_unheard_question_does_not_promote_one_word_final(clock, context):
    s = make(short_final_context=lambda _: context)
    await overlap(s)
    clock[0] += .3
    assert await s.process_frame(final("haan")) == ProcessFrameResult.CONTINUE
    s.trigger_user_turn_started.assert_not_awaited()


async def test_short_final_needs_gated_vad_not_just_text(clock):
    s = make(short_final_context=lambda _: "answer")
    await s.process_frame(BotStartedSpeakingFrame())
    assert await s.process_frame(final("हाँ")) == ProcessFrameResult.CONTINUE
    s.trigger_user_turn_started.assert_not_awaited()


async def test_late_provider_final_can_confirm_after_vad_stop_and_resume(clock):
    s = make(short_final_context=lambda _: "answer")
    await overlap(s)
    clock[0] += .3
    await s.process_frame(VADUserStoppedSpeakingFrame())
    assert not s.provisional
    assert [call.args for call in s._on_provisional.await_args_list] == [(True,), (False,)]
    clock[0] += .6
    assert await s.process_frame(final("नहीं")) == ProcessFrameResult.STOP
    # Same final duplicated without a new VAD burst cannot re-interrupt.
    assert await s.process_frame(final("नहीं")) == ProcessFrameResult.CONTINUE
    s.trigger_user_turn_started.assert_awaited_once()


async def test_orphan_delayed_final_does_not_claim_a_later_question(clock):
    s = make(short_final_context=lambda _: "answer")
    await overlap(s)
    clock[0] += .2
    await s.process_frame(VADUserStoppedSpeakingFrame())
    clock[0] += 3
    assert await s.process_frame(final("हाँ")) == ProcessFrameResult.CONTINUE
    s.trigger_user_turn_started.assert_not_awaited()


async def test_short_interim_is_not_promoted_even_with_answer_context(clock):
    s = make(short_final_context=lambda _: "answer")
    await overlap(s)
    clock[0] += .4
    await s.process_frame(interim("हाँ"))
    await s.process_frame(interim("हाँ"))
    s.trigger_user_turn_started.assert_not_awaited()


@pytest.mark.parametrize("text", ["hmm", "hmm hmm", "हम्म।", "... ???",
                                      "Call is now being recorded."])
@pytest.mark.parametrize("frame_builder", [interim, final])
async def test_known_non_answer_does_not_interrupt_by_words_or_duration(clock, text, frame_builder):
    s = make(short_final_context=lambda _: "answer")
    await overlap(s)
    clock[0] += .3
    await s.process_frame(frame_builder(text))
    clock[0] += 1.1
    assert await s.process_frame(Frame()) == ProcessFrameResult.CONTINUE
    await s.process_frame(VADUserStoppedSpeakingFrame())
    assert not s.provisional
    s.trigger_user_turn_started.assert_not_awaited()
    assert [call.args for call in s._on_provisional.await_args_list] == [(True,), (False,)]


async def test_hum_then_real_speech_can_still_interrupt(clock):
    s = make(short_final_context=lambda _: None)
    await overlap(s)
    clock[0] += .4
    await s.process_frame(interim("hmm"))
    clock[0] += .8
    assert await s.process_frame(final("एक मिनट रुकिए")) == ProcessFrameResult.STOP
    s.trigger_user_turn_started.assert_awaited_once()


async def test_new_vad_episode_does_not_inherit_hum_suppression(clock):
    s = make()
    await overlap(s)
    clock[0] += .3
    await s.process_frame(interim("hmm"))
    await s.process_frame(VADUserStoppedSpeakingFrame())
    await s.process_frame(VADUserStartedSpeakingFrame())
    clock[0] += 1.1
    assert await s.process_frame(Frame()) == ProcessFrameResult.STOP


async def test_short_answer_cannot_bypass_caller_level_guard(clock):
    background = LevelVerdict(
        label="background_suspect", level_dbfs=-50, baseline_dbfs=-30,
        delta_db=-20, margin_db=10, baseline_segments=3,
    )
    s = make(short_final_context=lambda _: "answer", speech_classifier=lambda: background)
    await overlap(s)
    clock[0] += .3
    assert await s.process_frame(final("हाँ")) == ProcessFrameResult.CONTINUE
    s.trigger_user_turn_started.assert_not_awaited()


async def test_short_answer_cannot_bypass_start_protection(clock):
    s = make(short_final_context=lambda _: "answer", start_protection=True,
             caller_reference_ready=lambda: False)
    await overlap(s)
    clock[0] += .3
    assert await s.process_frame(final("हाँ")) == ProcessFrameResult.CONTINUE
    s.trigger_user_turn_started.assert_not_awaited()


async def test_short_context_does_not_weaken_explicit_transcript_only_floor(clock):
    s = make(short_final_context=lambda _: "answer", require_transcript=True,
             meaningful_words=2)
    await overlap(s)
    clock[0] += .3
    assert await s.process_frame(final("हाँ")) == ProcessFrameResult.CONTINUE
    s.trigger_user_turn_started.assert_not_awaited()
    s._on_provisional.assert_not_awaited()


async def test_bad_context_callback_preserves_reply(clock):
    def failed(_):
        raise RuntimeError("context unavailable")

    s = make(short_final_context=failed)
    await overlap(s)
    clock[0] += .3
    assert await s.process_frame(final("हाँ")) == ProcessFrameResult.CONTINUE
    s.trigger_user_turn_started.assert_not_awaited()


@pytest.mark.parametrize("text,heard,interrupts", [
    ("हाँ", False, False), ("नहीं", True, True), ("Hello", False, True),
])
async def test_strategy_uses_actual_brain_hearing_context(clock, text, heard, interrupts):
    from tests.unit.test_brain_turn_taking import make_brain

    brain = make_brain()
    try:
        brain._active_workflow = "service_flow"
        brain._pending_workflow_question = "Do you require service?"
        brain._pending_ask_shape = {"kind": "yes_no"}
        brain._bot_speaking = True
        brain._reply_audio_started = True
        brain._reply_heard = heard
        s = make(short_final_context=brain._short_barge_in_context)
        await overlap(s)
        clock[0] += .3
        result = await s.process_frame(final(text))
        assert (result == ProcessFrameResult.STOP) is interrupts
        assert bool(s.trigger_user_turn_started.await_count) is interrupts
        # Acquiring the floor is not consent and does not run business logic.
        assert brain._recorder.turns == []
    finally:
        await brain.cleanup()
