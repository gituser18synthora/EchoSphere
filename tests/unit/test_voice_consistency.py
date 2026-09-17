"""Filler words must sound like the reply (2026-09-17 voice-consistency audit).

Measured on live telephony recordings: cues 6-10 dB quieter than the reply,
rendered with provider defaults (pace 1.0, default temperature) while the
reply streamed with the bot's settings, cached without those settings, and
downsampled 16 -> 8 kHz by linear interpolation (aliasing).
"""

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from pipecat.frames.frames import TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection

from shared.audio.pcm import resample_pcm
from shared.providers.base import ProviderConfig
from shared.providers.tts import sarvam as sarvam_tts
from shared.providers.tts.delivery import resolve_engine_params
from voice_runtime.audio_gate import frame_dbfs
from voice_runtime.voiced_cues import (
    CUE_RENDER_SAMPLE_RATE,
    VoicedCueLibrary,
    default_renderer,
    voice_params,
)


def _tone(freq, rate, ms=400, level=8000):
    n = int(rate * ms / 1000)
    return (level * np.sin(2 * np.pi * freq * np.arange(n) / rate)).astype("<i2").tobytes()


def _hf_share(pcm: bytes, rate: int, cut_hz: float) -> float:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    f = np.fft.rfftfreq(x.size, 1 / rate)
    return float(spec[f > cut_hz].sum() / spec.sum())


class TestAntiAliasedResampling:
    def test_length_contract_unchanged(self):
        assert len(resample_pcm(b"\x00\x01" * 16000, 16000, 8000)) == 16000
        assert len(resample_pcm(b"\x00\x01" * 8000, 8000, 24000)) == 48000
        assert resample_pcm(b"\x01\x02" * 100, 16000, 16000) == b"\x01\x02" * 100

    def test_content_above_the_new_nyquist_is_removed_not_folded(self):
        # A 6 kHz tone at 16 kHz lies above the 4 kHz Nyquist of an 8 kHz
        # stream: linear interpolation folds it to 2 kHz (an audible alias);
        # a proper resample removes it.
        pcm = _tone(6000, 16000)
        out = np.frombuffer(resample_pcm(pcm, 16000, 8000), dtype="<i2").astype(np.float64)
        source_rms = np.sqrt(np.mean(np.frombuffer(pcm, dtype="<i2").astype(np.float64) ** 2))
        assert np.sqrt(np.mean(out**2)) < 0.05 * source_rms

    def test_in_band_content_survives(self):
        pcm = _tone(1000, 16000)
        out = resample_pcm(pcm, 16000, 8000)
        x = np.frombuffer(out, dtype="<i2").astype(np.float64)
        assert abs(20 * np.log10(np.sqrt(np.mean(x[200:-200] ** 2)) / 8000 * np.sqrt(2))) < 0.5


class TestEngineParamsResolver:
    TTS = {
        "provider": "sarvam", "model": "bulbul:v3", "voice": "shubh",
        "settings": {"temperature": 0.01, "min_buffer_size": 50, "max_chunk_length": 150},
        "language_map": {
            "hi-IN": {"provider": "sarvam", "model": "bulbul:v3", "voice": "sunny",
                      "params": {"dict_id": "p_1"}},
            "en-IN": {"provider": "elevenlabs", "model": "eleven_flash_v2_5", "voice": "v1",
                      "params": {"stability": 0.4}},
        },
    }

    def test_default_engine_override_inherits_bot_settings_plus_speed(self):
        params = resolve_engine_params(self.TTS, self.TTS["language_map"]["hi-IN"], speed=1.1, energy=None)
        assert params["temperature"] == 0.01
        assert params["dict_id"] == "p_1"
        assert params["pace"] == pytest.approx(1.1)

    def test_other_provider_keeps_only_its_own_params(self):
        params = resolve_engine_params(self.TTS, self.TTS["language_map"]["en-IN"], speed=1.1, energy=None)
        assert "temperature" not in params and params["stability"] == 0.4
        assert params["speed"] == pytest.approx(1.1)

    def test_voice_params_drops_transport_plumbing(self):
        engine = {"params": {"temperature": 0.01, "min_buffer_size": 50, "pace": 1.1, "dict_id": None}}
        assert voice_params(engine) == {"temperature": 0.01, "pace": 1.1}


