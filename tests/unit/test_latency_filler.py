"""Latency fillers — synthesized breath, clip library, pipeline processor,
brain arming and pipeline construction.

The contract under test: a dispatched reply that has not started speaking
``delay_ms`` after the caller stopped gets a short, gender-matched breath as
plain output audio (never TTS audio, never a bot-speaking event), paced in
real time so reply audio is never queued behind it, and the breath stops the
instant reply audio, caller speech or any cancellation arrives.
"""

import asyncio
import math

import numpy as np
import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.audio.pcm import pcm_to_wav_bytes
from shared.bot_config import ResolvedBotConfig
from shared.orchestration.naturalness import HUMAN_SPEECH_DEFAULTS, SpeechNaturalnessPlanner
from voice_runtime.brain import ConversationBrain
import voice_runtime.latency_filler as latency_filler_module
from voice_runtime.latency_filler import (
    GENDERS,
    RUNGS,
    FillerClipLibrary,
    LatencyFillerProcessor,
    gender_from_filename,
    kind_from_filename,
    normalize_gender,
    scale_pcm,
    synthesize_breath,
)
from voice_runtime.pipeline import build_latency_filler
from voice_runtime.voiced_cues import VoicedCueLibrary, trim_silence

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM
RATE = 16000


def samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64)


def dbfs(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-9) / 32767.0)


def centroid_hz(pcm: bytes, rate: int) -> float:
    x = samples(pcm)
    spectrum = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(x.size, 1.0 / rate)
    return float((spectrum * freqs).sum() / spectrum.sum())


# ── synthesized breath ───────────────────────────────────────────────────


class TestSynthesizedBreath:
    @pytest.mark.parametrize("rate", [8000, 16000, 24000])
    def test_every_gender_is_short_quiet_and_click_free(self, rate):
        for gender in GENDERS:
            pcm = synthesize_breath(gender, rate)
            x = samples(pcm)
            duration_ms = x.size / rate * 1000.0
            assert 650 <= duration_ms <= 1000, (gender, duration_ms)
            rms = dbfs(float(np.sqrt(np.mean(x**2))))
            assert -40.0 <= rms <= -26.0, (gender, rms)         # presence, never a word
            assert dbfs(float(np.max(np.abs(x)))) <= -12.0        # far below speech peaks
            assert abs(x[:8]).max() < 100 and abs(x[-8:]).max() < 100  # no click at the ends

    def test_breath_is_audible_early_not_only_at_its_middle(self):
        # A reply landing 200–300 ms into the breath must cut a breath that
        # was already heard: the loudest 50 ms sits in the first ~40 % of the
        # clip and the first 300 ms carry a real share of its energy.
        for gender in GENDERS:
            x = samples(synthesize_breath(gender, RATE))
            window = int(RATE * 0.05)
            power = np.convolve(x**2, np.ones(window) / window, mode="valid")
            loudest_at = (int(np.argmax(power)) + window / 2) / x.size
            assert 0.2 <= loudest_at <= 0.42, (gender, loudest_at)
            first_300ms = float((x[: int(RATE * 0.3)] ** 2).sum() / (x**2).sum())
            assert first_300ms >= 0.3, (gender, first_300ms)

    def test_inhale_is_short_rising_and_quieter_than_the_breath(self):
        for gender in GENDERS:
            inhale = samples(synthesize_breath(gender, RATE, kind="inhale"))
            breath = samples(synthesize_breath(gender, RATE))
            duration_ms = inhale.size / RATE * 1000.0
            assert 250 <= duration_ms <= 400, (gender, duration_ms)
            window = int(RATE * 0.05)
            power = np.convolve(inhale**2, np.ones(window) / window, mode="valid")
            loudest_at = (int(np.argmax(power)) + window / 2) / inhale.size
            assert loudest_at >= 0.55, (gender, loudest_at)          # rises into the sentence
            assert dbfs(float(np.sqrt(np.mean(inhale**2)))) < dbfs(float(np.sqrt(np.mean(breath**2))))
            assert abs(inhale[-8:]).max() < 100 and abs(inhale[:8]).max() < 100
            assert centroid_hz(synthesize_breath(gender, RATE, kind="inhale"), RATE) > centroid_hz(
                synthesize_breath(gender, RATE), RATE
            )
        assert synthesize_breath("male", RATE, kind="bogus") == synthesize_breath("male", RATE)

    def test_male_is_darker_than_female(self):
        male = centroid_hz(synthesize_breath("male", RATE), RATE)
        neutral = centroid_hz(synthesize_breath("neutral", RATE), RATE)
        female = centroid_hz(synthesize_breath("female", RATE), RATE)
        assert male < neutral < female

    def test_deterministic_with_distinct_variants(self):
        assert synthesize_breath("female", RATE) == synthesize_breath("female", RATE)
        first = synthesize_breath("male", RATE, variant=0)
        second = synthesize_breath("male", RATE, variant=1)
        assert first != second
        assert synthesize_breath("male", RATE, variant=3) == first  # wraps

    def test_junk_inputs_degrade_safely(self):
        assert synthesize_breath("male", 0) == b""
        assert normalize_gender("MALE ") == "male"
        assert normalize_gender("robot") == "neutral"
        assert synthesize_breath("robot", RATE) == synthesize_breath("neutral", RATE)


# ── clip library ─────────────────────────────────────────────────────────


def _tone_wav(rate: int, duration_ms: int, amplitude: int) -> bytes:
    n = int(rate * duration_ms / 1000)
    pcm = np.full(n, amplitude, dtype="<i2").tobytes()
    return pcm_to_wav_bytes(pcm, sample_rate=rate)


