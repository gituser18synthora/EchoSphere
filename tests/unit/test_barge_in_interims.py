"""Interim transcripts and the barge-in word gate (live cut-off, 2026-10-22).

With SARVAM_REALTIME_STT=true the realtime socket keeps an utterance open
while the bot speaks and its cumulative partials pick up the bot's own PSTN
echo; counted as plain words they confirmed a "barge-in" 2-6 s into every long
reply (events: barge_in_confirmed "transcript (2 words >= 2)" with
sustained_s=null, then workflow_reply_interrupted / tts_generation_interrupted,
no caller turn after). An interim now confirms only when the VAD shows
sustained caller speech AND its leading words are stable across two updates;
finals keep the pre-Phase-2 behaviour.
"""
import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
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


def make(min_words=2, vad_fallback_secs=1.0):
    fired = []
    strategy = WordConfirmedBargeInStrategy(
        min_words=min_words, vad_fallback_secs=vad_fallback_secs,
        on_confirmed=lambda why, sustained: fired.append((why, sustained)),
    )

    async def _fire():
        fired.append(("turn",))

    strategy.trigger_user_turn_started = _fire
    return strategy, fired


def interim(text):
    return InterimTranscriptionFrame(text=text, user_id="u", timestamp="t")


def final(text):
    return TranscriptionFrame(text=text, user_id="u", timestamp="t")


class TestLiveCutOffReproduction:
    async def test_echo_partials_without_vad_never_confirm(self, clock):
        # The live pattern: the bot's own greeting echoed back as a growing
        # partial, no VAD start (sustained_s was null on every live event).
        s, fired = make()
        await s.process_frame(BotStartedSpeakingFrame())
        for text in ("namaste", "namaste main", "namaste main kavya", "namaste main kavya bol rahi hoon",
                     "namaste main kavya bol rahi hoon kya meri baat"):
            clock.t += 0.5
            result = await s.process_frame(interim(text))
            assert result == ProcessFrameResult.CONTINUE, text
        assert fired == []

    async def test_partials_with_a_short_vad_blip_do_not_confirm(self, clock):
        s, fired = make()
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.1                                   # under _INTERIM_VAD_MIN_S
        await s.process_frame(interim("kya meri baat"))
        await s.process_frame(interim("kya meri baat rohan"))
        assert fired == []
        await s.process_frame(VADUserStoppedSpeakingFrame())   # blip over: episode resets
        clock.t += 0.5
        await s.process_frame(interim("kya meri baat rohan ji"))
        await s.process_frame(interim("kya meri baat rohan ji se"))
        assert fired == []                               # no VAD speech right now

    async def test_changing_words_with_vad_do_not_confirm_until_stable(self, clock):
        s, fired = make()
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.5
        assert await s.process_frame(interim("ek minute")) == ProcessFrameResult.CONTINUE
        assert await s.process_frame(interim("ek second ruko")) == ProcessFrameResult.CONTINUE  # head changed
        assert fired == []
        result = await s.process_frame(interim("ek second ruko please"))                       # head stable
        assert result == ProcessFrameResult.STOP
        assert fired[0][0].startswith("interim transcript (4 words >= 2, stable over 2 updates, VAD 0.5s)")
        assert fired[-1] == ("turn",)

    async def test_genuine_interruption_confirms_from_stable_interims(self, clock):
        s, fired = make()
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.3
        await s.process_frame(interim("ek minute ruko"))
        clock.t += 0.2
        result = await s.process_frame(interim("ek minute ruko bhai"))
        assert result == ProcessFrameResult.STOP and fired[-1] == ("turn",)

    async def test_finals_keep_the_pre_phase_2_behaviour(self, clock):
        s, fired = make()
        await s.process_frame(BotStartedSpeakingFrame())
        result = await s.process_frame(final("ek minute ruko"))   # no VAD, no history needed
        assert result == ProcessFrameResult.STOP
        assert fired[0][0] == "transcript (3 words >= 2)"

    async def test_bot_stop_and_new_episode_reset_interim_history(self, clock):
        s, fired = make()
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.5
        await s.process_frame(interim("haan ji bolo"))
        await s.process_frame(BotStoppedSpeakingFrame())        # confirms via "bot stopped during gated speech"
        assert fired and fired[0][0] == "bot stopped during gated speech"
        fired.clear()
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 0.5
        assert await s.process_frame(interim("haan ji bolo")) == ProcessFrameResult.CONTINUE  # history was cleared
        assert fired == []

    async def test_sustained_vad_fallback_still_works_without_any_transcript(self, clock):
        s, fired = make(vad_fallback_secs=1.0)
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(VADUserStartedSpeakingFrame())
        clock.t += 1.05
        result = await s.process_frame(interim("hmm"))        # any frame past the window
        assert result == ProcessFrameResult.STOP
        assert fired[0][0].startswith("sustained VAD speech")