class _FakeTTS:
    def __init__(self):
        self.calls = []

    async def convert(self, **request):
        self.calls.append(request)
        rate = request["speech_sample_rate"]
        from shared.audio.pcm import pcm_to_wav_bytes
        import base64
        return SimpleNamespace(audios=[base64.b64encode(pcm_to_wav_bytes(_tone(300, rate), rate)).decode()])


class TestSarvamRestHonoursReplyParameters:
    @pytest.fixture()
    def provider(self, monkeypatch):
        import sarvamai

        class _Client:
            def __init__(self, *a, **k):
                self.text_to_speech = _FakeTTS()

        monkeypatch.setattr(sarvamai, "AsyncSarvamAI", _Client)
        monkeypatch.setattr(sarvam_tts, "get_settings", lambda: SimpleNamespace(
            resolve_secret=lambda ref: "k", tts_api_key_reference="x"))
        return sarvam_tts.SarvamTTS(ProviderConfig(
            provider="sarvam", model="bulbul:v3", voice="sunny", language="hi-IN",
            api_key_reference="x",
            extra={"temperature": 0.01, "pace": 1.2, "min_buffer_size": 50,
                   "output_sample_rate": 24000, "pitch": 0.2},
        ))

    def test_request_carries_temperature_speed_and_native_rate(self, provider):
        result = asyncio.run(provider.synthesize("Hmm…", voice="sunny", language="hi-IN", speed=1.1))
        req = provider._client.text_to_speech.calls[-1]
        assert req["temperature"] == 0.01
        assert req["pace"] == pytest.approx(1.1)          # canonical speed wins over stored pace
        assert req["speech_sample_rate"] == 24000
        assert "min_buffer_size" not in req and "pitch" not in req  # v2-only / plumbing dropped
        assert result.sample_rate == 24000 and result.audio

    def test_unsupported_rate_falls_back_to_16k(self, monkeypatch):
        import sarvamai

        class _Client:
            def __init__(self, *a, **k):
                self.text_to_speech = _FakeTTS()

        monkeypatch.setattr(sarvamai, "AsyncSarvamAI", _Client)
        monkeypatch.setattr(sarvam_tts, "get_settings", lambda: SimpleNamespace(
            resolve_secret=lambda ref: "k", tts_api_key_reference="x"))
        p = sarvam_tts.SarvamTTS(ProviderConfig(provider="sarvam", model="bulbul:v3",
                                                api_key_reference="x", extra={"output_sample_rate": 11025}))
        assert p.output_sample_rate == 16000


class TestCueCacheAndRenderer:
    async def test_key_changes_with_voice_params_and_renderer_gets_rate(self, tmp_path):
        seen = []

        async def render(engine, language, text, *, sample_rate):
            seen.append((voice_params(engine), sample_rate))
            return _tone(300, sample_rate), sample_rate

        lib = VoicedCueLibrary(tmp_path, renderer=render)
        plain = {"provider": "sarvam", "model": "bulbul:v3", "voice": "sunny"}
        tuned = {**plain, "params": {"temperature": 0.01, "pace": 1.1, "min_buffer_size": 50}}
        assert lib._key(plain, "hi-IN", "hmm") != lib._key(tuned, "hi-IN", "hmm")
        # Plumbing-only params do not fork the cache.
        assert lib._key({**plain, "params": {"min_buffer_size": 50}}, "hi-IN", "hmm") == lib._key(plain, "hi-IN", "hmm")
        lib.clip(tuned, "hi-IN", "hmm", 8000)
        await lib.wait_ready(tuned, "hi-IN")
        assert seen == [({"temperature": 0.01, "pace": 1.1}, CUE_RENDER_SAMPLE_RATE)]
        clip_8k = lib.clip(tuned, "hi-IN", "hmm", 8000)
        assert clip_8k and _hf_share(clip_8k, 8000, 3500) < 0.05  # resampled without aliasing

    async def test_legacy_three_argument_renderers_still_work(self, tmp_path):
        async def render(engine, language, text):
            return _tone(300, 16000), 16000

        lib = VoicedCueLibrary(tmp_path, renderer=render)
        engine = {"provider": "sarvam", "voice": "shubh"}
        lib.clip(engine, "hi-IN", "hmm", 16000)
        await lib.wait_ready(engine, "hi-IN")
        assert lib.clip(engine, "hi-IN", "hmm", 16000)

    async def test_default_renderer_passes_params_speed_and_rate_to_the_provider(self, monkeypatch):
        captured = {}

        class _Provider:
            def __init__(self, config):
                captured["config"] = config

            async def synthesize(self, text, *, voice=None, language=None, speed=1.0):
                captured["speed"] = speed
                return SimpleNamespace(audio=_tone(300, 24000), sample_rate=24000)

        import shared.providers.factory as factory
        monkeypatch.setattr(factory, "get_tts_provider", lambda config: _Provider(config))
        engine = {"provider": "sarvam", "model": "bulbul:v3", "voice": "sunny",
                  "params": {"temperature": 0.01, "pace": 1.2, "min_buffer_size": 50}}
        pcm, rate = await default_renderer(engine, "hi-IN", "Hmm…", sample_rate=24000)
        assert rate == 24000 and pcm
        assert captured["speed"] == pytest.approx(1.2)
        assert captured["config"].extra == {"temperature": 0.01, "output_sample_rate": 24000}