class TestFillerClipLibrary:
    def test_gender_token_parsing(self, tmp_path):
        assert gender_from_filename(tmp_path / "filler_female_1.wav") == "female"
        assert gender_from_filename(tmp_path / "breath_MALE.wav") == "male"
        assert gender_from_filename(tmp_path / "female-2.wav") == "female"
        assert gender_from_filename(tmp_path / "hmm.wav") is None

    def test_synthesized_fallback_rotates_variants(self):
        library = FillerClipLibrary(None)
        clips = [library.clip("male", RATE) for _ in range(4)]
        assert all(clips)
        assert len({len(c) for c in clips[:3]}) == 3       # three distinct variants
        assert clips[3] == clips[0]                          # then round again
        assert library.describe()["female"] == [
            "synth:female:0", "synth:female:1", "synth:female:2",
        ]

    def test_operator_files_win_for_their_gender_only(self, tmp_path):
        (tmp_path / "filler_female_1.wav").write_bytes(_tone_wav(8000, 400, 900))
        (tmp_path / "filler_female_2.wav").write_bytes(_tone_wav(8000, 300, 900))
        (tmp_path / "notes.txt").write_text("not audio")
        library = FillerClipLibrary(tmp_path)
        described = library.describe()
        assert described["female"] == ["file:filler_female_1.wav", "file:filler_female_2.wav"]
        assert described["male"][0].startswith("synth:male")
        first = library.clip("female", RATE)
        second = library.clip("female", RATE)
        # Resampled 8 kHz → 16 kHz, so 400 ms and 300 ms of audio, in rotation.
        assert abs(len(first) / (RATE * 2) - 0.4) < 0.01
        assert abs(len(second) / (RATE * 2) - 0.3) < 0.01
        assert library.clip("female", RATE) == first
        # Faded edges: the constant tone now starts from silence.
        assert abs(samples(first)[0]) < 50 and abs(samples(first)[-1]) < 50
        assert library.clip("male", RATE) == synthesize_breath("male", RATE, variant=0)

    def test_broken_operator_file_falls_back_to_synthesized(self, tmp_path):
        (tmp_path / "filler_male.wav").write_bytes(b"RIFF junk that is not a wave file")
        library = FillerClipLibrary(tmp_path)
        assert library.clip("male", RATE) == synthesize_breath("male", RATE, variant=0)

    def test_missing_directory_means_synthesized(self, tmp_path):
        library = FillerClipLibrary(tmp_path / "does-not-exist")
        assert library.clip("neutral", 8000) == synthesize_breath("neutral", 8000, variant=0)

    def test_synthesis_disabled_and_no_files_yields_nothing(self):
        assert FillerClipLibrary(None, synthesize=False).clip("male", RATE) == b""

    def test_max_ms_trims_the_front_loaded_body_with_a_fade(self):
        library = FillerClipLibrary(None)
        full = library.clip("male", RATE)
        short = FillerClipLibrary(None).clip("male", RATE, max_ms=520)
        assert len(short) == int(RATE * 0.52) * 2 < len(full)
        assert short[: len(short) - int(RATE * 0.06) * 2] == full[: len(short) - int(RATE * 0.06) * 2]
        assert abs(samples(short)[-1]) < 40                     # faded to silence
        assert abs(samples(short[: -int(RATE * 0.06) * 2])).max() > 300  # body intact

    def test_inhale_files_and_synthesis_are_kept_apart_from_breaths(self, tmp_path):
        assert kind_from_filename(tmp_path / "inhale_female_1.wav") == "inhale"
        assert kind_from_filename(tmp_path / "filler_female_1.wav") == "breath"
        (tmp_path / "inhale_female.wav").write_bytes(_tone_wav(16000, 200, 700))
        library = FillerClipLibrary(tmp_path)
        assert library.describe("inhale")["female"] == ["file:inhale_female.wav"]
        assert library.describe()["female"][0].startswith("synth:female")
        assert library.describe("inhale")["male"] == [
            "synth-inhale:male:0", "synth-inhale:male:1", "synth-inhale:male:2",
        ]
        assert abs(len(library.clip("female", RATE, kind="inhale")) / (RATE * 2) - 0.2) < 0.01
        assert library.clip("male", RATE, kind="inhale") == synthesize_breath(
            "male", RATE, variant=0, kind="inhale"
        )
        assert library.clip("male", RATE) == synthesize_breath("male", RATE, variant=0)

    def test_gain_db_lowers_the_in_reply_breath(self):
        full = FillerClipLibrary(None).clip("female", RATE)
        quiet = FillerClipLibrary(None).clip("female", RATE, gain_db=-3.0)
        assert len(quiet) == len(full)
        ratio = float(np.sqrt(np.mean(samples(quiet) ** 2)) / np.sqrt(np.mean(samples(full) ** 2)))
        assert 0.69 <= ratio <= 0.72                             # -3 dB
        assert scale_pcm(b"", -3.0) == b"" and scale_pcm(full, 0.0) == full


# ── processor harness ────────────────────────────────────────────────────


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-filler"

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def kinds(self):
        return [kind for kind, _ in self.events]

    def data(self, kind):
        return [d for k, d in self.events if k == kind]


