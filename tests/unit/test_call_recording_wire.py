"""Call recording = what the far end played (voice_runtime.recording
AlignedStereoRecorder + the output transport's wire tap).

Three defects fixed on 2026-11-10 (Haier bot_450a4f7b4832, demo calls
cv_fadadc7782d5 / cv_479df7320d2d / cv_b4cb8a4eeb66):

1. The recorder took the bot track from the CLEAN frames the transport
   pushes downstream, so the background ambience — mixed in at the wire —
   and the room-only audio between turns were never recorded.
2. The recorder laid bot audio onto the ARRIVAL clock of caller frames and
   padded the caller track for any frame that arrived > 200 ms late, then
   appended the delayed frames too. Every delivery hiccup lengthened the
   recording and cut a same-sized hole into the bot's speech that the caller
   never heard.
3. The FreeSWITCH fork packet ramp held audio back while the far end ran
   dry: ~80 ms + ~40 ms of dead air in the first 400 ms of every reply.

Part 1 drives the alignment logic with explicit clocks; part 2 runs the
production transport, serializers, ambience mixer and recording processor
against the far-end playout models of test_background_ambience.
"""

import asyncio
import base64
import json
import struct
import time

import numpy as np
import pytest
from pipecat.frames.frames import InputAudioRawFrame, InterruptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker

from tests.unit import test_background_ambience as harness
from voice_runtime.pipeline import AlignedRecordingProcessor
from voice_runtime.recording import AlignedStereoRecorder

RATE = 8000
SPEECH = harness.SPEECH
ROOM_MAX = harness.ROOM_MAX


def pcm(value, ms, rate=RATE):
    return struct.pack("<h", value) * int(round(rate * ms / 1000))


def tracks(stereo):
    data = np.frombuffer(bytes(stereo), dtype="<i2").reshape(-1, 2)
    return data[:, 0], data[:, 1]


def zero_runs(x, rate=RATE, min_ms=5.0):
    """(start_s, ms) of every exact-zero run of at least ``min_ms``."""
    edges = np.diff(np.concatenate(([0], (x == 0).astype(np.int8), [0])))
    return [
        (round(a / rate, 3), round((b - a) * 1000.0 / rate, 1))
        for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))
        if (b - a) * 1000.0 / rate >= min_ms
    ]


def seconds(x, rate=RATE):
    return len(x) / rate


# ── part 1: alignment logic ──────────────────────────────────────────────

class Timeline:
    """Feeds an aligner on an explicit clock; collects everything it emits."""

    def __init__(self, **kwargs):
        self.aligner = AlignedStereoRecorder(sample_rate=RATE, **kwargs)
        self.out = bytearray()

    def caller(self, at, value=5, ms=20):
        self.out += self.aligner.add_user_audio(pcm(value, ms), now=at)

    def caller_stream(self, start, end, value=5):
        """Real-time 20 ms frames whose capture ends in (start, end]."""
        t = start
        while t + 0.02 <= end + 1e-9:
            t += 0.02
            self.caller(round(t, 6), value)

    def bot(self, at, ms, value=900):
        self.out += self.aligner.add_bot_audio(pcm(value, ms), now=at)

    def stop(self, at):
        self.out += self.aligner.stop(now=at)
        return tracks(self.out)


