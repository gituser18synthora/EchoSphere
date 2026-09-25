"""Telephony self-echo evidence (voice_runtime.echo_reference) and its use in
the caller audio gate.

The bot's own reply leaking back on the PSTN leg is a scaled, delayed,
band-limited copy of the audio the transport just sent. These tests build
that copy synthetically (independent band-limited noise bursts with a
syllabic envelope stand in for speech: two different seeds are as
uncorrelated as two different sentences) and check that the decision is
about SIMILARITY to the sent audio, not about level: echo is rejected at
every leak level, a different voice is accepted at any level, and a mixture
is only rejected when the echo dominates it.
"""

import numpy as np
import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from voice_runtime.audio_gate import CallerAudioGate
from voice_runtime.echo_reference import (
    MODE_ENFORCE,
    MODE_OFF,
    MODE_SHADOW,
    SOURCE_PLAUSIBILITY_MARGIN_DB,
    EchoReference,
    EchoReferenceTap,
    mode_from_setting,
)

SR = 8000
FRAME = 160  # 20 ms at 8 kHz — the FreeSWITCH fork's uplink packet


def speech_like(seconds: float, seed: int, *, sr: int = SR) -> np.ndarray:
    """Band-limited noise with a ~4 Hz syllabic envelope (float32, peak-normalised)."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    x = rng.standard_normal(n)
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    spectrum[(freqs < 300) | (freqs > 3400)] = 0
    x = np.fft.irfft(spectrum, n)
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 4.0 * np.arange(n) / sr + rng.uniform(0, 6.28))
    x = x * env
    return (x / (np.max(np.abs(x)) * 1.2)).astype(np.float32)


def dbfs(x: np.ndarray) -> float:
    return 20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)


def at_level(x: np.ndarray, target_dbfs: float) -> np.ndarray:
    return (x * 10 ** ((target_dbfs - dbfs(x)) / 20)).astype(np.float32)


def pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def line_noise(n: int, seed: int = 99, level_dbfs: float = -65.0) -> np.ndarray:
    return at_level(np.random.default_rng(seed).standard_normal(n).astype(np.float32), level_dbfs)


def pstn_colour(x: np.ndarray) -> np.ndarray:
    """A hybrid/handset echo path: band-pass, two reflections, mu-law companding."""
    n = len(x)
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    spectrum[(freqs < 300) | (freqs > 3400)] = 0
    y = np.fft.irfft(spectrum, n)
    y = y + 0.5 * np.concatenate([np.zeros(96), y[:-96]]) + 0.25 * np.concatenate([np.zeros(240), y[:-240]])
    mu = 255.0
    comp = np.sign(y) * np.log1p(mu * np.abs(y)) / np.log1p(mu)
    comp = np.round(comp * 127) / 127
    return (np.sign(comp) * (np.expm1(np.abs(comp) * np.log1p(mu)) / mu)).astype(np.float32)


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def send_bot_audio(ref: EchoReference, clock: Clock, bot: np.ndarray) -> None:
    """Play ``bot`` through the reference on a real-time 20 ms clock."""
    for start in range(0, len(bot) - FRAME + 1, FRAME):
        clock.t += FRAME / SR
        ref.add_output(pcm(bot[start:start + FRAME]), SR)


def echo_window(bot: np.ndarray, clock_start: float, clock: Clock, lag_s: float, window_s: float = 0.12) -> np.ndarray:
    """The bot audio that arrives back as echo in the window ending now."""
    end = int(round((clock.t - clock_start - lag_s) * SR))
    start = end - int(window_s * SR)
    return bot[start:end]


class TestEchoLikeness:
    @pytest.mark.parametrize("leak_db", [-40, -35, -30, -25, -20, -15])
    def test_delayed_scaled_copy_is_echo_at_every_leak_level(self, leak_db):
        clock = Clock()
        start = clock.t
        ref = EchoReference(sample_rate=SR, clock=clock)
        bot = at_level(speech_like(1.0, seed=1), -20.0)
        send_bot_audio(ref, clock, bot)
        echo = echo_window(bot, start, clock, lag_s=0.18) * 10 ** (leak_db / 20)
        inbound = echo + line_noise(len(echo))
        peak, lag_ms = ref.match(inbound)
        # −40 dB of a −20 dBFS reply is −60 dBFS: 5 dB above the −65 dBFS line
        # noise, so the correlation is noise-limited there (and the gate floor
        # would not admit it anyway).
        assert peak > (0.6 if leak_db <= -40 else 0.85), (leak_db, peak)
        assert abs(lag_ms - 180) < 5, lag_ms
        assert ref.judge(inbound, dbfs(inbound)) is True

    def test_pstn_coloured_echo_is_still_echo(self):
        clock = Clock()
        start = clock.t
        ref = EchoReference(sample_rate=SR, clock=clock)
        bot = at_level(speech_like(1.0, seed=1), -20.0)
        send_bot_audio(ref, clock, bot)
        coloured = pstn_colour(bot * 10 ** (-25 / 20))
        end = int(round((clock.t - start - 0.30) * SR))
        inbound = coloured[end - int(0.12 * SR):end] + line_noise(int(0.12 * SR))
        peak, lag_ms = ref.match(inbound)
        assert peak > 0.6, peak
        assert 295 <= lag_ms <= 320, lag_ms

    @pytest.mark.parametrize("caller_dbfs", [-45.0, -30.0, -12.0])
    def test_a_different_voice_is_not_echo_at_any_level(self, caller_dbfs):
        clock = Clock()
        ref = EchoReference(sample_rate=SR, clock=clock)
        send_bot_audio(ref, clock, at_level(speech_like(1.0, seed=1), -20.0))
        caller = at_level(speech_like(0.12, seed=2), caller_dbfs)
        peak, _ = ref.match(caller)
        assert peak < 0.35, peak
        assert ref.judge(caller, caller_dbfs) is False

    def test_mixture_is_echo_only_when_the_echo_dominates(self):
        clock = Clock()
        start = clock.t
        ref = EchoReference(sample_rate=SR, clock=clock)
        bot = at_level(speech_like(1.0, seed=1), -20.0)
        send_bot_audio(ref, clock, bot)
        echo = at_level(echo_window(bot, start, clock, lag_s=0.2), -40.0)
        caller = speech_like(0.12, seed=3)
        # Caller 8 dB louder than the echo: the caller's speech, accepted.
        louder = echo + at_level(caller, -32.0)
        peak_louder, _ = ref.match(louder)
        assert peak_louder < 0.45, peak_louder
        assert ref.judge(louder, dbfs(louder)) is False
        # Caller 6 dB quieter than the echo: an echo-dominated window, rejected.
        quieter = echo + at_level(caller, -46.0)
        peak_quieter, _ = ref.match(quieter)
        assert peak_quieter > 0.7, peak_quieter
        assert ref.judge(quieter, dbfs(quieter)) is True

    def test_weak_peak_counts_only_at_the_established_echo_lag(self):
        clock = Clock()
        start = clock.t
        ref = EchoReference(sample_rate=SR, clock=clock)
        bot = at_level(speech_like(1.0, seed=1), -20.0)
        send_bot_audio(ref, clock, bot)
        caller = speech_like(0.12, seed=3)
        echo = at_level(echo_window(bot, start, clock, lag_s=0.2), -40.0)
        # Caller 3 dB louder than the echo: ρ ≈ 0.58, in the weak band. With
        # no echo path known yet it is NOT evidence (the caller wins).
        mixed = echo + at_level(caller, -37.0)
        peak, lag = ref.match(mixed)
        assert 0.5 <= peak < 0.7, peak
        assert ref.judge(mixed, dbfs(mixed)) is False
        assert ref.stats["weak_inconsistent"] == 1
        # A strong match establishes the path delay (≈ 200 ms) …
        assert ref.judge(echo + line_noise(len(echo)), -40.0) is True
        assert abs(ref.stats["lag_est_ms"] - 200) < 30
        # … after which the same mixture is rejected as echo-dominated.
        assert ref.judge(mixed, dbfs(mixed)) is True
        assert ref.stats["weak_rejected"] == 1

    def test_lag_search_does_not_reach_past_the_echo_delay_range(self):
        # Once the bot has been quiet for a while, only reference audio sent
        # within max_lag + window before NOW may be compared: older audio
        # cannot be arriving as echo, and searching it would only add
        # spurious peaks at impossible delays.
        clock = Clock()
        start = clock.t
        ref = EchoReference(sample_rate=SR, clock=clock)
        bot = at_level(speech_like(1.0, seed=1), -20.0)
        send_bot_audio(ref, clock, bot)
        clock.t += 0.5
        echo = at_level(echo_window(bot, start, clock, lag_s=0.6), -40.0)
        peak, lag = ref.match(echo + line_noise(len(echo)))
        assert peak > 0.85 and 590 <= lag <= 615, (peak, lag)
        clock.t += 0.3  # the same audio would now be a 0.9 s echo: outside the range
        assert ref.match(echo + line_noise(len(echo))) is None

    def test_no_bot_audio_recently_means_no_evidence(self):
        clock = Clock()
        ref = EchoReference(sample_rate=SR, clock=clock)
        assert ref.active() is False
        assert ref.match(at_level(speech_like(0.12, seed=2), -30.0)) is None
        send_bot_audio(ref, clock, at_level(speech_like(0.5, seed=1), -20.0))
        assert ref.active() is True
        clock.t += ref.max_lag_s + ref.window_s + 0.05
        assert ref.active() is False
        # Silence (digital zero) on either side is never a match.
        assert ref.match(np.zeros(int(0.12 * SR), dtype=np.float32)) is None

    def test_rejection_bursts_are_recorded_once(self):
        class Recorder:
            def __init__(self):
                self.events = []

            def add_event(self, kind, **data):
                self.events.append((kind, data))

        clock = Clock()
        start = clock.t
        rec = Recorder()
        ref = EchoReference(sample_rate=SR, clock=clock, recorder=rec)
        bot = at_level(speech_like(1.0, seed=1), -20.0)
        send_bot_audio(ref, clock, bot)
        for _ in range(3):
            echo = at_level(echo_window(bot, start, clock, lag_s=0.15), -45.0)
            assert ref.judge(echo, -45.0) is True
            clock.t += 0.02
        assert [k for k, _ in rec.events] == ["echo_reference_burst"]
        ev = rec.events[0][1]
        assert ev["ncc"] > 0.85 and abs(ev["lag_ms"] - 150) < 30
        assert ev["tier"] == "strong" and ev["mode"] == "enforce"
        assert ev["source_plausible"] is True and ev["would_reject"] is True and ev["actually_rejected"] is True
        assert ev["delta_db"] < -15 and ev["inbound_dbfs"] < ev["source_dbfs"]
        assert ref.stats["bursts_rejected"] == 1 and ref.stats["frames_rejected"] == 3 == ref.stats["would_reject"]
        assert ref.stats["longest_run_ms"] == 60.0 and ref.stats["lag_std_ms"] is not None


def telephony_gate(reference=None) -> CallerAudioGate:
    # The telephony noise-gate defaults (shared.turn_detection): floor + 8 dB,
    # +5 dB while the bot speaks, never below −52 dBFS.
    return CallerAudioGate(
        noise_margin_db=8.0, min_speech_ms=120.0, echo_min_speech_ms=180.0,
        hangover_ms=320.0, preroll_ms=160.0, echo_margin_db=5.0, echo_tail_ms=250.0,
        min_threshold_dbfs=-52.0, echo_reference=reference,
    )


async def run_gate(gate: CallerAudioGate, clock: Clock, reference, bot: np.ndarray, inbound_fn):
    """Drive the gate frame by frame while ``bot`` plays: ``inbound_fn(i)`` is the
    caller-leg audio for uplink frame ``i``. Returns the audio the gate passed."""
    passed = []

    async def _push(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, InputAudioRawFrame):
            passed.append(np.frombuffer(frame.audio, dtype="<i2"))

    gate.push_frame = _push
    # One second of line noise with the bot quiet: learns the noise floor.
    for i in range(50):
        frame = InputAudioRawFrame(audio=pcm(line_noise(FRAME, seed=i)), sample_rate=SR, num_channels=1)
        await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
    await gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    frames = len(bot) // FRAME
    for i in range(frames):
        clock.t += FRAME / SR
        if reference is not None:
            reference.add_output(pcm(bot[i * FRAME:(i + 1) * FRAME]), SR)
        frame = InputAudioRawFrame(audio=pcm(inbound_fn(i)), sample_rate=SR, num_channels=1)
        await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
    return np.concatenate(passed) if passed else np.zeros(0, dtype="<i2")


def delayed(bot: np.ndarray, i: int, lag_frames: int) -> np.ndarray:
    j = i - lag_frames
    if j < 0:
        return np.zeros(FRAME, dtype=np.float32)
    return bot[j * FRAME:(j + 1) * FRAME]


class TestGateWithReference:
    @pytest.mark.parametrize("leak_db", [-35, -25, -15])
    async def test_echo_alone_never_opens_the_gate(self, leak_db):
        clock = Clock()
        ref = EchoReference(sample_rate=SR, clock=clock)
        gate = telephony_gate(ref)
        bot = at_level(speech_like(2.0, seed=1), -20.0)
        passed = await run_gate(
            gate, clock, ref, bot,
            lambda i: delayed(bot, i, 9) * 10 ** (leak_db / 20) + line_noise(FRAME, seed=i),
        )
        stats = gate.stats()
        assert stats["opens"] == 0, stats
        assert not np.any(passed), "echo leaked through the gate"
        if leak_db >= -25:
            # Above the gate's absolute floor the echo is speechlike frame
            # after frame; every one of them was rejected on evidence.
            assert stats["echo_ref_rejected_ms"] > 1000, stats
            assert stats["echo_reference"]["bursts_rejected"] >= 1

    async def test_without_a_reference_the_same_echo_opens_the_gate(self):
        # The pre-existing behaviour, kept for the browser path: a −25 dB leak
        # sits far above the noise floor, so the level margin admits it.
        clock = Clock()
        gate = telephony_gate(None)
        bot = at_level(speech_like(2.0, seed=1), -20.0)
        passed = await run_gate(
            gate, clock, None, bot,
            lambda i: delayed(bot, i, 9) * 10 ** (-25 / 20) + line_noise(FRAME, seed=i),
        )
        assert gate.stats()["opens"] >= 1
        assert np.any(passed)

    async def test_caller_speaking_over_the_reply_opens_the_gate(self):
        clock = Clock()
        ref = EchoReference(sample_rate=SR, clock=clock)
        gate = telephony_gate(ref)
        bot = at_level(speech_like(2.0, seed=1), -20.0)
        caller = at_level(speech_like(2.0, seed=2), -30.0)

        def inbound(i):
            echo = delayed(bot, i, 9) * 10 ** (-25 / 20)
            voice = caller[i * FRAME:(i + 1) * FRAME] if i >= 20 else np.zeros(FRAME, dtype=np.float32)
            return echo + voice + line_noise(FRAME, seed=i)

        passed = await run_gate(gate, clock, ref, bot, inbound)
        stats = gate.stats()
        assert stats["opens"] >= 1, stats
        assert np.any(passed)
        # The echo before the caller started was held back.
        assert stats["echo_ref_rejected_ms"] >= 150, stats

    @pytest.mark.parametrize("caller_dbfs", [-42.0, -12.0])
    async def test_quiet_and_loud_callers_are_not_rejected_for_their_level(self, caller_dbfs):
        clock = Clock()
        ref = EchoReference(sample_rate=SR, clock=clock)
        gate = telephony_gate(ref)
        bot = at_level(speech_like(2.0, seed=1), -20.0)
        caller = at_level(speech_like(2.0, seed=5), caller_dbfs)
        passed = await run_gate(
            gate, clock, ref, bot,
            lambda i: delayed(bot, i, 9) * 10 ** (-30 / 20) + caller[i * FRAME:(i + 1) * FRAME] + line_noise(FRAME, seed=i),
        )
        assert gate.stats()["opens"] >= 1
        assert np.any(passed)


class TestEchoTail:
    async def test_echo_tail_after_the_bot_stops_is_rejected_and_the_caller_after_it_passes(self):
        # The last ~200 ms of echo arrive AFTER the bot's final frame was
        # sent (the path delay); they must still be matched against the
        # reference. A caller who starts 300 ms after the bot stops is
        # different audio and must pass.
        clock = Clock()
        ref = EchoReference(sample_rate=SR, clock=clock)
        gate = telephony_gate(ref)
        passed = []

        async def _push(frame, direction=FrameDirection.DOWNSTREAM):
            if isinstance(frame, InputAudioRawFrame):
                passed.append(np.frombuffer(frame.audio, dtype="<i2"))

        gate.push_frame = _push
        for i in range(50):
            await gate.process_frame(InputAudioRawFrame(audio=pcm(line_noise(FRAME, seed=i)), sample_rate=SR, num_channels=1), FrameDirection.DOWNSTREAM)
        await gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        bot = at_level(speech_like(1.0, seed=1), -20.0)
        caller = at_level(speech_like(1.0, seed=2), -30.0)
        lag_frames = 10  # 200 ms echo path
        frames = len(bot) // FRAME
        for i in range(frames + 40):  # bot for 1 s, then 0.8 s more of inbound
            clock.t += FRAME / SR
            if i < frames:
                ref.add_output(pcm(bot[i * FRAME:(i + 1) * FRAME]), SR)
            elif i == frames:
                await gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            echo = delayed(bot, i, lag_frames) * 10 ** (-20 / 20) if i < frames + lag_frames else np.zeros(FRAME, dtype=np.float32)
            j = i - (frames + 15)  # caller starts 300 ms after the bot's last frame
            voice = caller[j * FRAME:(j + 1) * FRAME] if 0 <= j < len(caller) // FRAME else np.zeros(FRAME, dtype=np.float32)
            await gate.process_frame(InputAudioRawFrame(audio=pcm(echo + voice + line_noise(FRAME, seed=i)), sample_rate=SR, num_channels=1), FrameDirection.DOWNSTREAM)
            if i == frames + lag_frames - 1:
                assert gate.stats()["opens"] == 0, "echo (including its tail) opened the gate"
        stats = gate.stats()
        assert stats["opens"] == 1, stats
        assert stats["echo_ref_rejected_ms"] >= 600, stats
        assert np.any(np.concatenate(passed))


class TestFeedTiming:
    @pytest.mark.parametrize("feed_delay_frames, expect_opens", [(0, 0), (2, 1)])
    async def test_a_reference_fed_after_the_pacing_wait_misses_a_fast_echo(self, feed_delay_frames, expect_opens):
        # Why the transport feeds the reference INSIDE its write path: fed
        # 40 ms late (a tap after the pacing wait), a 20 ms echo path returns
        # the frame before the reference holds it — nothing to match, and an
        # abrupt loud onset (an acknowledgement cue) opens the gate.
        clock = Clock()
        ref = EchoReference(sample_rate=SR, clock=clock)
        gate = telephony_gate(ref)

        async def _push(frame, direction=FrameDirection.DOWNSTREAM):
            pass

        gate.push_frame = _push
        for i in range(50):
            await gate.process_frame(InputAudioRawFrame(audio=pcm(line_noise(FRAME, seed=i)), sample_rate=SR, num_channels=1), FrameDirection.DOWNSTREAM)
        await gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        rng = np.random.default_rng(4)
        flat = rng.standard_normal(int(0.8 * SR))
        spectrum = np.fft.rfft(flat)
        freqs = np.fft.rfftfreq(len(flat), 1.0 / SR)
        spectrum[(freqs < 300) | (freqs > 3400)] = 0
        cue = at_level(np.fft.irfft(spectrum, len(flat)).astype(np.float32), -20.0)  # abrupt, sustained onset
        frames = len(cue) // FRAME
        for i in range(frames + 10):
            clock.t += FRAME / SR
            j = i - feed_delay_frames
            if 0 <= j < frames:
                ref.add_output(pcm(cue[j * FRAME:(j + 1) * FRAME]), SR)
            echo = delayed(cue, i, 1) * 10 ** (-15 / 20) if i < frames + 1 else np.zeros(FRAME, dtype=np.float32)
            await gate.process_frame(InputAudioRawFrame(audio=pcm(echo + line_noise(FRAME, seed=i)), sample_rate=SR, num_channels=1), FrameDirection.DOWNSTREAM)
        assert gate.stats()["opens"] == expect_opens, gate.stats()


class TestSourcePlausibility:
    def test_a_copy_not_clearly_below_its_source_is_not_enforceable(self):
        # Physically implausible as echo: the inbound window is as loud as or
        # louder than the bot audio it correlates with. Genuine caller voices
        # produce exactly this on real lines at loud voiced onsets (2026-09-25
        # cross-tenant replay: 73 such frames, all within 6 dB of or louder
        # than their supposed source; every real echo frame was ≥ 15 dB below).
        clock = Clock()
        start = clock.t
        ref = EchoReference(sample_rate=SR, clock=clock)
        bot = at_level(speech_like(1.0, seed=1), -30.0)
        send_bot_audio(ref, clock, bot)
        source = echo_window(bot, start, clock, lag_s=0.2)
        amplified = source * 10 ** (12 / 20)  # +12 dB over the window it copies
        d = ref.decide(amplified, dbfs(amplified))
        assert d.tier == "strong" and d.ncc > 0.9
        assert 11.0 < d.delta_db < 13.0 and d.source_plausible is False
        assert d.would_reject is False and d.actually_rejected is False
        assert ref.stats["candidates"] == 1 and ref.stats["source_implausible"] == 1
        assert ref.stats["would_reject"] == 0 and ref.stats["frames_rejected"] == 0
        # A copy at the SAME level is not clearly an echo either (the band
        # where real callers' voices land): not enforceable.
        d = ref.decide(source * 1.0, dbfs(source))
        assert -1.0 < d.delta_db < 1.0 and d.source_plausible is False and d.would_reject is False
        # 8 dB below the source: an echo, enforceable.
        d = ref.decide(source * 10 ** (-8 / 20), dbfs(source) - 8)
        assert -9.0 < d.delta_db < -7.0 and d.would_reject is True

    @pytest.mark.parametrize("attenuation_db", [15, 25, 35])
    def test_attenuated_copies_remain_enforceable(self, attenuation_db):
        clock = Clock()
        start = clock.t
        ref = EchoReference(sample_rate=SR, clock=clock)
        bot = at_level(speech_like(1.0, seed=1), -20.0)
        send_bot_audio(ref, clock, bot)
        echo = echo_window(bot, start, clock, lag_s=0.2) * 10 ** (-attenuation_db / 20)
        d = ref.decide(echo + line_noise(len(echo)), dbfs(echo))
        assert d.source_plausible is True and d.would_reject is True
        # At −35 dB the copy sits near the −65 dBFS line noise, which lifts
        # the measured inbound level; the delta is still far below the margin.
        assert abs(d.delta_db + attenuation_db) < (3.0 if attenuation_db <= 25 else 10.0), d.delta_db

    def test_weak_candidates_also_need_plausibility(self):
        clock = Clock()
        start = clock.t
        ref = EchoReference(sample_rate=SR, clock=clock)
        bot = at_level(speech_like(1.0, seed=1), -20.0)
        send_bot_audio(ref, clock, bot)
        assert ref.judge(at_level(echo_window(bot, start, clock, lag_s=0.2), -40.0), -40.0) is True  # establishes lag
        caller = speech_like(0.12, seed=3)
        # Weak-band mixture, louder than the source by 10 dB: consistent lag, not plausible.
        mixed = at_level(echo_window(bot, start, clock, lag_s=0.2), -12.0) + at_level(caller, -9.0)
        d = ref.decide(mixed, -8.0)
        assert d.tier in ("strong", "weak") and d.source_plausible is False and d.would_reject is False


class TestModes:
    def test_shadow_records_the_decision_but_never_rejects(self):
        class Recorder:
            def __init__(self):
                self.events = []

            def add_event(self, kind, **data):
                self.events.append((kind, data))

        clock = Clock()
        start = clock.t
        rec = Recorder()
        ref = EchoReference(sample_rate=SR, clock=clock, mode=MODE_SHADOW, recorder=rec)
        bot = at_level(speech_like(1.0, seed=1), -20.0)
        send_bot_audio(ref, clock, bot)
        echo = at_level(echo_window(bot, start, clock, lag_s=0.15), -45.0)
        d = ref.decide(echo, -45.0)
        assert d.would_reject is True and d.actually_rejected is False
        assert ref.judge(echo, -45.0) is False
        assert ref.stats["would_reject"] == 2 and ref.stats["frames_rejected"] == 0
        assert rec.events[0][0] == "echo_reference_burst"
        assert rec.events[0][1]["mode"] == "shadow" and rec.events[0][1]["actually_rejected"] is False

    async def test_shadow_gate_passes_echo_but_counts_it(self):
        clock = Clock()
        ref = EchoReference(sample_rate=SR, clock=clock, mode=MODE_SHADOW)
        gate = telephony_gate(ref)
        bot = at_level(speech_like(2.0, seed=1), -20.0)
        passed = await run_gate(gate, clock, ref, bot, lambda i: delayed(bot, i, 9) * 10 ** (-20 / 20) + line_noise(FRAME, seed=i))
        stats = gate.stats()
        assert stats["opens"] >= 1 and np.any(passed)             # behaviour unchanged: echo opens the gate
        assert stats["echo_ref_rejected_ms"] == 0.0
        assert stats["echo_ref_would_reject_ms"] > 500, stats     # ...but the evidence says what enforce would do
        assert stats["echo_reference"]["mode"] == "shadow"

    async def test_enforce_gate_rejects_the_same_echo(self):
        clock = Clock()
        ref = EchoReference(sample_rate=SR, clock=clock, mode=MODE_ENFORCE)
        gate = telephony_gate(ref)
        bot = at_level(speech_like(2.0, seed=1), -20.0)
        await run_gate(gate, clock, ref, bot, lambda i: delayed(bot, i, 9) * 10 ** (-20 / 20) + line_noise(FRAME, seed=i))
        stats = gate.stats()
        assert stats["opens"] == 0
        assert stats["echo_ref_rejected_ms"] == stats["echo_ref_would_reject_ms"] > 500

    def test_mode_setting_mapping_and_validation(self):
        assert mode_from_setting(0) == MODE_OFF and mode_from_setting(0.0) == MODE_OFF
        assert mode_from_setting(1) == MODE_SHADOW and mode_from_setting(2.0) == MODE_ENFORCE
        assert mode_from_setting("shadow") == MODE_SHADOW and mode_from_setting("weird") == MODE_OFF
        assert mode_from_setting(None) == MODE_OFF and mode_from_setting(7) == MODE_OFF
        with pytest.raises(ValueError):
            EchoReference(sample_rate=SR, mode="loud")

    def test_platform_default_is_off_and_builds_nothing(self):
        from shared.turn_detection import NOISE_GATE_DEFAULTS, NOISE_GATE_RECOMMENDED

        for transport in ("browser", "telephony"):
            assert NOISE_GATE_DEFAULTS[transport]["echo_reference_mode"] == 0.0
            assert NOISE_GATE_RECOMMENDED[transport]["echo_reference_mode"] == 0.0
            assert NOISE_GATE_DEFAULTS[transport]["echo_reference_max_lag_ms"] == 700.0

    def test_burst_events_are_capped_per_call(self):
        from voice_runtime.echo_reference import MAX_BURST_EVENTS

        class Recorder:
            def __init__(self):
                self.n = 0

            def add_event(self, kind, **data):
                self.n += 1

        clock = Clock()
        start = clock.t
        rec = Recorder()
        ref = EchoReference(sample_rate=SR, clock=clock, recorder=rec)
        bot = at_level(speech_like(1.0, seed=1), -20.0)
        send_bot_audio(ref, clock, bot)
        echo = at_level(echo_window(bot, start, clock, lag_s=0.15), -45.0)
        other = at_level(speech_like(0.12, seed=9), -30.0)
        for _ in range(MAX_BURST_EVENTS + 10):
            assert ref.judge(echo, -45.0) is True   # burst start
            assert ref.judge(other, -30.0) is False  # burst end
        assert rec.n == MAX_BURST_EVENTS
        assert ref.stats["bursts_rejected"] == MAX_BURST_EVENTS + 10


class TestTap:
    async def test_tap_copies_sent_audio_and_passes_frames_through(self):
        clock = Clock()
        ref = EchoReference(sample_rate=SR, clock=clock)
        tap = EchoReferenceTap(ref)
        pushed = []

        async def _push(frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

        tap.push_frame = _push
        frame = OutputAudioRawFrame(audio=pcm(at_level(speech_like(0.02, seed=1), -20.0)), sample_rate=SR, num_channels=1)
        await tap.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert pushed == [frame]
        assert ref.active() is True