class _ShortLibrary:
    """Constant clips per gender, short enough for fast tests."""

    def __init__(self, clip_ms=120, rate=RATE):
        self.clip_ms = clip_ms
        self.rate = rate
        self.requests = []

    def clip(self, gender, sample_rate):
        self.requests.append((gender, sample_rate))
        level = {"male": 1000, "female": 2000, "neutral": 3000}[gender]
        n = int(sample_rate * self.clip_ms / 1000)
        return np.full(n, level, dtype="<i2").tobytes()


class _CueStub:
    """Voiced-cue library stand-in: constant clips per kind, warm() tracked."""

    def __init__(self, clip_ms=60, rate=RATE, missing=()):
        self.clip_ms = clip_ms
        self.rate = rate
        self.missing = set(missing)
        self.requests = []
        self.warmed = []

    def warm(self, engine, language):
        self.warmed.append((engine, language))

    def clip(self, engine, language, kind, sample_rate):
        self.requests.append((engine, language, kind, sample_rate))
        if kind in self.missing:
            return b""
        level = {"hmm": 4000, "wait": 5000}[kind]
        n = int(sample_rate * self.clip_ms / 1000)
        return np.full(n, level, dtype="<i2").tobytes()


def make_filler(*, delay_ms=60, library=None, rate=RATE, recorder=None, lead_chunks=2,
                cue_library=None, hmm_after_ms=None, spoken_after_ms=None):
    filler = LatencyFillerProcessor(
        delay_ms=delay_ms, library=library or _ShortLibrary(), sample_rate=rate,
        recorder=recorder or _RecorderStub(), chunk_ms=20, lead_chunks=lead_chunks,
        cue_library=cue_library, hmm_after_ms=hmm_after_ms, spoken_after_ms=spoken_after_ms,
    )
    filler.pushed = []

    async def _push(frame, direction=DOWN):
        filler.pushed.append((frame, direction))

    def _create_task(coro, name=None):
        return asyncio.get_running_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    filler.push_frame = _push
    filler.create_task = _create_task
    filler.cancel_task = _cancel_task
    return filler


def filler_audio(filler):
    return [
        f for f, _ in filler.pushed
        if isinstance(f, OutputAudioRawFrame) and not isinstance(f, TTSAudioRawFrame)
    ]


async def wait(seconds):
    await asyncio.sleep(seconds)
    for _ in range(3):
        await asyncio.sleep(0)


def tts_audio():
    return TTSAudioRawFrame(audio=b"\x01\x00" * 320, sample_rate=RATE, num_channels=1)


