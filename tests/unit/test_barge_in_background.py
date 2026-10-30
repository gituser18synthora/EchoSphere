"""Background-speech guard inside the word-confirmed barge-in strategy.

While the bot is speaking, neither sustained VAD speech nor a multi-word
transcript may cancel the reply when the live gated speech sits far below the
caller's own established level (another person near the handset). Caller-level
speech, and any speech before a baseline exists, interrupts exactly as before.
"""

import asyncio

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult

from voice_runtime.barge_in import WordConfirmedBargeInStrategy
from voice_runtime.caller_level import LevelVerdict


def verdict(label, delta=-20.0):
    return LevelVerdict(
        label=label, level_dbfs=-50.0, baseline_dbfs=-30.0, delta_db=delta,
        margin_db=10.0, baseline_segments=2,
    )


def make(classifier, *, enforce=True, fallback=0.05, min_words=2):
    suppressed = []
    fired = []
    strategy = WordConfirmedBargeInStrategy(
        min_words=min_words, vad_fallback_secs=fallback,
        speech_classifier=classifier, enforce_background=enforce,
        on_suppressed=lambda *args: suppressed.append(args),
    )

    async def _fire():
        fired.append(True)

    strategy.trigger_user_turn_started = _fire
    return strategy, fired, suppressed


def final(text):
    return TranscriptionFrame(text=text, user_id="u", timestamp="t")


class TestSustainedVad:
    async def test_background_speech_does_not_interrupt(self):
        strategy, fired, suppressed = make(lambda: verdict("background_suspect"))
        await strategy.process_frame(BotStartedSpeakingFrame())
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.06)
        result = await strategy.process_frame(VADUserStartedSpeakingFrame())
        assert not fired and result == ProcessFrameResult.CONTINUE
        assert suppressed and suppressed[0][0] == "sustained_vad"
        assert suppressed[0][3] is True  # enforced

    async def test_caller_level_speech_still_interrupts(self):
        strategy, fired, suppressed = make(lambda: verdict("caller", delta=-2.0))
        await strategy.process_frame(BotStartedSpeakingFrame())
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.06)
        result = await strategy.process_frame(VADUserStartedSpeakingFrame())
        assert fired and result == ProcessFrameResult.STOP and not suppressed

    async def test_no_verdict_yet_behaves_as_before(self):
        # Baseline not established / live segment too short → None.
        strategy, fired, suppressed = make(lambda: None)
        await strategy.process_frame(BotStartedSpeakingFrame())
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.06)
        result = await strategy.process_frame(VADUserStartedSpeakingFrame())
        assert fired and result == ProcessFrameResult.STOP and not suppressed

    async def test_shadow_mode_records_but_interrupts(self):
        strategy, fired, suppressed = make(
            lambda: verdict("background_suspect"), enforce=False
        )
        await strategy.process_frame(BotStartedSpeakingFrame())
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.06)
        result = await strategy.process_frame(VADUserStartedSpeakingFrame())
        assert fired and result == ProcessFrameResult.STOP
        assert suppressed and suppressed[0][3] is False

    async def test_suppression_is_rechecked_at_a_bounded_cadence(self):
        calls = []

        def classifier():
            calls.append(1)
            return verdict("background_suspect")

        strategy, fired, suppressed = make(classifier)
        await strategy.process_frame(BotStartedSpeakingFrame())
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.06)
        for _ in range(10):  # ~20 ms audio frames keep arriving
            await strategy.process_frame(VADUserStartedSpeakingFrame())
        assert not fired
        assert len(calls) == 1  # one verdict per 0.5 s, not one per frame

    async def test_classifier_failure_never_blocks_a_turn(self):
        def classifier():
            raise RuntimeError("gate gone")

        strategy, fired, _ = make(classifier)
        await strategy.process_frame(BotStartedSpeakingFrame())
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.06)
        result = await strategy.process_frame(VADUserStartedSpeakingFrame())
        assert fired and result == ProcessFrameResult.STOP


class TestTranscriptArbiter:
    async def test_multiword_background_transcript_does_not_interrupt(self):
        strategy, fired, suppressed = make(lambda: verdict("background_suspect"), fallback=0)
        await strategy.process_frame(BotStartedSpeakingFrame())
        result = await strategy.process_frame(final("यहां पर ग्रीन कोऑर्डिनेट कर रहे हैं"))
        assert not fired and result == ProcessFrameResult.CONTINUE
        assert suppressed[0][0] == "transcript"

    async def test_multiword_caller_transcript_interrupts(self):
        strategy, fired, _ = make(lambda: verdict("caller", delta=0.5), fallback=0)
        await strategy.process_frame(BotStartedSpeakingFrame())
        result = await strategy.process_frame(final("एक मिनट रुकिए"))
        assert fired and result == ProcessFrameResult.STOP


class TestBotStops:
    async def test_bot_stop_during_gated_speech_still_opens_the_turn(self):
        # Nothing audible is left to protect; the brain judges the transcript.
        strategy, fired, _ = make(lambda: verdict("background_suspect"), fallback=0)
        await strategy.process_frame(BotStartedSpeakingFrame())
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        result = await strategy.process_frame(BotStoppedSpeakingFrame())
        assert fired and result == ProcessFrameResult.STOP
