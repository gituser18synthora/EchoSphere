"""Background ambience (voice_runtime.ambience + the output transport).

Asset/mixer units, the human_speech switch, and the transport behaviour on
every wire format the worker speaks (browser PCM, FreeSWITCH fork, Vaani)
through a far-end model that PLAYS what the socket received: the browser
model mirrors ``PcmPlaybackQueue`` in src/services/voiceClient.ts, the
telephony model is a sequential line that ``killAudio``/``clear`` flushes.
"""

import asyncio
import base64
import importlib.util
import json
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from pipecat.frames.frames import (
    EndFrame, InterruptionFrame, OutputAudioRawFrame, OutputTransportMessageFrame,
    TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from shared.audio.ambience_presets import (
    AMBIENCE_PRESETS, DEFAULT_AMBIENCE_PRESET, ambience_volume_db, resolve_ambience_preset,
    resolve_ambience_volume,
)
from shared.orchestration.naturalness import (
    HUMAN_SPEECH_DEFAULTS, resolve_human_speech, validate_human_speech,
)
from voice_runtime import ambience as amb
from voice_runtime.filler_transport import FillerWebsocketOutputTransport
from voice_runtime.frames import AUDIO_FLUSH_MESSAGE_TYPE, FillerAudioOwner, FillerAudioRawFrame
from voice_runtime.serializer import RawPCMSerializer
from voice_runtime.telephony import FreeSwitchAudioForkSerializer, VaaniFrameSerializer

REPO = Path(__file__).resolve().parents[2]
SPEECH = 12000          # reply samples in these tests: far above any room sample
ROOM_MAX = 2000         # the -54 dBFS loop peaks near -30 dBFS (~1000)


# ── asset / mixer ────────────────────────────────────────────────────────

PRESET_IDS = list(AMBIENCE_PRESETS)
RATES = [8000, 16000, 22050, 24000]


def _gen():
    """scripts/generate_ambience_asset.py as a module (its loudness measure)."""
    spec = importlib.util.spec_from_file_location(
        "generate_ambience_asset", REPO / "scripts" / "generate_ambience_asset.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestPresetAssets:
    def test_registry_matches_the_shipped_files(self):
        assert PRESET_IDS == ["office", "call_center", "light_office", "busy_office", "room_tone"]
        assert DEFAULT_AMBIENCE_PRESET == "office"
        for preset in AMBIENCE_PRESETS.values():
            files = sorted(amb.ASSET_DIR.glob(f"{preset.asset}_*.wav"))
            assert files, preset.id
            for path in files:
                with wave.open(str(path), "rb") as wav:
                    rate = int(path.stem.rsplit("_", 1)[1])
                    assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, rate)
                    assert wav.getcomptype() == "NONE"
                    assert wav.getnframes() / rate >= 10
        # office keeps its three baseline rates; the others one canonical file
        assert len(list(amb.ASSET_DIR.glob("office_ambience_*.wav"))) == 3
        for pid in PRESET_IDS[1:]:
            assert len(list(amb.ASSET_DIR.glob(f"{AMBIENCE_PRESETS[pid].asset}_*.wav"))) == 1

    @pytest.mark.parametrize("rate", RATES)
    @pytest.mark.parametrize("preset", PRESET_IDS)
    def test_every_preset_loads_at_every_rate_at_its_level(self, preset, rate):
        bed = amb.load_ambience_bed(rate, preset=preset)
        spec = AMBIENCE_PRESETS[preset]
        assert bed.sample_rate == rate and bed.samples.dtype == np.int16 and bed.preset == preset
        assert bed.loop_seconds >= 10
        expected = amb.SPEECH_REFERENCE_DBFS + amb.AMBIENCE_LEVEL_DB + spec.trim_db
        assert abs(bed.rms_dbfs - expected) < 0.2, (bed.rms_dbfs, expected)
        assert np.abs(bed.samples.astype(np.int32)).max() < ROOM_MAX

    @pytest.mark.parametrize("rate", [8000, 24000])
    @pytest.mark.parametrize("preset", PRESET_IDS)
    def test_every_preset_loops_without_a_click(self, preset, rate):
        loop = amb.load_ambience_bed(rate, preset=preset).samples.astype(np.int32)
        steps = np.abs(np.diff(loop))
        seam = abs(int(loop[0]) - int(loop[-1]))
        assert seam <= np.percentile(steps, 99.5), (seam, np.percentile(steps, 99.5))

    def test_presets_are_as_loud_as_office_unless_meant_to_be_lighter(self):
        gen = _gen()
        loud = {}
        for pid in PRESET_IDS:
            x = amb.load_ambience_bed(8000, preset=pid).samples.astype(np.float64) / 32768
            loud[pid] = gen.phone_loudness(x, 8000) + 20 * np.log10(np.sqrt(np.mean(x ** 2)))
        for pid in PRESET_IDS:
            offset = AMBIENCE_PRESETS[pid].character_offset_db
            assert abs(loud[pid] - loud["office"] - offset) < 0.5, (pid, loud)

    def test_presets_have_different_acoustic_character(self):
        """Not level variants of one recording: voices, events and spectrum
        differ by design."""
        def features(pid):
            x = amb.load_ambience_bed(16000, preset=pid).samples.astype(np.float64)
            spec = np.abs(np.fft.rfft(x)) ** 2
            f = np.fft.rfftfreq(len(x), 1 / 16000)
            centroid = float(np.sum(f * spec) / np.sum(spec))
            w = 1600   # 100 ms level windows
            r100 = 20 * np.log10(np.sqrt(np.mean(x[: len(x) // w * w].reshape(-1, w) ** 2, axis=1)) + 1e-9)
            spread = float(np.percentile(r100, 95) - np.percentile(r100, 5))
            return centroid, spread
        feats = {pid: features(pid) for pid in PRESET_IDS}
        # room tone: steady (no voices, no events); every other preset moves
        assert feats["room_tone"][1] < 2.0
        assert min(v[1] for k, v in feats.items() if k != "room_tone") > 3.0
        # call center: voice-band murmur, darker than the typing offices
        assert feats["call_center"][0] < feats["office"][0] - 200
        assert feats["call_center"][0] < feats["busy_office"][0] - 200
        # busy office: livelier than the light office
        assert feats["busy_office"][1] > feats["light_office"][1]
        # no two presets are the same audio at a different level
        beds = {pid: amb.load_ambience_bed(16000, preset=pid).samples.astype(np.float64) for pid in PRESET_IDS}
        for a in PRESET_IDS:
            for b in PRESET_IDS:
                if a < b:
                    n = min(len(beds[a]), len(beds[b]))
                    corr = np.corrcoef(beds[a][:n], beds[b][:n])[0, 1]
                    assert abs(corr) < 0.2, (a, b, corr)

    def test_office_asset_is_still_the_accepted_baseline(self):
        gen = _gen()
        master = gen.render_preset("office")
        pcm = np.clip(np.rint(master * 32767.0), -32768, 32767).astype("<i2").tobytes()
        with wave.open(str(amb.ASSET_DIR / "office_ambience_24000.wav"), "rb") as wav:
            assert wav.readframes(wav.getnframes()) == pcm

    def test_closing_a_hard_edged_recording_removes_the_seam_step(self):
        rng = np.random.default_rng(1)
        raw = rng.standard_normal(16000) * 500
        raw[:200] += 20000    # a recording that starts on a loud edge
        closed = amb._close_loop(raw, 1200)
        assert abs(closed[0] - closed[-1]) < 3000
        assert len(closed) == 16000 - 1200


class TestCache:
    def test_decoded_once_and_rescaled_per_level(self):
        a = amb.load_ambience_bed(8000, preset="call_center")
        assert amb.load_ambience_bed(8000, preset="call_center") is a
        sources = len(amb._SOURCE_CACHE)
        louder = amb.load_ambience_bed(8000, preset="call_center", level_db=-30.0)
        assert louder is not a and len(amb._SOURCE_CACHE) == sources   # no second decode
        assert louder.rms_dbfs - a.rms_dbfs == pytest.approx(6.0, abs=0.05)

    def test_level_cache_is_bounded(self):
        for step in range(amb._BED_CACHE_SIZE + 6):
            amb.load_ambience_bed(8000, preset="room_tone", level_db=-40.0 + step * 0.5)
        assert len(amb._BED_CACHE) <= amb._BED_CACHE_SIZE

    def test_unknown_preset_loads_office(self):
        assert amb.load_ambience_bed(8000, preset="jungle").preset == "office"


class TestVolumeScale:
    def test_mapping(self):
        assert ambience_volume_db(0) is None
        assert ambience_volume_db(1) == pytest.approx(-45.0)
        assert ambience_volume_db(25) == pytest.approx(-40.59, abs=0.01)
        assert ambience_volume_db(50) == pytest.approx(-36.0) == amb.AMBIENCE_LEVEL_DB
        assert ambience_volume_db(75) == pytest.approx(-30.0)
        assert ambience_volume_db(100) == pytest.approx(-24.0)
        levels = [ambience_volume_db(v) for v in range(1, 101)]
        assert all(b > a for a, b in zip(levels, levels[1:]))       # strictly louder
        assert max(b - a for a, b in zip(levels, levels[1:])) < 0.25  # no jumps

    @pytest.mark.parametrize("value, expected", [
        (None, 50), (50, 50), (0, 0), (100, 100), (-5, 0), (150, 100), (37.9, 37),
        ("70", 70), ("abc", 50), (True, 50), (float("nan"), 50), (float("inf"), 50), ([], 50),
    ])
    def test_malformed_values_are_clamped_or_defaulted(self, value, expected):
        assert resolve_ambience_volume(value) == expected

    def test_unknown_preset_falls_back_to_office(self):
        for value in (None, "", "jungle", 3, "OFFICE"):
            assert resolve_ambience_preset(value).id == "office"
        assert resolve_ambience_preset("room_tone").id == "room_tone"


class TestMixer:
    def mixer(self, rate=8000, preset="office", level_db=None, **kwargs):
        kwargs.setdefault("start_offset", 0)
        kwargs.setdefault("fade_in_ms", 0)
        bed = amb.load_ambience_bed(rate, preset=preset, level_db=amb.AMBIENCE_LEVEL_DB if level_db is None else level_db)
        return amb.AmbienceMixer(bed, lead_s=0.02, **kwargs)

    def test_silence_mixed_is_exactly_the_loop_across_the_wrap(self):
        m = self.mixer()
        loop = m.bed.samples
        n = 640
        chunks = [np.frombuffer(m.mix(b"\x00" * n), dtype="<i2") for _ in range(len(loop) // (n // 2) + 3)]
        played = np.concatenate(chunks)
        expected = np.take(loop, np.arange(len(played)), mode="wrap")
        assert np.array_equal(played, expected)

    def test_adds_the_room_to_normal_speech_unchanged(self):
        m = self.mixer()
        voice = (np.sin(np.arange(4000) / 9) * 9000).astype("<i2")
        out = np.frombuffer(m.mix(voice.tobytes()), dtype="<i2").astype(np.int32)
        room = np.take(m.bed.samples, np.arange(4000), mode="wrap").astype(np.int32)
        assert np.array_equal(out, voice.astype(np.int32) + room)
        assert m.stats()["guarded_chunks"] == 0

    @pytest.mark.parametrize("preset", PRESET_IDS)
    def test_never_clips_even_at_maximum_volume_under_full_scale_speech(self, preset):
        m = self.mixer(preset=preset, level_db=ambience_volume_db(100))
        loop = m.bed.samples.astype(np.int32)
        rng = np.random.default_rng(3)
        cursor = 0
        for _ in range(300):   # 12 s of 40 ms chunks
            voice = np.clip(rng.normal(0, 1, 320) * 40000, -32768, 32767).astype("<i2")
            voice[::7] = 32767
            voice[3::11] = -32768
            room = np.take(loop, np.arange(cursor, cursor + 320), mode="wrap").astype(np.float64)
            cursor = (cursor + 320) % len(loop)
            out = np.frombuffer(m.mix(voice.tobytes()), dtype="<i2").astype(np.int32)
            added = (out - voice.astype(np.int32)).astype(np.float64)
            # What was added is the room under ONE gain for the whole chunk
            # (lowered to fit), never a per-sample clip of speech + room.
            gain = float(added @ room / (room @ room)) if room.any() else 0.0
            assert 0.0 <= gain <= 1.0 + 1e-9
            assert np.allclose(added, np.trunc(room * gain), atol=1.01)
        stats = m.stats()
        assert stats["saturated_samples"] == 0 and stats["guarded_chunks"] > 0

    def test_fades_in_at_call_start(self):
        m = self.mixer(fade_in_ms=400)
        first = np.frombuffer(m.mix(b"\x00" * 320), dtype="<i2")   # 20 ms
        later = [np.frombuffer(m.mix(b"\x00" * 320), dtype="<i2") for _ in range(40)]
        assert np.abs(first.astype(np.int32)).mean() < np.abs(later[-1].astype(np.int32)).mean() / 4

    def test_idle_frames_carry_the_room_sound_under_one_owner(self):
        m = self.mixer()
        a, b = m.idle_frame(flush_pending=False), m.idle_frame(flush_pending=True)
        assert a.owner is b.owner is m.owner and not m.owner.cancelled
        assert len(a.audio) == 2 * m.chunk_samples == 320
        assert b.flush_pending and a.sample_rate == 8000

    def test_playout_clock(self):
        now = [100.0]
        m = self.mixer(start_after_s=1.0, clock=lambda: now[0])
        m.begin()
        assert m.idle_wait(100.0) == pytest.approx(1.0)       # not armed yet
        m.arm()
        assert m.idle_wait(100.0) == 0.0                      # nothing queued: due now
        # bot audio just finished: its next chunk gets a moment to arrive
        assert m.idle_wait(100.0, last_audio_at=100.0) == pytest.approx(amb.IDLE_CONFIRM_S)
        m.note_sent(0.2, 100.0)                               # 200 ms queued at the far end
        assert m.idle_wait(100.0) == pytest.approx(0.2 - 0.02)
        m.note_interruption(100.05)                           # far end flushed
        assert m.idle_wait(100.05) == 0.0
        m.stop()
        assert m.idle_wait(100.1) is None
        assert m.mix(b"\x01\x00") == b"\x01\x00"                # stopped: untouched

    def test_stats_event_once_on_stop(self):
        class Recorder:
            events = []

            def add_event(self, kind, **data):
                self.events.append((kind, data))

        rec = Recorder()
        m = self.mixer(preset="busy_office", recorder=rec, volume=50, level_db=-36.0)
        m.mix(b"\x00" * 640)
        m.stop("end")
        m.stop("end")
        assert [k for k, _ in rec.events] == ["background_ambience"]
        data = rec.events[0][1]
        assert data["mixed_chunks"] == 1 and data["stop_reason"] == "end"
        assert data["preset"] == "busy_office" and data["volume"] == 50
        assert data["level_db_rel_speech"] == -36.0 and data["guarded_chunks"] == 0


class TestSetting:
    def test_defaults_and_validation(self):
        assert HUMAN_SPEECH_DEFAULTS["background_ambience"] is False
        assert HUMAN_SPEECH_DEFAULTS["background_ambience_preset"] == "office"
        assert HUMAN_SPEECH_DEFAULTS["background_ambience_volume"] == 50
        assert resolve_human_speech()["background_ambience"] is False
        assert resolve_human_speech({"background_ambience": True})["background_ambience"] is True
        assert validate_human_speech({
            "background_ambience": True, "background_ambience_preset": "call_center",
            "background_ambience_volume": 0,
        }) == []
        assert validate_human_speech({"background_ambience_volume": 100}) == []
        for bad in ({"background_ambience": "yes"}, {"background_ambience_preset": "jungle"},
                    {"background_ambience_preset": 3}, {"background_ambience_volume": 101},
                    {"background_ambience_volume": -1}, {"background_ambience_volume": 12.5},
                    {"background_ambience_volume": "70"}, {"background_ambience_volume": True}):
            assert validate_human_speech(bad), bad

    def test_runtime_merge_is_lenient(self):
        merged = resolve_human_speech(
            {"background_ambience_preset": "busy_office", "background_ambience_volume": 30},   # tenant
            {"background_ambience_preset": "jungle", "background_ambience_volume": 250},      # junk bot row
        )
        assert merged["background_ambience_preset"] == "busy_office"   # junk ignored → tenant value
        assert merged["background_ambience_volume"] == 100             # clamped

    def test_enabled_only_with_the_switch_and_the_layer(self):
        assert not amb.ambience_enabled(None)
        assert not amb.ambience_enabled(dict(HUMAN_SPEECH_DEFAULTS))           # existing bots
        assert not amb.ambience_enabled({"enabled": True})                    # old cached config
        assert amb.ambience_enabled({"background_ambience": True})
        assert not amb.ambience_enabled({"enabled": False, "background_ambience": True})

    def test_build_ambience(self, tmp_path, monkeypatch):
        class Config:
            human_speech = dict(HUMAN_SPEECH_DEFAULTS)

        assert amb.build_ambience(Config(), transport_kind="browser", sample_rate=24000) is None
        Config.human_speech = {**HUMAN_SPEECH_DEFAULTS, "background_ambience": True}
        browser = amb.build_ambience(Config(), transport_kind="browser", sample_rate=24000)
        phone = amb.build_ambience(Config(), transport_kind="telephony", sample_rate=8000)
        assert browser.sample_rate == 24000 and browser.lead_s == 0.06
        assert phone.sample_rate == 8000 and phone.lead_s == 0.02
        assert phone.bed.preset == "office" and phone.volume == 50 and phone.level_db == -36.0

        Config.human_speech = {"background_ambience": True, "background_ambience_preset": "room_tone",
                               "background_ambience_volume": 100}
        loud = amb.build_ambience(Config(), transport_kind="telephony", sample_rate=8000)
        assert loud.bed.preset == "room_tone" and loud.level_db == pytest.approx(-24.0)
        assert loud.bed.rms_dbfs == pytest.approx(-18.0 - 24.0 + AMBIENCE_PRESETS["room_tone"].trim_db, abs=0.2)

        # Missing preset/volume (older rows): office at the original level.
        Config.human_speech = {"background_ambience": True}
        legacy = amb.build_ambience(Config(), transport_kind="telephony", sample_rate=8000)
        assert legacy.bed.preset == "office" and legacy.bed.rms_dbfs == pytest.approx(-54.0, abs=0.1)

        # Malformed values never break a call.
        Config.human_speech = {"background_ambience": True, "background_ambience_preset": {"x": 1},
                               "background_ambience_volume": "loud"}
        junk = amb.build_ambience(Config(), transport_kind="telephony", sample_rate=8000)
        assert junk.bed.preset == "office" and junk.volume == 50

        # Volume 0: muted means no room audio at all, exactly like off.
        Config.human_speech = {"background_ambience": True, "background_ambience_volume": 0}
        assert amb.build_ambience(Config(), transport_kind="telephony", sample_rate=8000) is None

        events = []

        class Recorder:
            def add_event(self, kind, **data):
                events.append(kind)

        # No usable asset → the call runs without ambience (and says why).
        Config.human_speech = {"background_ambience": True}
        monkeypatch.setattr(
            amb, "load_ambience_bed",
            lambda rate, **_: amb._asset_for(tmp_path, amb.ASSET_NAME, rate),
        )
        assert amb.build_ambience(Config(), transport_kind="telephony", sample_rate=8000, recorder=Recorder()) is None
        assert events == ["background_ambience_unavailable"]

    def test_ui_offers_exactly_the_registry_presets(self):
        source = (REPO / "src" / "components" / "HumanSpeechSettings.tsx").read_text()
        block = source.split("AMBIENCE_PRESET_OPTIONS")[1].split("];")[0]
        pairs = re.findall(r'value: "([a-z_]+)", label: "([^"]+)"', block)
        assert pairs == [(p.id, p.label) for p in AMBIENCE_PRESETS.values()]


# ── transport ────────────────────────────────────────────────────────────

class BrowserFarEnd:
    """The browser client's playback queue (voiceClient.ts PcmPlaybackQueue +
    VoiceClient.handleMessage), on the test's monotonic clock."""

    def __init__(self, rate):
        self.rate = rate
        self.lead = 0.04
        self.playhead = 0.0
        self.segments = []            # [start, end, owner, samples]
        self.cleared = set()
        self.owners = set()
        self.priority = False
        self.suppress = False

    def _active(self, now):
        return [s for s in self.segments if s[1] > now]

    def enqueue(self, pcm, owner, now):
        if owner and owner in self.cleared:
            return
        samples = np.frombuffer(pcm, dtype="<i2")
        if not samples.size:
            return
        if owner:
            self.owners.add(owner)
        if not self._active(now):
            self.priority = False
        lead = 0.0 if (owner is None and self.priority) else self.lead
        start = self.playhead if self.playhead > now else now + lead
        self.playhead = start + samples.size / self.rate
        self.segments.append([start, self.playhead, owner, samples])

    def clear_filler(self, owner, now):
        self.cleared.add(owner)
        self.owners.discard(owner)
        removed, boundary = False, now
        for seg in list(self.segments):
            if seg[2] != owner or seg[1] <= now:
                continue
            stop = min(seg[1], now + 0.002) if seg[0] <= now else now
            self._truncate(seg, stop)
            # The kept part ends on a whole sample (up to half a sample past
            # ``stop``); the next audio starts there, never inside it.
            boundary = max(boundary, seg[1] if len(seg[3]) else stop)
            removed = True
        if not removed:
            return
        self.playhead = max([boundary] + [s[1] for s in self._active(now) if s[2] != owner])
        self.priority = True

    def stop(self, now):
        self.cleared |= self.owners
        self.owners.clear()
        for seg in list(self.segments):
            if seg[1] > now:
                self._truncate(seg, max(now, seg[0]))
        self.playhead = 0.0
        self.priority = False

    def _truncate(self, seg, at):
        keep = max(0, int(round((at - seg[0]) * self.rate)))
        seg[3] = seg[3][:keep]
        seg[1] = seg[0] + keep / self.rate
        if keep == 0:
            self.segments.remove(seg)

    def receive(self, payload, now):
        if isinstance(payload, (bytes, bytearray)):
            if not self.suppress:
                self.enqueue(bytes(payload), None, now)
            return None
        msg = json.loads(payload)
        kind = msg.get("type")
        if kind == "filler_audio":
            self.enqueue(base64.b64decode(msg["audio"]), msg["owner"], now)
        elif kind == "filler_clear":
            self.clear_filler(msg["owner"], now)
        elif kind == "event" and msg.get("name") == "interruption":
            self.stop(now)
            self.suppress = True
        elif kind == "bot_text" or (kind == "event" and msg.get("name") == "bot_speaking_started"):
            self.suppress = False
        return kind


class LineFarEnd:
    """A telephony leg: plays packets back to back, flushed by a clear."""

    def __init__(self, rate=8000, kind="fork"):
        self.rate = rate
        self.kind = kind
        self.playhead = 0.0
        self.segments = []

    def receive(self, payload, now):
        msg = json.loads(payload)
        if self.kind == "fork":
            if msg.get("type") == "killAudio":
                return self.flush(now) or "kill"
            if msg.get("type") != "playAudio":
                return msg.get("type")
            pcm = base64.b64decode(msg["data"]["audioContent"])
        else:
            if msg.get("event") == "clear":
                return self.flush(now) or "clear"
            if msg.get("event") != "media":
                return msg.get("event")
            pcm = base64.b64decode(msg["media"]["payload"])
        samples = np.frombuffer(pcm, dtype="<i2")
        start = max(now, self.playhead)
        self.playhead = start + samples.size / self.rate
        self.segments.append([start, self.playhead, None, samples])
        return "audio"

    def flush(self, now):
        for seg in list(self.segments):
            if seg[1] > now:
                keep = max(0, int(round((now - seg[0]) * self.rate)))
                seg[3] = seg[3][:keep]
                seg[1] = seg[0] + keep / self.rate
                if keep == 0:
                    self.segments.remove(seg)
        self.playhead = now
        return None


class Wire:
    """Fake websocket client feeding a far-end model."""

    is_connected = True
    is_closing = False

    def __init__(self, far_end):
        self.far_end = far_end
        self.log = []                 # (t, kind, payload)

    async def setup(self, frame):
        pass

    async def disconnect(self):
        pass

    async def cleanup(self):
        pass

    async def send(self, payload):
        now = time.monotonic()
        self.log.append((now, self.far_end.receive(payload, now), payload))


class Downstream(FrameProcessor):
    """Stands where the call recorder sits (after transport.output())."""

    def __init__(self):
        super().__init__()
        self.audio = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame):
            self.audio.append((type(frame).__name__, bytes(frame.audio)))
        await self.push_frame(frame, direction)


class Passthrough(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class EchoSpy:
    def __init__(self):
        self.pcm = bytearray()

    def add_output(self, pcm, sample_rate, at=None):
        self.pcm += pcm


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class Call:
    def __init__(self, kind, *, ambience=True, end_silence=0, preset="office", level_db=None):
        self.kind = kind
        self.rate = 24000 if kind == "browser" else 8000
        if kind == "browser":
            serializer, far_end = RawPCMSerializer(), BrowserFarEnd(self.rate)
        elif kind == "fork":
            serializer, far_end = FreeSwitchAudioForkSerializer(), LineFarEnd(8000, "fork")
        else:
            serializer, far_end = VaaniFrameSerializer(stream_sid="MZtest"), LineFarEnd(8000, "vaani")
        self.far_end = far_end
        self.wire = Wire(far_end)
        self.output = FillerWebsocketOutputTransport(self.wire, self.wire, FastAPIWebsocketParams(
            audio_out_enabled=True, audio_out_sample_rate=self.rate, audio_out_10ms_chunks=4,
            audio_out_end_silence_secs=end_silence, serializer=serializer,
        ))
        self.echo = EchoSpy()
        self.output.attach_echo_reference(self.echo)
        self.mixer = None
        if ambience:
            self.mixer = amb.AmbienceMixer(
                amb.load_ambience_bed(
                    self.rate, preset=preset,
                    level_db=amb.AMBIENCE_LEVEL_DB if level_db is None else level_db,
                ),
                lead_s=amb.LEAD_MS["browser" if kind == "browser" else "telephony"] / 1000,
                transport_kind=kind, start_after_s=0.0, fade_in_ms=0, start_offset=0,
            )
            self.output.attach_ambience(self.mixer)
        self.downstream = Downstream()
        self.worker = PipelineWorker(
            Pipeline([Passthrough(), self.output, self.downstream]),
            params=PipelineParams(audio_in_sample_rate=self.rate, audio_out_sample_rate=self.rate),
            enable_rtvi=False, idle_timeout_secs=None,
        )

    async def __aenter__(self):
        self._runner = asyncio.create_task(WorkerRunner(handle_sigint=False).run(self.worker))
        await until(lambda: bool(self.output._media_senders))
        return self

    async def __aexit__(self, *exc):
        if not self._runner.done():
            await self.end()

    async def end(self):
        await self.worker.queue_frame(EndFrame())
        try:
            await asyncio.wait_for(self._runner, timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            self._runner.cancel()

    def speech_frame(self, ms):
        t = np.arange(int(self.rate * ms / 1000))
        samples = (SPEECH * np.sign(np.sin(2 * np.pi * 440 * t / self.rate))).astype("<i2")
        samples[samples == 0] = SPEECH
        return TTSAudioRawFrame(audio=samples.tobytes(), sample_rate=self.rate, num_channels=1)

    async def reply(self, ms=400, frame_ms=100):
        queued_at = time.monotonic()
        await self.worker.queue_frame(TTSStartedFrame())
        for _ in range(ms // frame_ms):
            await self.worker.queue_frame(self.speech_frame(frame_ms))
        await self.worker.queue_frame(TTSStoppedFrame())
        return queued_at

    # far-end analysis
    def timeline(self):
        return sorted((s for s in self.far_end.segments if len(s[3])), key=lambda s: s[0])

    def first_speech_after(self, t):
        for start, _end, _owner, samples in self.timeline():
            hits = np.flatnonzero(np.abs(samples.astype(np.int32)) > SPEECH // 2)
            if hits.size and start + hits[0] / self.rate >= t - 1e-6:
                return start + hits[0] / self.rate
        return None

    def gaps(self, t0, t1):
        """Silent holes in the far end's playout between t0 and t1 (s)."""
        holes, cursor = [], t0
        for start, end, _owner, _samples in self.timeline():
            if end <= t0 or start >= t1:
                continue
            if start - cursor > 0.002:
                holes.append(round(start - cursor, 4))
            cursor = max(cursor, end)
        if t1 - cursor > 0.002:
            holes.append(round(t1 - cursor, 4))
        return holes

    def played(self, t0, t1):
        out = []
        for start, end, _owner, samples in self.timeline():
            a, b = max(t0, start), min(t1, end)
            if b > a:
                out.append(samples[int((a - start) * self.rate):int((b - start) * self.rate)])
        return np.concatenate(out) if out else np.zeros(0, dtype=np.int16)


def dbfs(samples):
    x = samples.astype(np.float64) / 32768
    return 20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-12)


KINDS = ["browser", "fork", "vaani"]


class TestTransportOff:
    @pytest.mark.parametrize("kind", KINDS)
    async def test_off_sends_only_the_reply(self, kind):
        async with Call(kind, ambience=False) as call:
            await asyncio.sleep(0.3)
            await call.reply(400)
            await asyncio.sleep(0.8)
        kinds = [k for _t, k, _p in call.wire.log]
        assert "filler_audio" not in kinds and "filler_clear" not in kinds
        played = call.played(0, time.monotonic() + 5)
        assert played.size and np.all(np.abs(played.astype(np.int32)) >= SPEECH - 1)


class TestTransportOn:
    @pytest.mark.parametrize("kind", KINDS)
    async def test_room_fills_the_silence_and_sits_under_the_reply(self, kind):
        async with Call(kind) as call:
            started = time.monotonic()
            await asyncio.sleep(0.6)
            queued = await call.reply(400)
            await until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(1.2)
            stopped = time.monotonic()
        onset = call.first_speech_after(queued)
        # Continuous room sound before the reply (after the first chunk) …
        assert call.gaps(started + 0.05, onset - 0.005) == []
        pre = call.played(started + 0.05, onset - 0.01)
        assert abs(dbfs(pre) - amb.AMBIENCE_TARGET_DBFS) < 3
        assert np.abs(pre.astype(np.int32)).max() < ROOM_MAX
        # … the reply carries it (speech + loop, never clean speech) …
        speech = call.played(onset, onset + 0.35)
        assert np.abs(speech.astype(np.int32)).min() > SPEECH - ROOM_MAX
        assert len(set(np.abs(speech.astype(np.int32)).tolist())) > 20
        # … and it continues after the reply until the call ends.
        tail_start = onset + 0.45
        assert call.gaps(tail_start, stopped - 0.1) == []
        post = call.played(tail_start + 0.05, stopped - 0.1)
        assert abs(dbfs(post) - amb.AMBIENCE_TARGET_DBFS) < 3
        stats = call.mixer.stats()
        assert stats["idle_chunks"] > 30 and stats["mixed_chunks"] >= 8
        assert stats["late_chunks"] == 0, stats

    @pytest.mark.parametrize("kind", KINDS)
    async def test_echo_reference_and_recorder_see_only_the_clean_reply(self, kind):
        async with Call(kind) as call:
            await asyncio.sleep(0.4)
            queued = await call.reply(400)
            await until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(0.8)
        clean = np.frombuffer(bytes(call.echo.pcm), dtype="<i2")
        assert clean.size == int(call.rate * 0.4)
        assert np.all(np.abs(clean.astype(np.int32)) == SPEECH)
        recorded = b"".join(pcm for _name, pcm in call.downstream.audio)
        assert recorded == bytes(call.echo.pcm)
        assert {name for name, _ in call.downstream.audio} == {"TTSAudioRawFrame"}

    async def test_browser_reply_starts_at_once_over_queued_room_audio(self):
        async with Call("browser") as call:
            await asyncio.sleep(0.5)
            queued = await call.reply(300)
            await until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(0.3)
        kinds = [k for _t, k, _p in call.wire.log]
        first_speech = next(i for i, (_t, k, _p) in enumerate(call.wire.log) if k is None)
        assert kinds[first_speech - 1] == "filler_clear"
        assert call.first_speech_after(queued) - queued < 0.03
        owners = {json.loads(p)["owner"] for _t, k, p in call.wire.log if k == "filler_audio"}
        assert len(owners) >= 2            # a new owner after the handoff clear

    @pytest.mark.parametrize("kind", ["fork", "vaani"])
    async def test_telephony_reply_packets_and_tail_are_unchanged(self, kind):
        """Room packets are their own 320-byte packets; the reply keeps the
        640/1280/2560 ramp, and its last partial packet is flushed before
        the room resumes (instead of waiting in the buffer)."""
        async with Call(kind) as call:
            await asyncio.sleep(0.5)
            queued = await call.reply(330, frame_ms=110)
            await until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(0.8)
        sizes = []
        for t, k, payload in call.wire.log:
            if k != "audio":
                continue
            msg = json.loads(payload)
            pcm = base64.b64decode(msg["data"]["audioContent"] if kind == "fork" else msg["media"]["payload"])
            speech = bool(np.abs(np.frombuffer(pcm, dtype="<i2").astype(np.int32)).max() > SPEECH // 2)
            sizes.append((len(pcm), speech))
        speech_packets = [n for n, s in sizes if s]
        assert speech_packets[:3] == [640, 1280, 2560]
        assert sum(speech_packets) == 330 * 16 - (330 * 16) % 640  # every full 40 ms chunk
        assert all(n == 320 for n, s in sizes if not s)
        assert call.mixer.stats()["remnant_flushes"] == 1
        # no room packet between the first and the last reply packet
        first = next(i for i, (_n, s) in enumerate(sizes) if s)
        last = max(i for i, (_n, s) in enumerate(sizes) if s)
        assert all(s for _n, s in sizes[first:last + 1])

    @pytest.mark.parametrize("kind", KINDS)
    async def test_barge_in_cuts_the_reply_and_the_room_carries_on(self, kind):
        async with Call(kind) as call:
            await asyncio.sleep(0.4)
            queued = await call.reply(1500)
            await until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(0.3)
            cut = time.monotonic()
            await call.worker.queue_frame(InterruptionFrame())
            await asyncio.sleep(0.8)
            stopped = time.monotonic()
        after = call.played(cut + 0.1, stopped - 0.05)
        assert after.size and np.abs(after.astype(np.int32)).max() < ROOM_MAX   # no reply audio
        assert call.gaps(cut + 0.1, stopped - 0.05) == []
        assert call.mixer.stats()["interruptions"] == 1

    @pytest.mark.parametrize("kind", ["browser", "fork"])
    async def test_provisional_pause_holds_the_reply_not_the_room(self, kind):
        async with Call(kind) as call:
            await asyncio.sleep(0.3)
            queued = await call.reply(1200)
            await until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(0.2)
            call.output.pause_playback()
            paused = time.monotonic()
            await asyncio.sleep(0.6)
            call.output.resume_playback()
            resumed = time.monotonic()
            await asyncio.sleep(1.6)
        during = call.played(paused + 0.3, resumed)
        assert during.size and np.abs(during.astype(np.int32)).max() < ROOM_MAX
        assert call.gaps(paused + 0.3, resumed) == []
        assert call.first_speech_after(resumed) is not None      # the reply continues

    @pytest.mark.parametrize("kind", KINDS)
    async def test_room_stops_with_the_call(self, kind):
        async with Call(kind, end_silence=1) as call:
            await asyncio.sleep(0.4)
            await call.reply(200)
            await asyncio.sleep(0.4)
            await call.end()
            ended = time.monotonic()
        assert not call.mixer.running
        late = [(t, k) for t, k, _p in call.wire.log if t > ended + 0.01 and k in ("filler_audio", "audio")]
        assert late == []
        tail = call.played(ended - 0.001, ended + 5)
        assert not tail.size or np.abs(tail.astype(np.int32)).max() < ROOM_MAX

    @pytest.mark.parametrize("kind", ["browser", "fork"])
    @pytest.mark.parametrize("preset", ["call_center", "busy_office", "room_tone"])
    async def test_presets_at_full_volume_on_the_wire(self, kind, preset):
        """A non-default room at volume 100: its level in the pauses, no
        playout holes, and a full-scale reply on top never clips."""
        level = ambience_volume_db(100)
        async with Call(kind, preset=preset, level_db=level) as call:
            started = time.monotonic()
            await asyncio.sleep(0.6)
            queued = time.monotonic()
            await call.worker.queue_frame(TTSStartedFrame())
            loud = np.full(int(call.rate * 0.3), 32767, dtype="<i2")
            loud[::2] = -32768
            await call.worker.queue_frame(TTSAudioRawFrame(audio=loud.tobytes(), sample_rate=call.rate, num_channels=1))
            await call.worker.queue_frame(TTSStoppedFrame())
            await until(lambda: call.first_speech_after(queued) is not None)
            await asyncio.sleep(0.9)
            stopped = time.monotonic()
        pre = call.played(started + 0.05, queued - 0.01)
        expected = amb.SPEECH_REFERENCE_DBFS + level + AMBIENCE_PRESETS[preset].trim_db
        assert abs(dbfs(pre) - expected) < 3, (dbfs(pre), expected)
        assert call.gaps(started + 0.05, queued - 0.01) == []
        onset = call.first_speech_after(queued)
        assert call.gaps(onset + 0.4, stopped - 0.1) == []
        stats = call.mixer.stats()
        assert stats["saturated_samples"] == 0 and stats["guarded_chunks"] > 0
        assert stats["late_chunks"] == 0

    @pytest.mark.parametrize("kind", ["browser", "fork"])
    async def test_queued_reply_wins_over_a_due_room_chunk(self, kind):
        """Regression (E2E 2026-09-28): a room chunk falling due at the very
        moment reply frames were queued made the sender call
        ``wait_for(get(), timeout=0)``, which cancels the get before it runs
        — it retried forever, the reply and the room both went silent until
        the next interruption. Force that coincidence on every frame."""
        async with Call(kind) as call:
            await asyncio.sleep(0.3)
            sender = call.output._media_senders[None]
            due_now = call.mixer.idle_wait
            call.mixer.idle_wait = lambda now, last_audio_at=0.0: (
                0.0 if not sender._audio_queue.empty() else due_now(now, last_audio_at=last_audio_at)
            )
            queued = await call.reply(400)
            await until(lambda: call.first_speech_after(queued) is not None, timeout=3)
            await asyncio.sleep(0.8)
            stopped = time.monotonic()
        onset = call.first_speech_after(queued)
        reply = call.played(onset, onset + 0.6)
        assert np.count_nonzero(np.abs(reply.astype(np.int32)) > SPEECH // 2) >= call.rate * 0.4 * 0.95
        assert call.gaps(onset + 0.5, stopped - 0.1) == []     # the room carries on after it

    async def test_latency_filler_clip_is_not_interleaved(self):
        async with Call("fork") as call:
            await asyncio.sleep(0.4)
            owner = FillerAudioOwner(turn_id=1)
            clip = np.full(int(8000 * 0.02), SPEECH, dtype="<i2").tobytes()
            for _ in range(15):   # 300 ms breath, streamed ahead like LatencyFillerProcessor
                await call.worker.queue_frame(FillerAudioRawFrame(
                    audio=clip, sample_rate=8000, num_channels=1, owner=owner,
                ))
            # The processor's completion marker on telephony (latency_filler.py).
            await call.worker.queue_frame(OutputTransportMessageFrame(message={
                "type": AUDIO_FLUSH_MESSAGE_TYPE, "filler_owner": owner.token,
            }))
            await asyncio.sleep(0.8)
            stopped = time.monotonic()
        # The room resumes the instant the clip completes: no hole after it.
        # Without the marker the room waits out IDLE_CONFIRM_S and this burst
        # leaves a 17–19 ms hole; with it, only real-clock wakeup jitter
        # (0–2.5 ms on a loaded run) — hence 6 ms, not the helper's 2 ms.
        speech = [s for s in call.timeline() if np.abs(s[3].astype(np.int32)).max() > SPEECH // 2]
        clip_end = max(s[1] for s in speech)
        assert all(hole < 0.006 for hole in call.gaps(clip_end - 0.005, stopped - 0.1))
        assert call.mixer.stats()["late_ms"] < 6.0
        flags = []
        for _t, k, payload in call.wire.log:
            if k == "audio":
                pcm = np.frombuffer(base64.b64decode(json.loads(payload)["data"]["audioContent"]), dtype="<i2")
                flags.append(bool(np.abs(pcm.astype(np.int32)).max() > SPEECH // 2))
        first, last = flags.index(True), len(flags) - 1 - flags[::-1].index(True)
        assert flags[first:last + 1] == [True] * 15


class TestLatency:
    """Reply onset (first reply sample played at the far end) with the room
    on vs off. Browser: queued room audio is cleared, the reply starts at
    once. Telephony: at most one queued room chunk + lead plays first."""

    @pytest.mark.parametrize("kind", KINDS)
    async def test_onset_delay(self, kind):
        results = {}
        for ambience in (False, True):
            onsets = []
            for _ in range(3):
                async with Call(kind, ambience=ambience) as call:
                    await asyncio.sleep(0.45 + 0.013 * len(onsets))
                    queued = await call.reply(200)
                    await until(lambda: call.first_speech_after(queued) is not None)
                    onsets.append(call.first_speech_after(queued) - queued)
            results[ambience] = float(np.median(onsets))
        added_ms = (results[True] - results[False]) * 1000
        print(f"{kind}: onset off {results[False]*1000:.1f} ms, on {results[True]*1000:.1f} ms, added {added_ms:.1f} ms")
        budget = 5.0 if kind == "browser" else 45.0
        assert added_ms <= budget
