"""Hybrid / provisional barge-in (voice_runtime.barge_in, provisional_duck=True).

VAD start while the bot speaks → provisional (reply paused) → commit after
``commit_secs`` of sustained speech or a ``min_words`` transcript → otherwise
resume when the VAD stops, without opening a turn.
"""

import asyncio

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult

import voice_runtime.barge_in as barge_in_module
from voice_runtime.barge_in import WordConfirmedBargeInStrategy


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(barge_in_module.time, "monotonic", c)
    return c


def make(commit_secs=1.0, min_words=3, async_hook=False):
    duck_calls, changes, fired = [], [], []

    def _hook(active):
        duck_calls.append(active)

    async def _ahook(active):
        duck_calls.append(active)

    strategy = WordConfirmedBargeInStrategy(
        min_words=min_words, provisional_duck=True, commit_secs=commit_secs,
        on_provisional=_ahook if async_hook else _hook,
        on_provisional_change=lambda state, sustained: changes.append((state, sustained)),
        on_confirmed=lambda why, sustained: fired.append((why, sustained)),
    )

    async def _fire():
        fired.append(("turn",))

    strategy.trigger_user_turn_started = _fire
    return strategy, duck_calls, changes, fired


def final(text):
    return TranscriptionFrame(text=text, user_id="u", timestamp="t")


class TestProvisional:
    async def test_vad_start_while_bot_speaks_pauses_without_a_turn(self, clock):
        s, duck, changes, fired = make()
        await s.process_frame(BotStartedSpeakingFrame())
        result = await s.process_frame(VADUserStartedSpeakingFrame())
        assert result == ProcessFrameResult.CONTINUE and fired == []
        assert duck == [True] and s.provisional
        assert changes[0][0] == "provisional"

    async def test_vad_stop_before_commit_resumes_and_opens_no_turn(self, clock):
        s, duck, changes, fired = make()
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.6
        await s.process_frame(Frame())
        await s.process_frame(VADUserStoppedSpeakingFrame())
        assert duck == [True, False] and not s.provisional and fired == []
        assert changes[-1][0] == "resumed" and 0.59 < changes[-1][1] < 0.61

    async def test_sustained_speech_commits_at_the_commit_window(self, clock):
        s, duck, changes, fired = make(commit_secs=1.0)
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.98
        assert await s.process_frame(Frame()) == ProcessFrameResult.CONTINUE
        clock.t += 0.03
        assert await s.process_frame(Frame()) == ProcessFrameResult.STOP
        assert ("turn",) in fired and fired[0][0].startswith("sustained VAD speech >= 1.0")
        assert duck == [True]  # no resume: the interruption frames clear the duck
        assert not s.provisional

    async def test_three_word_transcript_commits_two_words_do_not(self, clock):
        s, duck, changes, fired = make(min_words=3)
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.3
        assert await s.process_frame(final("हाँ जी")) == ProcessFrameResult.CONTINUE
        assert fired == []
        assert await s.process_frame(final("एक मिनट रुकिए")) == ProcessFrameResult.STOP
        assert fired[0][0].startswith("transcript (3 words")
        assert duck == [True]

    async def test_transcript_can_commit_before_the_vad_timer(self, clock):
        s, duck, changes, fired = make(commit_secs=1.0)
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.4
        assert await s.process_frame(final("please stop talking now")) == ProcessFrameResult.STOP
        assert 0.39 < fired[0][1] < 0.41

    async def test_bot_stops_during_provisional_speech_opens_the_turn(self, clock):
        s, duck, changes, fired = make()
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.3
        result = await s.process_frame(BotStoppedSpeakingFrame())
        assert result == ProcessFrameResult.STOP and fired[0][0] == "bot stopped during gated speech"
        assert not s.provisional

    async def test_rapid_start_stop_start_gives_independent_episodes(self, clock):
        s, duck, changes, fired = make()
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.2
        await s.process_frame(VADUserStoppedSpeakingFrame())
        clock.t += 0.1
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.5
        await s.process_frame(Frame())
        assert fired == []  # the clock restarted at the second start
        clock.t += 0.6
        assert await s.process_frame(Frame()) == ProcessFrameResult.STOP
        assert duck == [True, False, True]

    async def test_async_duck_hook_is_awaited(self, clock):
        s, duck, changes, fired = make(async_hook=True)
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        await s.process_frame(VADUserStoppedSpeakingFrame())
        assert duck == [True, False]

    async def test_bot_quiet_vad_start_opens_turn_immediately_without_pausing(self, clock):
        s, duck, changes, fired = make()
        assert await s.process_frame(VADUserStartedSpeakingFrame()) == ProcessFrameResult.STOP
        assert duck == [] and fired == [("turn",)]

    async def test_hook_failure_never_blocks_the_turn(self, clock):
        def boom(active):
            raise RuntimeError("duck gone")

        s = WordConfirmedBargeInStrategy(min_words=3, provisional_duck=True, commit_secs=0.5, on_provisional=boom)
        fired = []

        async def _fire():
            fired.append(True)

        s.trigger_user_turn_started = _fire
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.6
        assert await s.process_frame(Frame()) == ProcessFrameResult.STOP and fired


class TestLegacyModeUnchanged:
    async def test_without_duck_the_fallback_window_still_applies(self, clock):
        s = WordConfirmedBargeInStrategy(min_words=2, vad_fallback_secs=0.65, provisional_duck=False)
        fired = []

        async def _fire():
            fired.append(True)

        s.trigger_user_turn_started = _fire
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.6
        assert await s.process_frame(Frame()) == ProcessFrameResult.CONTINUE
        clock.t += 0.1
        assert await s.process_frame(Frame()) == ProcessFrameResult.STOP
        assert not s.provisional