class TestCallerClock:
    def test_a_delayed_burst_is_not_padded_and_cuts_no_hole_in_the_bot(self):
        """The 2026-11 artefact: caller frames held up for 360 ms (a busy
        processor upstream, an event-loop stall) and then delivered at once."""
        call = Timeline()
        call.caller_stream(0.0, 1.0)
        # Bot audio keeps going out in real time the whole while.
        for i in range(50):
            call.bot(1.0 + i * 0.04, 40)
        # The caller frames of 1.0 → 1.36 all arrive at 1.36.
        for _ in range(18):
            call.caller(1.36)
        call.caller_stream(1.36, 3.0)
        caller, bot = call.stop(3.0)
        assert abs(seconds(caller) - 3.0) < 0.03          # not 3.36
        assert zero_runs(caller) == []                    # nothing padded
        speech = np.flatnonzero(bot)
        assert abs(speech[0] / RATE - 1.0) < 0.03
        assert zero_runs(bot[speech[0]:speech[-1] + 1]) == []   # one unbroken reply
        assert abs((speech[-1] - speech[0]) / RATE - 2.0) < 0.01

    def test_recording_length_tracks_the_wall_clock_through_repeated_stalls(self):
        call = Timeline(chunk_seconds=0.5)
        t = 0.0
        for stall in (0.25, 0.4, 3.0, 0.21, 1.2):
            call.caller_stream(t, t + 2.0)
            t += 2.0
            for _ in range(int(round(stall / 0.02))):     # the held frames, at once
                call.caller(round(t + stall, 6))
            t += stall
        caller, _bot = call.stop(t)
        assert abs(seconds(caller) - t) < 0.05
        assert zero_runs(caller) == []

    def test_lost_caller_audio_is_silence_filled_once_where_it_was_lost(self):
        call = Timeline()
        call.caller_stream(0.0, 1.0, value=5)
        call.caller_stream(2.0, 4.0, value=7)             # 1 s never arrived
        caller, _bot = call.stop(4.0)
        assert abs(seconds(caller) - 4.0) < 0.03
        assert zero_runs(caller) == [(1.0, 1000.0)]
        assert set(caller[: RATE].tolist()) == {5} and set(caller[2 * RATE:].tolist()) == {7}

    def test_caller_stream_that_starts_after_the_bot_keeps_its_offset(self):
        call = Timeline()
        call.bot(10.0, 200)
        call.caller_stream(10.5, 11.0)
        caller, bot = call.stop(11.0)
        assert zero_runs(caller) == [(0.0, 500.0)]
        assert np.flatnonzero(bot)[0] == 0 and abs(seconds(caller) - 1.0) < 0.03

    def test_early_frames_move_the_origin_instead_of_padding_later(self):
        """The first frames were themselves delayed: later ones look early."""
        call = Timeline()
        call.caller(5.30)                                  # captured 5.00-5.02, 280 ms late
        call.caller_stream(5.02, 7.0)
        caller, _bot = call.stop(7.0)
        assert abs(seconds(caller) - 2.0) < 0.03 and zero_runs(caller) == []

    def test_bot_audio_sent_while_the_first_caller_frames_were_held_up_keeps_its_time(self):
        """Call start (real local call 2026-11-10): the first second of
        caller frames reaches the recorder late and in a burst while the
        room sound is already going out. The room must sit where it was
        sent — 0.9 s into the call — not at the recording's first sample."""
        call = Timeline()
        call.caller(100.90)                                # captured 100.00-100.02
        call.bot(100.90, 20, value=300)                    # the first room chunk goes out now
        for _ in range(44):                                # the held-up frames, at once
            call.caller(100.90)
        call.bot(100.92, 20, value=301)
        call.caller_stream(100.90, 102.0)
        caller, bot = call.stop(102.0)
        assert abs(seconds(caller) - 2.0) < 0.03 and zero_runs(caller) == []
        room = np.flatnonzero(bot)
        assert abs(room[0] / RATE - 0.90) < 0.025
        assert bot[room[0]] == 300 and bot[room[-1]] == 301 and len(room) == int(RATE * 0.04)

    def test_gate_pre_roll_replaces_the_silence_it_stood_for(self):
        """voice_runtime.audio_gate: closed, the gate passes silence on in
        real time and keeps the audio; opening, it releases the kept audio as
        extra frames. Appended (as before 2026-11-10) that is ~0.2 s more
        caller audio than wall time at EVERY opening — the recording grew and
        the bot track was cut by as much."""
        call = Timeline()
        call.caller_stream(0.0, 1.0, value=5)
        for i in range(25):                                # the reply being spoken meanwhile
            call.bot(1.0 + i * 0.04, 40)
        for opening in range(3):
            base = 1.0 + opening * 0.3
            for i in range(11):                            # 220 ms closed: silence downstream
                call.caller(round(base + (i + 1) * 0.02, 6), value=0)
            for _ in range(11):                            # … released on opening
                call.out += call.aligner.add_user_audio(pcm(700 + opening, 20), now=base + 0.22, replay=True)
            for i in range(4):                             # live speech
                call.caller(round(base + 0.22 + (i + 1) * 0.02, 6), value=900)
        caller, bot = call.stop(1.9)
        assert abs(seconds(caller) - 1.9) < 0.03           # not 1.9 + 3 × 0.22
        assert zero_runs(caller) == []                     # the onset is where it was spoken
        assert set(caller[int(1.0 * RATE):int(1.22 * RATE)].tolist()) == {700}
        speech = np.flatnonzero(bot)
        assert zero_runs(bot[speech[0]:speech[-1] + 1]) == []   # the reply is not cut

    def test_streamed_chunks_equal_the_one_shot_result(self):
        def run(chunk_seconds):
            call = Timeline(chunk_seconds=chunk_seconds)
            call.caller_stream(0.0, 0.6)
            for i in range(40):
                call.bot(0.6 + i * 0.04, 40, value=300 + i)
                call.caller_stream(0.6 + i * 0.04, 0.6 + (i + 1) * 0.04)
            call.aligner.flush_bot(now=2.2)
            call.caller_stream(2.2, 3.0)
            return bytes(call.out + call.aligner.stop(now=3.0))

        assert run(0.25) == run(1000.0)

    def test_an_absent_caller_stream_does_not_hold_the_recording_back(self):
        call = Timeline(chunk_seconds=1.0)
        for i in range(30 * 25):
            call.bot(i * 0.04, 40)
        assert len(call.out) > 0                           # streamed, not hoarded
        caller, bot = call.stop(30.0)
        assert abs(seconds(bot) - 30.0) < 0.05 and not caller.any()


