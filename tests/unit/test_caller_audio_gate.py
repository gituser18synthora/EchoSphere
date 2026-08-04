"""Caller-audio noise gate: what reaches the VAD/STT and what never does.

The gate is the first of three speech/noise layers (gate → Silero VAD →
transcript gate). Its job is that background noise, distant voices and the
bot's own speaker bleed never reach the VAD at all — because a VAD that fires
is what makes the bot stop talking and start listening to nothing, and what
feeds realtime STT the hiss it hallucinates words out of.

Levels here are synthesised rather than recorded: the gate's contract is
purely about energy relative to the noise floor it measures on the call, so a
sine/noise generator exercises the real decision path.
"""

import math

import numpy as np
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.turn_detection import (
    NOISE_GATE_BOUNDS,
    NOISE_GATE_DEFAULTS,
    validate_noise_gate,
)
from voice_runtime.audio_gate import CallerAudioGate, frame_dbfs

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


def tone(dbfs: float, samples: int = FRAME_SAMPLES, freq: float = 300.0) -> bytes:
    """A sine at a given RMS level in dBFS (voice-band frequency)."""
    amplitude = (10.0 ** (dbfs / 20.0)) * 32767.0 * math.sqrt(2)
    t = np.arange(samples, dtype=np.float64)
    wave = np.sin(2 * math.pi * freq * t / SAMPLE_RATE) * amplitude
    return np.clip(wave, -32768, 32767).astype("<i2").tobytes()


def hiss(dbfs: float, samples: int = FRAME_SAMPLES, seed: int = 7) -> bytes:
    """Broadband noise at a given RMS level (fan/line hum stand-in)."""
    rng = np.random.default_rng(seed)
    amplitude = (10.0 ** (dbfs / 20.0)) * 32767.0
    wave = rng.normal(0.0, amplitude, samples)
    return np.clip(wave, -32768, 32767).astype("<i2").tobytes()


def audio_frame(payload: bytes) -> InputAudioRawFrame:
    return InputAudioRawFrame(
        audio=payload, sample_rate=SAMPLE_RATE, num_channels=1
    )


def make_gate(**overrides) -> CallerAudioGate:
    params = {
        key: value
        for key, value in NOISE_GATE_DEFAULTS["telephony"].items()
        if key != "enabled"
    }
    params.update(overrides)
    gate = CallerAudioGate(**params)
    gate._pushed = []

    async def _push(frame, direction=None):
        gate._pushed.append(frame)

    gate.push_frame = _push
    return gate


async def feed(gate, payload: bytes, *, frames: int = 1) -> None:
    for _ in range(frames):
        await gate.process_frame(audio_frame(payload), FrameDirection.DOWNSTREAM)


def audible(gate) -> list[InputAudioRawFrame]:
    """Frames that reached the VAD carrying actual (non-silent) audio."""
    return [
        frame
        for frame in gate._pushed
        if isinstance(frame, InputAudioRawFrame) and any(frame.audio)
    ]


def audible_ms(gate) -> float:
    return sum(
        len(frame.audio) / 2 / SAMPLE_RATE * 1000.0 for frame in audible(gate)
    )


class TestLevelMeasurement:
    def test_dbfs_of_silence_and_full_scale(self):
        assert frame_dbfs(b"\x00" * 320) < -100
        assert frame_dbfs(b"") < -100
        # A -20 dBFS tone must measure back as -20 dBFS.
        assert abs(frame_dbfs(tone(-20.0)) - (-20.0)) < 1.0

    def test_odd_trailing_byte_does_not_crash(self):
        assert frame_dbfs(tone(-25.0) + b"\x01") < 0