class TestLatencyFillerProcessor:
    async def test_fires_after_the_delay_as_plain_output_audio(self):
        filler = make_filler(delay_ms=60)
        await filler.arm(turn_id=1, gender="male")
        await wait(0.03)
        assert filler_audio(filler) == []
        await wait(0.25)
        frames = filler_audio(filler)
        assert frames, "breath never played"
        assert all(type(f) is OutputAudioRawFrame for f in frames)   # never TTS audio
        assert all(f.sample_rate == RATE and f.num_channels == 1 for f in frames)
        assert b"".join(f.audio for f in frames)[:2] == np.int16(1000).tobytes()
        assert not any(isinstance(f, BotStartedSpeakingFrame) for f, _ in filler.pushed)
        played = filler._recorder.data("latency_filler_played")
        assert played and played[0]["turn"] == 1 and played[0]["gender"] == "male"
        assert 55 <= played[0]["waited_ms"] <= 200
        assert filler._recorder.data("latency_filler_completed")
        assert filler.fillers_played == 1
        assert not filler.armed and not filler.playing

    async def test_reply_audio_before_the_deadline_means_no_filler(self):
        filler = make_filler(delay_ms=60)
        await filler.arm(turn_id=1, gender="female")
        await wait(0.02)
        frame = tts_audio()
        await filler.process_frame(frame, DOWN)
        assert filler.pushed[-1][0] is frame                      # forwarded untouched
        await wait(0.15)
        assert filler_audio(filler) == []
        assert filler.fillers_unneeded == 1 and filler.fillers_played == 0
        assert "latency_filler_played" not in filler._recorder.kinds()

    async def test_reply_audio_mid_filler_cuts_at_once_and_is_forwarded(self):
        filler = make_filler(delay_ms=20, library=_ShortLibrary(clip_ms=600))
        await filler.arm(turn_id=2, gender="male")
        await wait(0.12)
        before = len(filler_audio(filler))
        assert before >= 3
        frame = tts_audio()
        await filler.process_frame(frame, DOWN)
        index = [f for f, _ in filler.pushed].index(frame)
        await wait(0.15)
        # Exactly one 20 ms taper chunk lands BEFORE the reply frame (a breath
        # ending, not a click), then nothing but reply audio after it.
        assert all(
            isinstance(f, TTSAudioRawFrame) for f, _ in filler.pushed[index:]
        )
        assert len(filler_audio(filler)) == before + 1
        taper = filler_audio(filler)[-1]
        assert filler.pushed[index - 1][0] is taper
        assert len(taper.audio) == int(RATE * 0.02) * 2
        levels = samples(taper.audio)
        assert levels[0] > 900 and abs(levels[-1]) < 60      # fades 1000 → 0
        cut = filler._recorder.data("latency_filler_cut")
        assert cut and cut[0]["reason"] == "tts_audio" and cut[0]["played_ms"] > 50
        assert not filler.playing

    async def test_pacing_is_real_time_with_a_small_lead(self):
        filler = make_filler(delay_ms=10, library=_ShortLibrary(clip_ms=400), lead_chunks=2)
        await filler.arm(turn_id=1, gender="neutral")
        await wait(0.11)   # ~100 ms into playback
        pushed = len(filler_audio(filler))
        # ≈ 5 chunks of 20 ms elapsed + 2 chunks of lead (+ jitter), never the
        # whole clip at once.
        assert 4 <= pushed <= 9, pushed
        await wait(0.45)
        assert len(filler_audio(filler)) == 400 // 20
        total = sum(len(f.audio) for f in filler_audio(filler))
        assert total == int(RATE * 0.4) * 2

    async def test_clip_is_padded_to_whole_40ms(self):
        filler = make_filler(delay_ms=10, library=_ShortLibrary(clip_ms=130))
        await filler.arm(turn_id=1, gender="male")
        await wait(0.3)
        total_ms = sum(len(f.audio) for f in filler_audio(filler)) / (RATE * 2) * 1000
        assert total_ms == 160

    @pytest.mark.parametrize("frame,direction,reason", [
        (UserStartedSpeakingFrame(), DOWN, "caller_speech"),
        (InterruptionFrame(), DOWN, "interruption"),
        (BotStartedSpeakingFrame(), UP, "bot_speaking"),
        (EndFrame(), DOWN, "pipeline_end"),
    ])
    async def test_caller_speech_interruption_bot_audio_and_end_cut_playback(
        self, frame, direction, reason
    ):
        filler = make_filler(delay_ms=10, library=_ShortLibrary(clip_ms=600))
        await filler.arm(turn_id=1, gender="male")
        await wait(0.08)
        assert filler.playing
        before = len(filler_audio(filler))
        await filler.process_frame(frame, direction)
        assert filler.pushed[-1] == (frame, direction)             # always forwarded
        # No taper here: only reply audio earns a tail, everything else is
        # the caller or the pipeline taking over, and the breath just stops.
        assert len(filler_audio(filler)) == before
        await wait(0.1)
        assert len(filler_audio(filler)) == before
        assert filler._recorder.data("latency_filler_cut")[0]["reason"] == reason

    async def test_cancel_disarms_a_pending_filler_silently(self):
        filler = make_filler(delay_ms=40)
        await filler.arm(turn_id=1, gender="male")
        await filler.cancel("barge_in")
        assert not filler.armed
        await wait(0.12)
        assert filler_audio(filler) == []
        assert filler._recorder.events == []

    async def test_bot_still_speaking_at_the_deadline_defers_to_its_silence(
        self, monkeypatch
    ):
        # The previous reply's tail is still audible at the deadline: the
        # breath is HELD (not dropped) and plays a short gap after the bot
        # falls silent — the caller's wait continues past that silence.
        monkeypatch.setattr(latency_filler_module, "_RESUME_GAP_S", 0.03)
        filler = make_filler(delay_ms=30)
        await filler.process_frame(BotStartedSpeakingFrame(), UP)
        await filler.arm(turn_id=3, gender="male")
        await wait(0.1)
        assert filler_audio(filler) == []
        assert filler._recorder.data("latency_filler_deferred") == [
            {"turn": 3, "rung": "breath", "reason": "bot_speaking"},
        ]
        assert filler.armed and not filler.playing
        await filler.process_frame(BotStoppedSpeakingFrame(), UP)
        await wait(0.01)
        assert filler_audio(filler) == []  # the resume gap, not a gasp on the heels of speech
        await wait(0.25)
        assert filler_audio(filler)
        assert filler._recorder.data("latency_filler_played")[0]["turn"] == 3

    async def test_reply_audio_during_the_deferral_disarms_silently(self):
        filler = make_filler(delay_ms=10)
        await filler.process_frame(BotStartedSpeakingFrame(), UP)
        await filler.arm(turn_id=3, gender="male")
        await wait(0.05)
        await filler.process_frame(tts_audio(), DOWN)
        await filler.process_frame(BotStoppedSpeakingFrame(), UP)
        await wait(0.1)
        assert filler_audio(filler) == [] and not filler.armed
        assert filler.fillers_unneeded == 1

    async def test_delay_counts_from_the_end_of_caller_speech(self):
        filler = make_filler(delay_ms=80)
        now = asyncio.get_running_loop().time()
        import time as _time
        stopped = _time.monotonic() - 0.06
        await filler.arm(turn_id=1, gender="male", speech_stopped_at=stopped,
                         dispatched_at=_time.monotonic())
        await wait(0.045)       # 80 ms after the speech stop has now passed
        assert filler_audio(filler), "should fire ~20 ms after arming"
        del now

    async def test_stale_speech_stop_is_ignored(self):
        import time as _time
        filler = make_filler(delay_ms=60)
        await filler.arm(turn_id=1, gender="male", speech_stopped_at=_time.monotonic() - 30.0)
        await wait(0.03)
        assert filler_audio(filler) == []      # timed from dispatch, not 30 s ago
        await wait(0.1)
        assert filler_audio(filler)

    async def test_rearming_replaces_the_pending_turn(self):
        filler = make_filler(delay_ms=40, library=_ShortLibrary(clip_ms=120))
        await filler.arm(turn_id=1, gender="male")
        await wait(0.02)
        await filler.arm(turn_id=2, gender="female")
        await wait(0.25)
        played = filler._recorder.data("latency_filler_played")
        assert [p["turn"] for p in played] == [2]
        assert b"".join(f.audio for f in filler_audio(filler))[:2] == np.int16(2000).tobytes()

    async def test_no_clip_available_is_recorded_not_raised(self):
        filler = make_filler(delay_ms=10, library=FillerClipLibrary(None, synthesize=False))
        await filler.arm(turn_id=1, gender="male")
        await wait(0.06)
        assert filler_audio(filler) == []
        assert filler._recorder.data("latency_filler_skipped") == [
            {"turn": 1, "rung": "breath", "reason": "no_clip", "gender": "male"},
        ]

    async def test_start_frame_sets_the_output_rate(self):
        library = _ShortLibrary(clip_ms=40)
        filler = make_filler(delay_ms=10, library=library, rate=24000)
        await filler.process_frame(StartFrame(audio_out_sample_rate=8000), DOWN)
        await filler.arm(turn_id=1, gender="male")
        await wait(0.1)
        assert library.requests == [("male", 8000)]
        assert all(f.sample_rate == 8000 for f in filler_audio(filler))

    async def test_real_synthesized_clip_streams_to_completion(self):
        filler = make_filler(delay_ms=10, library=FillerClipLibrary(None), rate=8000)
        await filler.process_frame(StartFrame(audio_out_sample_rate=8000), DOWN)
        await filler.arm(turn_id=1, gender="female")
        await wait(0.9)
        audio = b"".join(f.audio for f in filler_audio(filler))
        clip = synthesize_breath("female", 8000, variant=0)
        assert audio[:len(clip)] == clip
        assert filler._recorder.data("latency_filler_completed")