class TestFarEndPlayout:
    def test_audio_sent_faster_than_real_time_queues_behind_what_is_playing(self):
        call = Timeline(chunk_seconds=0.5)
        call.caller_stream(0.0, 1.0)
        for i in range(10):                                # 2 s of reply in one burst
            call.bot(1.0, 200, value=1000 + i)
        call.caller_stream(1.0, 4.0)
        caller, bot = call.stop(4.0)
        assert abs(seconds(caller) - 4.0) < 0.03
        speech = np.flatnonzero(bot)
        assert abs(speech[0] / RATE - 1.0) < 0.025 and abs(speech[-1] / RATE - 3.0) < 0.025
        assert zero_runs(bot[speech[0]:speech[-1] + 1]) == []
        # in the order it was sent
        assert bot[speech[0]] == 1000 and bot[speech[-1]] == 1009

    def test_an_underrun_is_the_gap_the_far_end_heard(self):
        call = Timeline()
        call.caller_stream(0.0, 1.0)
        call.bot(1.0, 200)
        call.bot(1.5, 200)                                 # 300 ms after the far end ran dry
        call.caller_stream(1.0, 2.0)
        _caller, bot = call.stop(2.0)
        assert zero_runs(bot) == [(0.0, 1000.0), (1.2, 300.0), (1.7, 300.0)]

    def test_a_telephony_leg_restarts_on_its_next_20ms_tick(self):
        call = Timeline()
        call.caller_stream(0.0, 1.0)
        call.bot(1.003, 40)                                # mid-tick
        call.bot(1.047, 40)                                # 4 ms "late": inside the cushion
        call.bot(1.085, 40)
        call.caller_stream(1.0, 1.5)
        _caller, bot = call.stop(1.5)
        speech = np.flatnonzero(bot)
        assert speech[0] / RATE == pytest.approx(1.02) and len(speech) == int(RATE * 0.12)
        assert zero_runs(bot[speech[0]:speech[-1] + 1]) == []

    def test_the_browser_client_restarts_with_its_playback_lead(self):
        call = Timeline(far_end="browser")
        call.caller_stream(0.0, 1.0)
        call.bot(1.0, 40)
        call.caller_stream(1.0, 1.5)
        _caller, bot = call.stop(1.5)
        assert np.flatnonzero(bot)[0] / RATE == pytest.approx(1.04)

    def test_a_clear_drops_what_the_far_end_had_not_played(self):
        call = Timeline()
        call.caller_stream(0.0, 1.0)
        for _ in range(10):
            call.bot(1.0, 200)                             # 2 s queued at the far end
        call.caller_stream(1.0, 1.5)
        call.aligner.flush_bot(now=1.5)                    # barge-in: killAudio
        call.bot(1.9, 100, value=77)                       # the next reply
        call.caller_stream(1.5, 3.0)
        _caller, bot = call.stop(3.0)
        assert zero_runs(bot) == [(0.0, 1000.0), (1.5, 400.0), (2.0, 1000.0)]
        assert set(bot[int(1.9 * RATE):int(2.0 * RATE)].tolist()) == {77}

    def test_stop_keeps_only_what_the_far_end_had_played(self):
        def run(stop_at):
            call = Timeline()
            call.caller_stream(0.0, 0.2)
            for _ in range(10):
                call.bot(0.2, 200)                         # 2 s reply; the call ends inside it
            return call.stop(stop_at)

        caller, bot = run(0.5)                             # caller hung up 300 ms in
        assert abs(seconds(bot) - 0.5) < 0.03 and len(caller) == len(bot)
        caller, bot = run(5.0)                             # the bot ended the call after it
        assert abs(seconds(bot) - 2.2) < 0.03

    def test_stop_is_idempotent_and_nothing_is_taken_after_it(self):
        call = Timeline()
        call.caller_stream(0.0, 0.1)
        assert call.aligner.stop(now=0.1) != b""
        assert call.aligner.stop(now=0.2) == b""
        assert call.aligner.add_user_audio(pcm(1, 20), now=0.3) == b""
        assert call.aligner.add_bot_audio(pcm(1, 20), now=0.3) == b""