class TestFillerMatchesReplyLevel:
    async def _filler(self):
        from tests.unit.test_latency_filler import make_filler

        async def render(engine, language, text, *, sample_rate):
            return _tone(300, sample_rate, ms=300, level=1600), sample_rate  # about -29 dBFS

        cues = VoicedCueLibrary(renderer=render)
        filler = make_filler(delay_ms=60, cue_library=cues)
        engine = {"provider": "sarvam", "voice": "sunny"}
        cues.warm(engine, "hi-IN")
        await cues.wait_ready(engine, "hi-IN")
        return filler, engine

    async def test_cue_is_raised_to_just_under_a_loud_reply(self):
        filler, engine = await self._filler()
        armed = SimpleNamespace(engine=engine, language="hi-IN", cue_selection=None, acknowledgement=None)
        before = filler._rung_clip(armed, "hmm")
        assert frame_dbfs(before) == pytest.approx(-26.0, abs=1.0)  # rendered baseline, nothing heard yet
        assert filler._last_cue_level is None
        loud = TTSAudioRawFrame(audio=_tone(300, 16000, ms=400, level=9000), sample_rate=16000, num_channels=1)
        for _ in range(5):
            await filler.process_frame(loud, FrameDirection.DOWNSTREAM)
        after = filler._rung_clip(armed, "hmm")
        assert frame_dbfs(after) == pytest.approx(filler._reply_level_dbfs - 3.0, abs=0.8)
        assert filler._last_cue_level["reply_level_dbfs"] == pytest.approx(filler._reply_level_dbfs, abs=0.1)

    async def test_gain_is_bounded_and_peaks_capped(self):
        filler, engine = await self._filler()
        filler._reply_level_dbfs = -2.0  # absurdly loud reply
        armed = SimpleNamespace(engine=engine, language="hi-IN", cue_selection=None, acknowledgement=None)
        out = np.frombuffer(filler._rung_clip(armed, "hmm"), dtype="<i2")
        assert 20 * np.log10(np.abs(out).max() / 32768) <= -3.0 + 0.1
        assert frame_dbfs(out.tobytes()) <= -26.0 + 15.0 + 0.5

    async def test_stub_libraries_without_level_matching_keep_their_clips(self):
        from tests.unit.test_latency_filler import make_filler

        class _Cues:
            def clip(self, engine, language, kind, sample_rate, selection=None):
                return b"\x10\x27" * 1600

            def acknowledgement_clip(self, engine, language, text, sample_rate):
                return b"\x10\x27" * 1600

            def warm(self, *a, **k):
                pass

        filler = make_filler(delay_ms=60, cue_library=_Cues())
        filler._reply_level_dbfs = -10.0
        armed = SimpleNamespace(engine={}, language="hi-IN", cue_selection=None, acknowledgement=None)
        assert filler._rung_clip(armed, "hmm") == b"\x10\x27" * 1600