# ── brain wiring ─────────────────────────────────────────────────────────



def levels(filler):
    """Distinct sample levels of the plain audio pushed so far, in order."""
    seen = []
    for frame in filler_audio(filler):
        for level in np.unique(np.frombuffer(frame.audio, dtype="<i2")):
            if level and (not seen or seen[-1] != level):
                seen.append(int(level))
    return seen


class TestEscalationLadder:
    """Breath → "हम्म…" → "एक सेकंड…" on a long wait; each rung only when the
    reply is still not speaking, cues only when already rendered."""

    def make(self, monkeypatch, *, cue_library=None, hmm=90, spoken=150, delay=30):
        monkeypatch.setattr(latency_filler_module, "_MIN_RUNG_GAP_S", 0.01)
        return make_filler(
            delay_ms=delay, cue_library=cue_library if cue_library is not None else _CueStub(),
            hmm_after_ms=hmm, spoken_after_ms=spoken,
        )

    async def test_rungs_play_in_order_as_plain_audio_when_the_reply_stays_away(
        self, monkeypatch
    ):
        filler = self.make(monkeypatch)
        await filler.arm(
            turn_id=1, gender="male", language="hi-IN",
            engine={"provider": "sarvam", "voice": "shubh"},
        )
        await wait(0.35)
        played = filler._recorder.data("latency_filler_played")
        assert [p["rung"] for p in played] == ["breath", "hmm", "wait"]
        assert levels(filler) == [1000, 4000, 5000]
        assert filler.rungs_played == {"breath": 1, "hmm": 1, "wait": 1}
        assert filler.fillers_played == 3
        assert not any(isinstance(f, TTSAudioRawFrame) for f, _ in filler.pushed)
        # Cues are rendered/warmed for the engine that will speak the reply.
        assert filler._cue_library.warmed == [({"provider": "sarvam", "voice": "shubh"}, "hi-IN")]
        assert filler._cue_library.requests[0][:3] == (
            {"provider": "sarvam", "voice": "shubh"}, "hi-IN", "hmm",
        )
        assert not filler.armed

    async def test_reply_audio_after_the_breath_cuts_the_ladder_and_is_not_unneeded(
        self, monkeypatch
    ):
        filler = self.make(monkeypatch, hmm=250, spoken=350)
        filler._library = _ShortLibrary(clip_ms=60)
        await filler.arm(turn_id=1, gender="male", language="hi-IN")
        await wait(0.15)  # breath done (~30 + 60 ms), hmm not yet due
        assert [p["rung"] for p in filler._recorder.data("latency_filler_completed")] == ["breath"]
        await filler.process_frame(tts_audio(), DOWN)
        await wait(0.2)
        assert filler.rungs_played["hmm"] == 0
        assert filler.fillers_unneeded == 0  # the breath WAS needed
        assert not filler.armed

    async def test_reply_audio_mid_cue_cuts_with_a_taper(self, monkeypatch):
        filler = self.make(monkeypatch, cue_library=_CueStub(clip_ms=400))
        await filler.arm(turn_id=2, gender="female", language="hi-IN")
        await wait(0.16)
        assert filler.playing and filler._armed.rung_kind == "hmm"
        await filler.process_frame(tts_audio(), DOWN)
        cut = filler._recorder.data("latency_filler_cut")
        assert cut and cut[0]["rung"] == "hmm" and cut[0]["reason"] == "tts_audio"
        assert not filler.armed

    async def test_spoken_rung_is_withheld_on_critical_turns(self, monkeypatch):
        filler = self.make(monkeypatch)
        await filler.arm(turn_id=1, gender="male", language="hi-IN", allow_spoken=False)
        await wait(0.3)
        assert [p["rung"] for p in filler._recorder.data("latency_filler_played")] == [
            "breath", "hmm",
        ]
        assert filler._recorder.data("latency_filler_skipped") == [
            {"turn": 1, "rung": "wait", "reason": "spoken_withheld"},
        ]

    async def test_missing_cue_is_skipped_and_the_ladder_continues(self, monkeypatch):
        filler = self.make(monkeypatch, cue_library=_CueStub(missing={"hmm"}))
        await filler.arm(turn_id=1, gender="male", language="hi-IN")
        await wait(0.3)
        assert [p["rung"] for p in filler._recorder.data("latency_filler_played")] == [
            "breath", "wait",
        ]
        skipped = filler._recorder.data("latency_filler_skipped")
        assert skipped[0]["rung"] == "hmm" and skipped[0]["reason"] == "no_clip"

    async def test_voiced_cues_open_and_close_the_echo_shield_window(self, monkeypatch):
        filler = self.make(monkeypatch)
        windows = []
        filler.cue_window_hook = windows.append
        await filler.arm(turn_id=1, gender="male", language="hi-IN")
        await wait(0.075)
        assert windows == []  # the breath is too quiet to shield
        await wait(0.25)
        assert windows == [True, False, True, False]

    async def test_tts_start_before_a_rung_holds_it_and_the_reply_cuts_silently(
        self, monkeypatch
    ):
        # Synthesis requested (TTSStartedFrame) before the hmm deadline: the
        # hmm never starts (a 200 ms chopped cue is a grunt), the reply audio
        # then disarms; the breath that DID play keeps this from "unneeded".
        filler = self.make(monkeypatch, hmm=250, spoken=350)
        filler._library = _ShortLibrary(clip_ms=60)
        await filler.arm(turn_id=1, gender="male", language="hi-IN")
        await wait(0.15)  # breath done
        await filler.process_frame(TTSStartedFrame(), DOWN)
        await wait(0.2)   # hmm deadline passes
        assert filler.rungs_played["hmm"] == 0
        skipped = filler._recorder.data("latency_filler_skipped")
        assert skipped == [{"turn": 1, "rung": "hmm", "reason": "reply_imminent"}]
        assert not filler.armed
        assert filler.fillers_unneeded == 0

    async def test_tts_start_before_the_breath_means_no_filler_at_all(self):
        filler = make_filler(delay_ms=40)
        await filler.arm(turn_id=1, gender="male")
        await filler.process_frame(TTSStartedFrame(), DOWN)
        await wait(0.1)
        assert filler_audio(filler) == []
        assert filler._recorder.data("latency_filler_skipped")[0]["reason"] == "reply_imminent"

    async def test_tts_start_during_a_playing_rung_lets_it_run_until_audio(self, monkeypatch):
        filler = self.make(monkeypatch, cue_library=_CueStub(clip_ms=400), delay=10)
        filler._library = _ShortLibrary(clip_ms=400)
        await filler.arm(turn_id=1, gender="male", language="hi-IN")
        await wait(0.1)
        assert filler.playing
        await filler.process_frame(TTSStartedFrame(), DOWN)
        await wait(0.05)
        assert filler.playing  # not cut by the start frame itself
        await filler.process_frame(tts_audio(), DOWN)
        assert not filler.playing
        assert filler._recorder.data("latency_filler_cut")[0]["reason"] == "tts_audio"

    async def test_rung_start_is_noted_on_the_clip_library(self, monkeypatch):
        filler = self.make(monkeypatch)
        library = FillerClipLibrary(None)
        filler._library = library
        assert not library.recently_played(10.0)
        await filler.arm(turn_id=1, gender="male", language="hi-IN")
        await wait(0.06)
        assert library.recently_played(10.0)
        assert not library.recently_played(0.0)

    async def test_no_cue_library_means_breath_only(self):
        filler = make_filler(delay_ms=20, hmm_after_ms=40, spoken_after_ms=60)
        assert not filler.ladder_enabled
        await filler.arm(turn_id=1, gender="male")
        await wait(0.2)
        assert [p["rung"] for p in filler._recorder.data("latency_filler_played")] == ["breath"]
        assert not filler.armed

    async def test_rearm_after_the_early_ack_holds_the_first_rung_a_beat(self, monkeypatch):
        monkeypatch.setattr(latency_filler_module, "_RESUME_GAP_S", 0.08)
        filler = make_filler(delay_ms=10)
        # Deadline long past (the ack took the first second of the wait).
        await filler.arm(turn_id=2, gender="male", speech_stopped_at=asyncio.get_running_loop().time() - 2.0
                         if False else None, dispatched_at=None, resume=True)
        await wait(0.04)
        assert filler_audio(filler) == []
        await wait(0.1)
        assert filler_audio(filler)

    def test_rung_order_and_delay_monotonicity(self):
        assert RUNGS == ("breath", "hmm", "wait")
        filler = make_filler(delay_ms=1500, cue_library=_CueStub(), hmm_after_ms=1000,
                             spoken_after_ms=900)
        # Later rungs can never be scheduled before earlier ones.
        assert filler._rung_delays_s == {"breath": 1.5, "hmm": 1.5, "wait": 1.5}