class TestNoiseRejection:
    async def test_steady_background_noise_never_reaches_the_vad(self):
        # 2 seconds of steady fan noise: the floor tracks it, so the threshold
        # tracks it too and the gate stays shut for the whole burst.
        gate = make_gate()
        await feed(gate, hiss(-45.0), frames=100)
        assert audible(gate) == []
        assert gate.stats()["opens"] == 0

    async def test_transient_clicks_are_rejected_by_sustain_requirement(self):
        # Keyboard taps / mic handling: loud but far shorter than a word.
        gate = make_gate()
        await feed(gate, hiss(-50.0), frames=40)          # establish the floor
        for _ in range(6):
            await feed(gate, tone(-12.0), frames=2)       # 40 ms burst
            await feed(gate, hiss(-50.0), frames=10)      # back to quiet
        assert gate.stats()["opens"] == 0

    async def test_distant_speech_below_the_margin_is_rejected(self):
        # Someone talking across the room: voice-like, but only a few dB above
        # this line's floor -- under the 8 dB telephony margin.
        gate = make_gate()
        await feed(gate, hiss(-45.0), frames=40)
        await feed(gate, tone(-40.0), frames=50)          # 1s of distant voice
        assert audible(gate) == []

    async def test_real_speech_opens_the_gate(self):
        gate = make_gate()
        await feed(gate, hiss(-50.0), frames=40)
        await feed(gate, tone(-22.0), frames=25)          # 500 ms of speech
        assert audible(gate), "genuine speech must reach the VAD"
        assert gate.stats()["opens"] == 1

    async def test_quiet_speech_on_a_quiet_line_still_opens(self):
        # The threshold is RELATIVE: -38 dBFS speech is quiet in absolute terms
        # but 12 dB above a -50 dBFS floor, so a bad handset is still heard.
        gate = make_gate(min_threshold_dbfs=-55.0)
        await feed(gate, hiss(-58.0), frames=40)
        await feed(gate, tone(-38.0), frames=25)
        assert audible(gate)


class TestAdaptiveFloor:
    async def test_floor_tracks_the_measured_background(self):
        gate = make_gate()
        await feed(gate, hiss(-40.0), frames=120)
        floor = gate.stats()["noise_floor_dbfs"]
        assert floor is not None and -46 < floor < -34

    async def test_threshold_rises_with_a_noisier_line(self):
        quiet, noisy = make_gate(), make_gate()
        await feed(quiet, hiss(-60.0), frames=120)
        await feed(noisy, hiss(-40.0), frames=120)
        assert noisy.threshold_dbfs() > quiet.threshold_dbfs()

    async def test_speech_does_not_walk_the_floor_up(self):
        # While the gate is open the floor must freeze, or a long utterance
        # would raise the bar until it shut the caller out mid-sentence.
        gate = make_gate()
        await feed(gate, hiss(-50.0), frames=40)
        floor_before = gate.stats()["noise_floor_dbfs"]
        await feed(gate, tone(-20.0), frames=150)         # 3s of speech
        assert gate.stats()["noise_floor_dbfs"] == floor_before


class TestPreRoll:
    async def test_onset_is_preserved_when_the_gate_opens(self):
        # The gate can only confirm speech after min_speech_ms, so without
        # pre-roll the first syllable of "haan" would already be gone.
        gate = make_gate(preroll_ms=160.0)
        await feed(gate, hiss(-50.0), frames=40)
        await feed(gate, tone(-22.0), frames=10)          # 200 ms word
        # min_speech_ms of 120 ms is consumed confirming speech; pre-roll must
        # hand that audio to the VAD anyway.
        assert audible_ms(gate) >= 180.0

    async def test_preroll_is_bounded(self):
        gate = make_gate(preroll_ms=100.0)
        await feed(gate, hiss(-50.0), frames=200)         # 4s of quiet
        await feed(gate, tone(-20.0), frames=10)
        # Only ~100 ms of retained history may be released, not the whole call.
        assert audible_ms(gate) < 400.0

    async def test_closed_gate_still_paces_the_stream(self):
        # Silence substitution, never frame dropping: downstream timers, the
        # STT keepalive and the recording all depend on continuous audio.
        gate = make_gate()
        await feed(gate, hiss(-50.0), frames=50)
        passed = [f for f in gate._pushed if isinstance(f, InputAudioRawFrame)]
        assert len(passed) == 50
        assert all(not any(f.audio) for f in passed)