# ── part 2: production transport → far end and recording ─────────────────

class MemoryWriter:
    """CallRecordingWriter's interface, kept in memory."""

    def __init__(self):
        self.pcm = bytearray()
        self.rate = 0
        self.audiobuffer = None

    async def append(self, audio, sample_rate, channels):
        assert channels == 2
        self.rate = sample_rate
        self.pcm += audio


class RecordedCall(harness.Call):
    """harness.Call with the production recording processor where the
    pipeline puts it, wired the way voice_runtime.pipeline wires it."""

    def __init__(self, kind, *, ambience=True, record_rate=None, wire_tap=True, **kwargs):
        super().__init__(kind, ambience=ambience, **kwargs)
        self.record_rate = record_rate or self.rate
        self.writer = MemoryWriter()
        self.recording = AlignedRecordingProcessor(
            writer=self.writer, sample_rate=self.record_rate,
            far_end="browser" if kind == "browser" else "telephony",
        )
        if wire_tap:
            self.output.attach_wire_recorder(self.recording.attach_wire())
        self.worker = PipelineWorker(
            Pipeline([harness.Passthrough(), self.output, self.recording]),
            params=PipelineParams(audio_in_sample_rate=self.rate, audio_out_sample_rate=self.rate),
            enable_rtvi=False, idle_timeout_secs=None,
        )
        self.started = 0.0
        self.hold_caller_until = 0.0
        self._held = []
        self._stop_caller = asyncio.Event()
        self._caller_task = None

    async def __aenter__(self):
        await super().__aenter__()
        self.started = time.monotonic()
        self._caller_task = asyncio.create_task(self._caller())
        return self

    async def _caller(self):
        """Real-time 20 ms caller frames: faint line noise, never zero."""
        rng = np.random.default_rng(7)
        n = int(self.rate * 0.02)
        due = time.monotonic()
        while not self._stop_caller.is_set():
            frame = InputAudioRawFrame(
                audio=rng.integers(20, 60, n).astype("<i2").tobytes(),
                sample_rate=self.rate, num_channels=1,
            )
            if time.monotonic() < self.hold_caller_until:
                self._held.append(frame)                   # stuck upstream …
            else:
                for held in self._held:                    # … then all at once
                    await self.worker.queue_frame(held)
                self._held.clear()
                await self.worker.queue_frame(frame)
            due += 0.02
            await asyncio.sleep(max(0.0, due - time.monotonic()))

    async def finish(self):
        """End of the test call: (wall seconds, caller track, bot track)."""
        wall = time.monotonic() - self.started
        self._stop_caller.set()
        await self._caller_task
        self.wire_at_finish = len(self.wire.log)       # teardown audio is not the call
        await self.recording.stop_recording()
        caller, bot = tracks(self.writer.pcm)
        return wall, caller, bot

    def far_end_audio(self, t0, t1):
        """The far end's playout on the wall clock, one array from ``t0``."""
        out = np.zeros(int((t1 - t0) * self.rate) + 1, dtype=np.int32)
        for start, _end, _owner, samples in self.timeline():
            i = int(round((start - t0) * self.rate))
            if i < 0:
                samples, i = samples[-i:], 0
            seg = samples[: max(0, len(out) - i)]
            out[i:i + len(seg)] += seg
        return out.astype(np.int16)

    def wire_pcm(self):
        """Every audio packet the socket carried, in order."""
        packets = []
        for _t, kind, payload in self.wire.log[: self.wire_at_finish]:
            if kind != "audio":
                continue
            msg = json.loads(payload)
            packets.append(base64.b64decode(
                msg["data"]["audioContent"] if self.kind == "fork" else msg["media"]["payload"]
            ))
        return packets