# ── voiced cue library ───────────────────────────────────────────────────


def tone_with_padding(rate=RATE, ms=300, pad_ms=200, level=8000):
    n = int(rate * ms / 1000)
    tone = (level * np.sin(2 * np.pi * 300 * np.arange(n) / rate)).astype("<i2")
    pad = np.zeros(int(rate * pad_ms / 1000), dtype="<i2")
    return np.concatenate([pad, tone, pad]).tobytes()


class TestVoicedCueLibrary:
    def renderer(self, calls, *, fail=False, rate=RATE):
        async def render(engine, language, text):
            calls.append((dict(engine), language, text))
            if fail:
                raise RuntimeError("provider down")
            return tone_with_padding(rate), rate
        return render

    def test_trim_silence_keeps_the_voiced_body(self):
        pcm = tone_with_padding()
        trimmed = trim_silence(pcm, RATE)
        assert 0.3 * RATE * 2 <= len(trimmed) <= 0.4 * RATE * 2
        assert trim_silence(b"\x00" * 4000, RATE) == b""

    async def test_first_request_renders_in_the_background_then_serves_from_cache(self, tmp_path):
        calls = []
        lib = VoicedCueLibrary(tmp_path, renderer=self.renderer(calls))
        engine = {"provider": "sarvam", "model": "bulbul:v2", "voice": "shubh"}
        assert lib.clip(engine, "hi-IN", "hmm", 8000) == b""  # never blocks
        await lib.wait_ready(engine, "hi-IN")
        assert calls == [(engine, "hi-IN", "हम्म…")]
        clip = lib.clip(engine, "hi-IN", "hmm", 8000)
        assert clip and len(clip) < 0.5 * 8000 * 2  # trimmed, resampled to 8 kHz
        assert lib.clip(engine, "hi-IN", "hmm", RATE) != clip
        assert len(calls) == 1
        # Quieter than the raw render (a cue sits under the reply's level).
        raw_peak = np.abs(np.frombuffer(tone_with_padding(), dtype="<i2")).max()
        assert np.abs(np.frombuffer(lib.clip(engine, "hi-IN", "hmm", RATE), dtype="<i2")).max() < raw_peak
        # A fresh process finds the WAV on disk and never re-renders.
        again = VoicedCueLibrary(tmp_path, renderer=self.renderer(calls, fail=True))
        assert again.clip(engine, "hi-IN", "hmm", RATE) == lib.clip(engine, "hi-IN", "hmm", RATE)
        assert len(calls) == 1

    async def test_warm_renders_every_cue_of_the_language_once(self, tmp_path):
        calls = []
        lib = VoicedCueLibrary(tmp_path, renderer=self.renderer(calls))
        engine = {"provider": "elevenlabs", "voice": "v1"}
        lib.warm(engine, "en-IN")
        lib.warm(engine, "en-IN")
        await lib.wait_ready(engine, "en-IN")
        assert sorted(c[2] for c in calls) == ["Hmm…", "One second…"]
        assert lib.ready(engine, "en-IN", "wait")

    async def test_failed_render_is_remembered_and_yields_nothing(self, tmp_path):
        calls = []
        lib = VoicedCueLibrary(tmp_path, renderer=self.renderer(calls, fail=True))
        engine = {"provider": "sarvam", "voice": "x"}
        lib.clip(engine, "hi-IN", "hmm", RATE)
        await lib.wait_ready(engine, "hi-IN")
        assert lib.render_failures == 1
        assert lib.clip(engine, "hi-IN", "hmm", RATE) == b""
        await wait(0.01)
        assert len(calls) == 1  # cooldown: no hammering the provider per turn

    async def test_unsupported_language_never_renders(self, tmp_path):
        calls = []
        lib = VoicedCueLibrary(tmp_path, renderer=self.renderer(calls))
        assert lib.clip({"provider": "sarvam"}, "fr-FR", "hmm", RATE) == b""
        assert lib.clip({"provider": "sarvam"}, "hi-IN", "sigh", RATE) == b""
        await wait(0.01)
        assert calls == []

    def test_engine_key_is_filesystem_safe_and_voice_specific(self):
        a = VoicedCueLibrary.engine_key({"provider": "sarvam", "model": "bulbul:v2", "voice": "shubh"}, "hi-IN")
        b = VoicedCueLibrary.engine_key({"provider": "sarvam", "model": "bulbul:v2", "voice": "anushka"}, "hi-IN")
        assert a != b and "/" not in a and ":" not in a


