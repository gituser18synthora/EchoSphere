"""PlaybackDuck — provisional pause of bot playback (voice_runtime.playback_duck)."""

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from voice_runtime.playback_duck import PlaybackDuck


class _Recorder:
    def __init__(self):
        self.events = []

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def kinds(self):
        return [k for k, _ in self.events]


def make(max_hold_seconds=30.0):
    rec = _Recorder()
    duck = PlaybackDuck(recorder=rec, max_hold_seconds=max_hold_seconds)
    duck._sample_rate = 8000
    out = []

    async def _push(frame, direction=FrameDirection.DOWNSTREAM):
        out.append((type(frame).__name__, direction, getattr(frame, "audio", None)))

    duck.push_frame = _push
    return duck, out, rec


def audio(tag: bytes, ms: int = 20) -> OutputAudioRawFrame:
    return OutputAudioRawFrame(audio=tag * (8000 * 2 * ms // 1000 // len(tag)), sample_rate=8000, num_channels=1)


DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


class TestPassThrough:
    async def test_not_paused_frames_pass_unchanged(self):
        duck, out, _ = make()
        await duck.process_frame(audio(b"\x01\x00"), DOWN)
        await duck.process_frame(BotStoppedSpeakingFrame(), UP)
        assert [o[0] for o in out] == ["OutputAudioRawFrame", "BotStoppedSpeakingFrame"]
        assert not duck.paused


class TestPauseResume:
    async def test_pause_holds_data_frames_then_resume_releases_in_order(self):
        duck, out, rec = make()
        await duck.pause()
        await duck.process_frame(audio(b"\x01\x00"), DOWN)
        await duck.process_frame(TTSStoppedFrame(), DOWN)
        await duck.process_frame(audio(b"\x02\x00"), DOWN)
        assert out == [] and duck.held_ms == 40.0
        await duck.resume()
        assert [o[0] for o in out] == ["OutputAudioRawFrame", "TTSStoppedFrame", "OutputAudioRawFrame"]
        assert out[0][2][:2] == b"\x01\x00" and out[2][2][:2] == b"\x02\x00"
        assert duck.held_ms == 0.0 and not duck.paused
        assert rec.kinds() == ["playback_paused", "playback_resumed"]

    async def test_transport_bot_stop_during_pause_is_swallowed_and_resume_start_too(self):
        duck, out, rec = make()
        await duck.pause()
        await duck.process_frame(audio(b"\x01\x00"), DOWN)
        # The transport ran dry and declares the bot stopped: not true.
        await duck.process_frame(BotStoppedSpeakingFrame(), UP)
        assert out == []
        assert "playback_bot_stop_suppressed" in rec.kinds()
        await duck.resume()
        # Playback resumes: the transport announces a "new" start — swallowed,
        # upstream saw one continuous utterance.
        await duck.process_frame(BotStartedSpeakingFrame(), UP)
        assert [o[0] for o in out] == ["OutputAudioRawFrame"]
        # The genuine end of the reply later passes normally.
        await duck.process_frame(BotStoppedSpeakingFrame(), UP)
        assert out[-1][0] == "BotStoppedSpeakingFrame"

    async def test_short_pause_without_transport_stop_does_not_swallow_a_real_start(self):
        duck, out, _ = make()
        await duck.pause()
        await duck.resume()
        await duck.process_frame(BotStartedSpeakingFrame(), UP)
        assert [o[0] for o in out] == ["BotStartedSpeakingFrame"]

    async def test_pause_and_resume_are_idempotent(self):
        duck, out, rec = make()
        await duck.pause()
        await duck.pause()
        await duck.resume()
        await duck.resume()
        assert rec.kinds() == ["playback_paused", "playback_resumed"]

    async def test_rapid_pause_resume_pause_keeps_order(self):
        duck, out, _ = make()
        await duck.pause()
        await duck.process_frame(audio(b"\x01\x00"), DOWN)
        await duck.resume()
        await duck.pause()
        await duck.process_frame(audio(b"\x02\x00"), DOWN)
        await duck.resume()
        assert [o[2][:2] for o in out] == [b"\x01\x00", b"\x02\x00"]


class TestCommitAndTeardown:
    async def test_interruption_discards_held_audio_and_reemits_swallowed_stop(self):
        duck, out, rec = make()
        await duck.pause()
        await duck.process_frame(audio(b"\x01\x00"), DOWN)
        await duck.process_frame(BotStoppedSpeakingFrame(), UP)  # swallowed
        await duck.process_frame(InterruptionFrame(), DOWN)
        names = [(o[0], o[1]) for o in out]
        assert ("BotStoppedSpeakingFrame", UP) in names   # re-emitted for the brain
        assert ("InterruptionFrame", DOWN) in names
        assert not any(o[0] == "OutputAudioRawFrame" for o in out)  # held audio never plays
        assert not duck.paused and duck.held_ms == 0.0
        assert ("playback_discarded", {"reason": "interruption", "held_ms": 20}) in rec.events
        # A brand-new reply after the commit passes untouched.
        await duck.process_frame(audio(b"\x03\x00"), DOWN)
        await duck.process_frame(BotStartedSpeakingFrame(), UP)
        assert out[-2][0] == "OutputAudioRawFrame" and out[-1][0] == "BotStartedSpeakingFrame"

    async def test_end_frame_clears_everything(self):
        duck, out, _ = make()
        await duck.pause()
        await duck.process_frame(audio(b"\x01\x00"), DOWN)
        await duck.process_frame(EndFrame(), DOWN)
        assert [o[0] for o in out] == ["EndFrame"]
        assert not duck.paused and duck.held_ms == 0.0

    async def test_held_audio_is_capped(self):
        duck, out, _ = make(max_hold_seconds=1.0)
        await duck.pause()
        for _ in range(100):  # 2 s of audio into a 1 s cap
            await duck.process_frame(audio(b"\x01\x00"), DOWN)
        assert duck.held_ms <= 1000.0
        assert duck.stats()["cap_drops"] >= 50
