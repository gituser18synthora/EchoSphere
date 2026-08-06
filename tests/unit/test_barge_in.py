"""Word-confirmed barge-in strategy.

While the bot is quiet the strategy behaves exactly like the stock VAD start
strategy (voice activity opens the turn — latency unchanged). While the bot is
SPEAKING, voice activity alone must not open a turn — an open turn interrupts
the reply, and ambient speech near the microphone is real voice activity — so
the interruption requires a transcript of at least ``min_words`` words.
"""

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult

from voice_runtime.barge_in import WordConfirmedBargeInStrategy


def make_strategy(min_words: int = 2) -> tuple[WordConfirmedBargeInStrategy, list]:
    strategy = WordConfirmedBargeInStrategy(min_words=min_words)
    fired: list[bool] = []

    async def _fire():
        fired.append(True)

    strategy.trigger_user_turn_started = _fire
    return strategy, fired


def final(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(text=text, user_id="u", timestamp="t")


class TestBotQuiet:
    async def test_vad_start_opens_the_turn(self):
        strategy, fired = make_strategy()
        result = await strategy.process_frame(VADUserStartedSpeakingFrame())
        assert fired and result == ProcessFrameResult.STOP

    async def test_transcripts_alone_do_not_open_a_turn(self):
        # Orphan finals while idle are the brain's job (finalize debounce);
        # a transcript-opened turn would wait out the stop strategy's fallback
        # timer instead and answer LATER.
        strategy, fired = make_strategy()
        result = await strategy.process_frame(final("हाँ बोल रहा हूँ"))
        assert not fired and result == ProcessFrameResult.CONTINUE

    async def test_vad_start_works_again_after_bot_stops(self):
        strategy, fired = make_strategy()
        await strategy.process_frame(BotStartedSpeakingFrame())
        await strategy.process_frame(BotStoppedSpeakingFrame())
        result = await strategy.process_frame(VADUserStartedSpeakingFrame())
        assert fired and result == ProcessFrameResult.STOP


class TestBotSpeaking:
    async def test_vad_start_does_not_interrupt(self):
        strategy, fired = make_strategy()
        await strategy.process_frame(BotStartedSpeakingFrame())
        result = await strategy.process_frame(VADUserStartedSpeakingFrame())
        assert not fired and result == ProcessFrameResult.CONTINUE

    async def test_short_transcript_does_not_interrupt(self):
        strategy, fired = make_strategy(min_words=2)
        await strategy.process_frame(BotStartedSpeakingFrame())
        result = await strategy.process_frame(final("हाँ"))
        assert not fired and result == ProcessFrameResult.CONTINUE

    async def test_min_words_transcript_interrupts(self):
        strategy, fired = make_strategy(min_words=2)
        await strategy.process_frame(BotStartedSpeakingFrame())
        result = await strategy.process_frame(final("एक मिनट रुकिए"))
        assert fired and result == ProcessFrameResult.STOP

    async def test_interim_transcript_counts(self):
        strategy, fired = make_strategy(min_words=2)
        await strategy.process_frame(BotStartedSpeakingFrame())
        frame = InterimTranscriptionFrame(text="please stop", user_id="u", timestamp="t")
        result = await strategy.process_frame(frame)
        assert fired and result == ProcessFrameResult.STOP

    async def test_empty_transcript_is_ignored(self):
        strategy, fired = make_strategy(min_words=1)
        await strategy.process_frame(BotStartedSpeakingFrame())
        result = await strategy.process_frame(final("   "))
        assert not fired and result == ProcessFrameResult.CONTINUE


class TestReset:
    async def test_bot_speaking_state_survives_turn_start_reset(self):
        # The controller resets start strategies on every turn start; a
        # barge-in turn starts precisely while the bot is still speaking, so
        # wiping the flag would let plain VAD interrupt the NEXT reply.
        strategy, fired = make_strategy()
        await strategy.process_frame(BotStartedSpeakingFrame())
        await strategy.reset()
        result = await strategy.process_frame(VADUserStartedSpeakingFrame())
        assert not fired and result == ProcessFrameResult.CONTINUE

    async def test_min_words_floor_is_one(self):
        strategy, fired = make_strategy(min_words=0)
        await strategy.process_frame(BotStartedSpeakingFrame())
        result = await strategy.process_frame(final("रुकिए"))
        assert fired and result == ProcessFrameResult.STOP