class _BrainRecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-brain"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0}
        self.turns = []
        self.language = "hi-IN"

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        self.turns.append(turn)

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))

    def flush_event_soon(self, kind, **data):
        self.events.append((kind, data))


class _FillerStub:
    def __init__(self):
        self.arms = []
        self.cancels = []

    async def arm(self, **kwargs):
        self.arms.append(kwargs)

    async def cancel(self, reason="cancelled"):
        self.cancels.append(reason)


GRACE = 0.02


def make_brain(filler, tts=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
        tts=tts if tts is not None else {
            "provider": "sarvam", "voice": "anushka", "voice_gender": "female",
        },
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_BrainRecorderStub(),
        finalize_grace=GRACE, latency_filler=filler,
    )
    brain._pushed = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        pass

    async def _handle(text):
        brain.handled.append(text)

    brain.handled = []
    brain.push_frame = _push
    brain._notify_client = _notify
    brain._handle_turn = _handle

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


def transcript(text):
    return TranscriptionFrame(text=text, user_id="u", timestamp="t", language="hi-IN")


async def caller_turn(brain, text="हाँ बोल रहा हूँ"):
    await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
    await brain.process_frame(transcript(text), DOWN)
    await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
    await wait(GRACE * 3)


class TestBrainWiring:
    async def test_dispatch_arms_the_filler_with_the_active_voice_gender(self):
        filler = _FillerStub()
        brain = make_brain(filler)
        await caller_turn(brain)
        assert brain.handled == ["हाँ बोल रहा हूँ"]
        assert len(filler.arms) == 1
        armed = filler.arms[0]
        assert armed["turn_id"] == 1 and armed["gender"] == "female"
        assert armed["dispatched_at"] is not None
        # The dispatch cancels any previous turn's filler before arming.
        assert filler.cancels[-1] == "new_turn"

    async def test_per_language_voice_gender_is_followed(self):
        filler = _FillerStub()
        brain = make_brain(filler, tts={
            "provider": "sarvam", "voice": "anushka", "voice_gender": "female",
            "language_map": {"hi-IN": {
                "provider": "sarvam", "voice": "abhilash", "voice_gender": "male",
            }},
        })
        await caller_turn(brain)
        assert filler.arms[0]["gender"] == "male"

    async def test_cancellation_paths_disarm_the_filler(self):
        filler = _FillerStub()
        brain = make_brain(filler)
        await caller_turn(brain)
        await brain._cancel_generation("barge_in")
        assert filler.cancels[-1] == "barge_in"
        await brain.cleanup()
        assert filler.cancels[-1] == "cleanup"

    async def test_arm_carries_language_engine_and_spoken_permission(self):
        filler = _FillerStub()
        brain = make_brain(filler)
        await caller_turn(brain)
        armed = filler.arms[0]
        assert armed["language"] == "hi-IN"
        assert armed["engine"]["provider"] == "sarvam" and armed["engine"]["voice"] == "anushka"
        assert armed["allow_spoken"] is True and armed["resume"] is False

    async def test_critical_caller_content_withholds_the_spoken_rung(self):
        filler = _FillerStub()
        brain = make_brain(filler)
        await caller_turn(brain, "मेरा नंबर 9876543210 है")
        assert filler.arms[-1]["allow_spoken"] is False

    async def test_early_ack_stands_the_filler_down_and_bot_silence_rearms_it(self):
        filler = _FillerStub()
        brain = make_brain(filler)
        brain._naturalness = SpeechNaturalnessPlanner({"acknowledgement_probability": 1.0})
        await caller_turn(brain)
        # The "जी…" went to TTS; the armed breath was cancelled for it.
        assert brain._early_ack_pending is True
        assert filler.cancels[-1] == "early_ack"
        assert len(filler.arms) == 1
        # The ack's audio comes and goes while the reply is still generating.
        brain._generation = asyncio.get_running_loop().create_task(asyncio.sleep(1))
        try:
            await brain.process_frame(BotStartedSpeakingFrame(), UP)
            assert brain._reply_audio_started is False  # the ack is not the reply
            await brain.process_frame(BotStoppedSpeakingFrame(), UP)
            assert brain._early_ack_pending is False
            assert len(filler.arms) == 2
            rearmed = filler.arms[1]
            assert rearmed["resume"] is True and rearmed["turn_id"] == filler.arms[0]["turn_id"]
        finally:
            brain._generation.cancel()

    async def test_no_rearm_when_the_reply_already_speaks_or_is_done(self):
        filler = _FillerStub()
        brain = make_brain(filler)
        brain._naturalness = SpeechNaturalnessPlanner({"acknowledgement_probability": 1.0})
        await caller_turn(brain)
        assert brain._early_ack_pending is True
        # Generation finished (nothing in flight): nothing to cover any more.
        await brain.process_frame(BotStartedSpeakingFrame(), UP)
        await brain.process_frame(BotStoppedSpeakingFrame(), UP)
        assert len(filler.arms) == 1

    async def test_brain_without_a_filler_dispatches_unchanged(self):
        brain = make_brain(None)
        await caller_turn(brain)
        assert brain.handled == ["हाँ बोल रहा हूँ"]
        assert brain._latency_filler is None