def audible(gaps):
    """Far-end gaps of a playout tick or more. Shorter ones are this
    machine's scheduling jitter eating the 40 ms cushion, not a defect."""
    return [gap for gap in gaps if gap >= 0.015]


# The reply's first packets on Vaani still ramp (provider packet guidance),
# so its first 400 ms carry the ramp's gaps; steady audio starts after it.
RAMP_S = {"fork": 0.05, "vaani": 0.45}


def speech_onset(x, rate):
    hits = np.flatnonzero(np.abs(x.astype(np.int32)) > SPEECH // 2)
    return hits[0] / rate if hits.size else None


TELEPHONY = ["fork", "vaani"]


class TestRecordingIsTheFarEndPlayout:
    @pytest.mark.parametrize("kind", TELEPHONY)
    async def test_room_and_reply_are_recorded_as_sent(self, kind):
        async with RecordedCall(kind) as call:
            await asyncio.sleep(0.6)
            queued = await call.reply(1000)
            await harness.until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(1.8)
            wall, caller, bot = await call.finish()
        rate = call.rate
        assert abs(seconds(caller, rate) - wall) < 0.1                 # the call's length
        far = call.far_end_audio(call.started, call.started + wall)
        onset_far, onset_rec = speech_onset(far, rate), speech_onset(bot, rate)
        assert abs(onset_rec - onset_far) < 0.045                      # where the caller heard it
        # Room sound between turns: recorded, at the level that was sent.
        idle_far = far[int(0.15 * rate):int((onset_far - 0.05) * rate)]
        idle_rec = bot[int(0.15 * rate):int((onset_rec - 0.05) * rate)]
        assert np.count_nonzero(idle_rec) > 0.9 * len(idle_rec)
        assert abs(harness.dbfs(idle_rec) - harness.dbfs(idle_far)) < 1.0
        assert np.abs(idle_rec.astype(np.int32)).max() < ROOM_MAX
        # The reply carries the room, as on the wire (never clean speech).
        speech = bot[int((onset_rec + RAMP_S[kind]) * rate):int((onset_rec + 0.9) * rate)]
        assert np.abs(speech.astype(np.int32)).min() > SPEECH - ROOM_MAX
        assert len(set(np.abs(speech.astype(np.int32)).tolist())) > 20
        # … and the room carries on after it.
        tail = bot[int((onset_rec + 1.3) * rate):int((wall - 0.15) * rate)]
        assert np.count_nonzero(tail) > 0.9 * len(tail)
        # Bit-exact: every packet the socket carried is in the recording, in order.
        # (The last packets were still queued at the far end when the test
        # stopped the recording, so they are rightly cut short.)
        recorded, cursor = bot.tobytes(), 0
        for packet in call.wire_pcm()[:-4]:
            found = recorded.find(packet, cursor)
            assert found >= 0 and found % 2 == 0, "a wire packet is missing from the recording"
            cursor = found + len(packet)

    @pytest.mark.parametrize("kind", TELEPHONY + ["browser"])
    async def test_no_hole_in_the_recording_that_the_far_end_did_not_hear(self, kind):
        async with RecordedCall(kind) as call:
            await asyncio.sleep(0.5)
            queued = await call.reply(1200)
            await harness.until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(1.9)
            wall, _caller, bot = await call.finish()
        rate = call.rate
        onset = call.first_speech_after(queued)
        far_gaps = call.gaps(call.started + 0.2, call.started + wall - 0.2)
        rec_holes = zero_runs(bot[int(0.2 * rate):int((wall - 0.2) * rate)], rate)
        # Each side's silence, to one restart quantum (20 ms tick / 40 ms lead).
        assert abs(sum(ms for _s, ms in rec_holes) / 1000.0 - sum(far_gaps)) < 0.06, (far_gaps, rec_holes)
        if kind != "vaani":      # vaani keeps its provider-mandated packet ramp
            assert audible(call.gaps(onset, onset + 1.1)) == []

    async def test_browser_recording_is_resampled_to_the_recording_rate(self):
        """Browser wire 24 kHz, recording at the STT rate (16 kHz)."""
        async with RecordedCall("browser", record_rate=16000) as call:
            await asyncio.sleep(0.5)
            queued = await call.reply(1000)
            await harness.until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(1.4)
            wall, caller, bot = await call.finish()
        assert call.writer.rate == 16000
        onset_far = call.first_speech_after(queued) - call.started
        onset_rec = speech_onset(bot, 16000)
        assert abs(onset_rec - onset_far) < 0.05
        idle = bot[int(0.15 * 16000):int((onset_rec - 0.06) * 16000)]
        assert np.count_nonzero(idle) > 0.9 * len(idle)                # the room is there
        speech = np.abs(bot[int((onset_rec + 0.1) * 16000):int((onset_rec + 0.9) * 16000)].astype(np.int32))
        assert np.percentile(speech, 50) > SPEECH // 2                 # the reply, unbroken
        assert zero_runs(bot[int(onset_rec * 16000):int((onset_rec + 0.95) * 16000)], 16000) == []

    @pytest.mark.parametrize("kind", ["fork", "browser"])
    async def test_without_ambience_only_the_reply_is_recorded(self, kind):
        async with RecordedCall(kind, ambience=False) as call:
            await asyncio.sleep(0.4)
            queued = await call.reply(800)
            await harness.until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(1.3)
            wall, _caller, bot = await call.finish()
        rate = call.rate
        speech = np.flatnonzero(bot)
        assert abs((speech[-1] - speech[0] + 1) / rate - 0.8) < 0.045
        assert zero_runs(bot[speech[0]:speech[-1] + 1], rate) == []
        assert np.all(np.abs(bot[speech[0]:speech[-1] + 1].astype(np.int32)) >= SPEECH - 1)
        assert abs(speech[0] / rate - (call.first_speech_after(queued) - call.started)) < 0.045

    @pytest.mark.parametrize("kind", TELEPHONY + ["browser"])
    async def test_barge_in_cuts_the_recording_where_the_far_end_was_cut(self, kind):
        async with RecordedCall(kind) as call:
            await asyncio.sleep(0.4)
            queued = await call.reply(1500)
            await harness.until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(0.75)
            cut = time.monotonic() - call.started
            await call.worker.queue_frame(InterruptionFrame())
            await asyncio.sleep(0.9)
            wall, _caller, bot = await call.finish()
        rate = call.rate
        after = bot[int((cut + 0.1) * rate):int((wall - 0.1) * rate)]
        assert np.abs(after.astype(np.int32)).max() < ROOM_MAX           # no reply audio …
        assert np.count_nonzero(after) > 0.9 * len(after)                # … the room carries on
        before = bot[int((cut - 0.2) * rate):int((cut - 0.05) * rate)]
        assert np.abs(before.astype(np.int32)).min() > SPEECH - ROOM_MAX

    @pytest.mark.parametrize("kind", ["fork", "browser"])
    async def test_a_delayed_caller_burst_leaves_the_recording_untouched(self, kind):
        """Before the fix: +0.36 s of recording and a 360 ms hole in the reply."""
        async with RecordedCall(kind) as call:
            await asyncio.sleep(0.5)
            queued = await call.reply(1600)
            await harness.until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(0.5)
            call.hold_caller_until = time.monotonic() + 0.36
            await asyncio.sleep(1.8)
            wall, caller, bot = await call.finish()
        rate = call.rate
        assert abs(seconds(caller, rate) - wall) < 0.1
        assert zero_runs(caller, rate, min_ms=40.0) == []
        onset = speech_onset(bot, rate)
        assert zero_runs(bot[int(onset * rate):int((onset + 1.55) * rate)], rate) == []

    async def test_a_transport_without_the_wire_hook_records_the_pushed_frames(self):
        """Fallback: the clean frames after the transport, on the same
        playout clock (no room — this transport cannot report it)."""
        async with RecordedCall("fork", wire_tap=False) as call:
            await asyncio.sleep(0.4)
            queued = await call.reply(800)
            await harness.until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(1.2)
            wall, caller, bot = await call.finish()
        speech = np.flatnonzero(bot)
        assert abs(seconds(caller) - wall) < 0.1
        assert np.all(np.abs(bot[speech[0]:speech[-1] + 1].astype(np.int32)) == SPEECH)
        assert abs((speech[-1] - speech[0] + 1) / RATE - 0.8) < 0.045


class TestForkReplyStart:
    """mod_audio_fork plays packets from the moment they arrive; audio held
    back for a bigger packet is dead air at the far end."""

    @pytest.mark.parametrize("ambience", [True, False])
    async def test_no_dead_air_in_the_first_packets_of_a_reply(self, ambience):
        async with harness.Call("fork", ambience=ambience) as call:
            await asyncio.sleep(0.5)
            queued = await call.reply(1200)
            await harness.until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(1.6)
        onset = call.first_speech_after(queued)
        assert audible(call.gaps(onset, onset + 1.15)) == []
        # The first packet is as fast as it was (one 40 ms chunk, at once).
        assert onset - queued < 0.06
        speech = [
            len(base64.b64decode(json.loads(p)["data"]["audioContent"]))
            for _t, k, p in call.wire.log
            if k == "audio" and np.abs(np.frombuffer(
                base64.b64decode(json.loads(p)["data"]["audioContent"]), dtype="<i2",
            ).astype(np.int32)).max() > SPEECH // 2
        ]
        assert speech[0] == 640 and sum(speech) == 1200 * 16
        assert all(size % 320 == 0 for size in speech)

    async def test_audio_arriving_faster_than_it_plays_still_grows_packets(self):
        """The guard only fires while the far end is about to run dry."""
        from pipecat.frames.frames import OutputAudioRawFrame

        from voice_runtime.telephony import FreeSwitchAudioForkSerializer

        serializer = FreeSwitchAudioForkSerializer()
        sizes = []
        for _ in range(12):                                # 480 ms handed over at once
            payload = await serializer.serialize(OutputAudioRawFrame(
                audio=b"\x01\x02" * 320, sample_rate=8000, num_channels=1,
            ))
            if payload:
                sizes.append(len(base64.b64decode(json.loads(payload)["data"]["audioContent"])))
        # 640 at once, one more while the far end holds < 60 ms, then the
        # buffered packets of the ramp: nothing is rushed out needlessly.
        assert sizes[:2] == [640, 640] and max(sizes) >= 1280
        assert sum(sizes) + len(serializer._pending_audio) == 12 * 640


# ── part 3: the real caller gate in front of the recorder ────────────────

class TestGatePreRollOnTheRecording:
    """Demo calls of bot_450a4f7b4832 (cv_fadadc7782d5: 5 gate openings,
    recording 1.16 s longer than the 61.0 s of caller audio received, and
    160 ms + 60 ms of silence cut into "Namaskar" at 0.92 s, the moment the
    line noise opened the gate)."""

    async def _run(self, openings):
        from tests.unit.test_caller_audio_gate import SAMPLE_RATE, audio_frame, hiss, make_gate, tone

        gate = make_gate()
        aligner = AlignedStereoRecorder(sample_rate=SAMPLE_RATE)
        out = bytearray()
        clock = [50.0]
        replayed = [0]

        async def to_recorder(frame, direction=None):
            if isinstance(frame, InputAudioRawFrame):
                marked = getattr(frame, "gate_preroll", False)
                replayed[0] += marked
                out.extend(aligner.add_user_audio(frame.audio, now=clock[0], replay=marked))

        gate.push_frame = to_recorder

        async def line(payload, frames):
            for _ in range(frames):
                clock[0] += 0.02
                await gate.process_frame(audio_frame(payload), harness_direction())

        def harness_direction():
            from pipecat.processors.frame_processor import FrameDirection

            return FrameDirection.DOWNSTREAM

        await line(hiss(-62.0), 50)                         # 1 s of line noise: gate shut
        for _ in range(openings):
            await line(tone(-22.0), 40)                     # 0.8 s of speech: gate opens
            await line(hiss(-62.0), 60)                     # 1.2 s quiet: gate shuts again
        out.extend(aligner.stop(now=clock[0]))
        caller, _bot = tracks(out)
        return clock[0] - 50.0, caller, replayed[0], gate

    async def test_each_opening_releases_marked_pre_roll(self):
        wall, caller, replayed, gate = await self._run(openings=3)
        assert gate._stats["opens"] == 3
        assert replayed >= 3 * 6                            # ≥ 120 ms of pre-roll per opening
        # The recording is as long as the call — not 3 pre-rolls longer.
        assert abs(len(caller) / 8000 - wall) < 0.03

    async def test_the_onset_of_each_word_is_recorded_where_it_was_spoken(self):
        wall, caller, _replayed, _gate = await self._run(openings=2)
        loud = np.abs(caller.astype(np.int32)) > 1500
        for opening in range(2):
            start = 1.0 + opening * 2.0                     # speech begins here on the line
            first = np.flatnonzero(loud[int((start - 0.1) * 8000):])[0] / 8000 + start - 0.1
            assert abs(first - start) < 0.03                # not ~0.2 s late, not clipped
            spoken = loud[int((start + 0.02) * 8000):int((start + 0.78) * 8000)]
            assert spoken.mean() > 0.5                      # the whole word, unbroken