class TestEchoAndBargeIn:
    async def test_bot_audio_echo_does_not_open_the_gate(self):
        # Speaker bleed while the bot talks: above the normal margin, but under
        # the raised echo bar.
        gate = make_gate()
        await feed(gate, hiss(-50.0), frames=40)
        await gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await feed(gate, tone(-38.0), frames=50)
        assert audible(gate) == []
        assert gate.stats()["echo_guard_ms"] > 0

    async def test_real_barge_in_still_opens_the_gate_while_bot_speaks(self):
        gate = make_gate()
        await feed(gate, hiss(-50.0), frames=40)
        await gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await feed(gate, tone(-18.0), frames=20)          # caller cuts in loudly
        assert audible(gate), "a real interruption must still get through"

    async def test_barge_in_needs_slightly_longer_sustain_than_normal(self):
        gate = make_gate(min_speech_ms=100.0, echo_min_speech_ms=300.0)
        await feed(gate, hiss(-50.0), frames=40)
        await gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await feed(gate, tone(-14.0), frames=7)           # 140 ms blip of echo
        assert audible(gate) == []
        await feed(gate, tone(-14.0), frames=10)          # sustained -> real
        assert audible(gate)

    async def test_echo_guard_outlives_the_bot_audio(self):
        gate = make_gate(echo_tail_ms=400.0)
        await feed(gate, hiss(-50.0), frames=40)
        await gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        # Room echo arriving just after the bot stopped is still suppressed.
        await feed(gate, tone(-38.0), frames=10)
        assert audible(gate) == []

    async def test_bot_speaking_frames_are_forwarded_untouched(self):
        gate = make_gate()
        started = BotStartedSpeakingFrame()
        await gate.process_frame(started, FrameDirection.UPSTREAM)
        assert started in gate._pushed


class TestSpeechEvidence:
    async def test_snapshot_reports_snr_above_the_measured_floor(self):
        gate = make_gate()
        await feed(gate, hiss(-50.0), frames=40)
        await feed(gate, tone(-20.0), frames=25)
        snapshot = gate.speech_snapshot()
        assert snapshot is not None
        assert snapshot["snr_db"] > 15
        assert snapshot["during_bot_audio"] is False

    async def test_snapshot_flags_audio_captured_during_bot_speech(self):
        gate = make_gate()
        await feed(gate, hiss(-50.0), frames=40)
        await gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await feed(gate, tone(-14.0), frames=25)
        assert gate.speech_snapshot()["during_bot_audio"] is True

    async def test_no_snapshot_before_any_speech(self):
        gate = make_gate()
        await feed(gate, hiss(-50.0), frames=20)
        assert gate.speech_snapshot() is None

    async def test_stats_expose_no_audio_or_text(self):
        gate = make_gate()
        await feed(gate, hiss(-50.0), frames=30)
        stats = gate.stats()
        assert set(stats) == {
            "opens", "suppressed_ms", "passed_ms", "echo_guard_ms",
            "noise_floor_dbfs",
        }
        assert all(isinstance(v, (int, float, type(None))) for v in stats.values())


class TestConfiguration:
    def test_defaults_are_within_their_own_bounds(self):
        for transport, defaults in NOISE_GATE_DEFAULTS.items():
            assert set(defaults) == set(NOISE_GATE_BOUNDS), transport
            for key, value in defaults.items():
                low, high = NOISE_GATE_BOUNDS[key]
                assert low <= value <= high, f"{transport}.{key}"

    def test_telephony_margin_is_more_permissive_than_browser(self):
        # PSTN audio is quieter and band-limited, so a wide margin would drop
        # genuine low-energy words.
        assert (
            NOISE_GATE_DEFAULTS["telephony"]["noise_margin_db"]
            < NOISE_GATE_DEFAULTS["browser"]["noise_margin_db"]
        )

    def test_validation_rejects_unknown_and_out_of_range(self):
        assert validate_noise_gate(None) == []
        assert validate_noise_gate({"noise_margin_db": 8}) == []
        assert validate_noise_gate("loud")
        assert validate_noise_gate({"nope": 1})
        assert validate_noise_gate({"noise_margin_db": 90})
        assert validate_noise_gate({"min_speech_ms": "long"})

    def test_pipeline_resolution_clamps_and_survives_junk(self):
        from shared.bot_config import ResolvedBotConfig
        from voice_runtime.pipeline import resolve_noise_gate

        config = ResolvedBotConfig(
            tenant_id="t", bot_id="b", bot_name="n", version="1", published=True,
            stt={"provider": "sarvam", "settings": {"noise_gate": {
                "noise_margin_db": 999,      # would deafen the gate
                "min_speech_ms": "forever",  # not a number
            }}},
        )
        resolved = resolve_noise_gate(config, "telephony")
        assert resolved["noise_margin_db"] == NOISE_GATE_BOUNDS["noise_margin_db"][1]
        assert resolved["min_speech_ms"] == (
            NOISE_GATE_DEFAULTS["telephony"]["min_speech_ms"]
        )