# ── pipeline construction ────────────────────────────────────────────────


class TestPipelineBuilder:
    def test_defaults_enable_latency_fillers(self):
        assert HUMAN_SPEECH_DEFAULTS["latency_fillers"] is True
        assert HUMAN_SPEECH_DEFAULTS["latency_filler_delay_ms"] == 1500

    def test_disabled_config_builds_no_processor(self):
        library = FillerClipLibrary(None)
        off = SpeechNaturalnessPlanner({"latency_fillers": False})
        assert build_latency_filler(off, sample_rate=8000, library=library) is None
        master_off = SpeechNaturalnessPlanner({"enabled": False})
        assert build_latency_filler(master_off, sample_rate=8000, library=library) is None

    def test_ladder_defaults_and_wiring(self):
        assert HUMAN_SPEECH_DEFAULTS["latency_filler_ladder"] is True
        assert HUMAN_SPEECH_DEFAULTS["latency_filler_hmm_ms"] == 3500
        assert HUMAN_SPEECH_DEFAULTS["latency_filler_spoken_ms"] == 5000
        cues = _CueStub()
        processor = build_latency_filler(
            SpeechNaturalnessPlanner({}), sample_rate=8000,
            library=FillerClipLibrary(None), cue_library=cues,
        )
        assert processor.ladder_enabled and processor._cue_library is cues
        assert processor._rung_delays_s == {"breath": 1.5, "hmm": 3.5, "wait": 5.0}
        off = build_latency_filler(
            SpeechNaturalnessPlanner({"latency_filler_ladder": False}), sample_rate=8000,
            library=FillerClipLibrary(None), cue_library=cues,
        )
        assert not off.ladder_enabled and off._cue_library is None

    def test_enabled_config_carries_delay_and_rate(self):
        planner = SpeechNaturalnessPlanner({"latency_filler_delay_ms": 900})
        recorder = _RecorderStub()
        processor = build_latency_filler(
            planner, sample_rate=8000, recorder=recorder, library=FillerClipLibrary(None),
        )
        assert isinstance(processor, LatencyFillerProcessor)
        assert processor.delay_ms == 900
        assert processor._sample_rate == 8000
        assert processor._recorder is recorder
