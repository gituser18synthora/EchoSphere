"""Sarvam TTS ``loudness`` end to end.

Sarvam's TTS API reference: loudness 0.3-3.0, default 1.0, "only supported for
bulbul:v2. It is NOT supported for bulbul:v3 or bulbul:v4-flash" (the sarvamai
SDK's WebSocket config: "Will be ignored if provided"). So it is a bulbul:v2
setting: catalog schema range, sent on the handshake when set, omitted at the
API default (bots that never set it render exactly as before), never sent to
v3 / v4-flash, rejected there at save time, and recorded per reply.
"""

import asyncio

import pytest

import shared.providers.tts.sarvam_ws as sarvam_ws
from backend.core.provider_catalog import validate_params
from backend.seeds import provider_catalog_seed as catalog_seed
from shared.providers.tts.delivery import (
    SARVAM_LOUDNESS_DEFAULT,
    effective_loudness,
    omit_default_loudness,
    sarvam_accepts_loudness,
)
from shared.providers.tts.sarvam_ws import SarvamWebSocketTTSProvider
from shared.providers.tts.streaming import TTSStreamSettings
from tests.mock_tts_servers import MockSarvamTTSServer
from tests.unit.test_sarvam_rest_tts_language_kwarg import _NewSdkTTS, _provider
from tests.unit.test_streaming_tts_providers import sarvam_settings
from tests.unit.test_tts_router_flush_finals import FakeProvider, FakeRecorder, make_router
from voice_runtime.tts_router import _Generation

V2 = dict(model="bulbul:v2", voice="anushka", language="hi-IN")


@pytest.fixture(autouse=True)
def _no_warm_spare(monkeypatch):
    monkeypatch.setattr(sarvam_ws, "_WARM_SPARE", False)


async def _handshake(monkeypatch, **overrides) -> dict:
    async with MockSarvamTTSServer() as server:
        monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
        provider = SarvamWebSocketTTSProvider(sarvam_settings(**overrides))
        await provider.connect()
        await provider.close()
    return server.configs[0]


def _schema(model: str) -> dict:
    row = next(r for r in catalog_seed.PROVIDER_MODELS if r[:3] == ("sarvam", "tts", model))
    return row[8]


class TestModelSupport:
    def test_only_bulbul_v2_takes_loudness(self):
        assert sarvam_accepts_loudness("bulbul:v2")
        assert not sarvam_accepts_loudness("bulbul:v3")
        assert not sarvam_accepts_loudness("bulbul:v4-flash")
        assert SARVAM_LOUDNESS_DEFAULT == 1.0
        assert _schema("bulbul:v2")["loudness"]["default"] == SARVAM_LOUDNESS_DEFAULT
        assert "loudness" not in _schema("bulbul:v3")
        assert "loudness" not in _schema("bulbul:v4-flash")

    def test_effective_value(self):
        assert effective_loudness("sarvam", "bulbul:v2", {"loudness": 1.4}) == 1.4
        assert effective_loudness("sarvam", "bulbul:v2", {}) == 1.0
        assert effective_loudness("sarvam", "bulbul:v3", {"loudness": 1.4}) is None
        assert effective_loudness("elevenlabs", "eleven_flash_v2_5", {"loudness": 1.4}) is None

    def test_default_is_dropped_and_other_values_kept(self):
        assert omit_default_loudness({"loudness": 1, "pitch": 0.1}) == {"pitch": 0.1}
        assert omit_default_loudness({"loudness": 0.8}) == {"loudness": 0.8}


class TestHandshake:
    async def test_carries_loudness_when_set(self, monkeypatch):
        config = await _handshake(monkeypatch, **V2, params={"pace": 1.0, "loudness": 1.4})
        assert config["loudness"] == 1.4

    async def test_omits_loudness_at_the_api_default(self, monkeypatch):
        config = await _handshake(monkeypatch, **V2, params={"loudness": 1.0, "pitch": 0.1})
        assert "loudness" not in config
        assert config["pitch"] == 0.1

    async def test_omits_loudness_when_unset(self, monkeypatch):
        config = await _handshake(monkeypatch, **V2, params={"pace": 1.0})
        assert "loudness" not in config

    @pytest.mark.parametrize("model,voice", [("bulbul:v3", "shubh"), ("bulbul:v4-flash", "shubh")])
    async def test_never_sent_to_models_without_loudness(self, monkeypatch, model, voice):
        config = await _handshake(monkeypatch, model=model, voice=voice, language="hi-IN",
                                  params={"loudness": 1.4})
        assert "loudness" not in config


class TestRest:
    async def test_rest_sends_a_set_value_and_omits_the_default(self, monkeypatch):
        tts = _NewSdkTTS()
        provider = _provider(monkeypatch, tts, language="hi-IN")
        provider._model = "bulbul:v2"
        provider._params = {"loudness": 1.0}
        await provider.synthesize("Hello", voice="anushka", language="hi-IN")
        assert "loudness" not in tts.calls[-1]
        provider._params = {"loudness": 1.4}
        await provider.synthesize("Hello", voice="anushka", language="hi-IN")
        assert tts.calls[-1]["loudness"] == 1.4


class TestValidation:
    @pytest.mark.parametrize("value", [0.3, 1.0, 2.2, 3.0])
    def test_bulbul_v2_accepts_its_documented_range(self, value):
        assert validate_params(_schema("bulbul:v2"), {"loudness": value}, prefix="TTS") == []

    @pytest.mark.parametrize("value", [0.29, 3.01, -1, 10])
    def test_bulbul_v2_rejects_out_of_range(self, value):
        assert validate_params(_schema("bulbul:v2"), {"loudness": value}, prefix="TTS") == [
            "TTS: 'loudness' must be between 0.3 and 3.0."]

    def test_bulbul_v2_rejects_a_non_number(self):
        assert validate_params(_schema("bulbul:v2"), {"loudness": "loud"}, prefix="TTS") == [
            "TTS: 'loudness' must be a number."]

    @pytest.mark.parametrize("model", ["bulbul:v3", "bulbul:v4-flash"])
    def test_models_without_loudness_reject_it(self, model):
        assert validate_params(_schema(model), {"loudness": 1.2}, prefix="Voice mapping [hi-IN]") == [
            "Voice mapping [hi-IN]: unknown parameter 'loudness'."]


class TestReplyEvent:
    async def _event(self, *, model: str, params: dict) -> dict:
        recorder = FakeRecorder()
        router = make_router(recorder=recorder)
        provider = FakeProvider()
        provider._settings = TTSStreamSettings(provider="sarvam", model=model, voice="anushka",
                                               language="hi-IN", sample_rate=16000, params=params)
        router._generations["ctx1"] = _Generation(
            engine={"provider": "sarvam", "model": model, "voice": "anushka"}, provider=provider)
        await router._finalize_generation("ctx1", failed=False)
        await asyncio.gather(*router.tasks)
        return next(e for e in recorder.events if e["kind"] == "tts_provider_used")

    async def test_records_the_configured_loudness(self):
        event = await self._event(model="bulbul:v2", params={"loudness": 1.4})
        assert (event["model"], event["loudness"]) == ("bulbul:v2", 1.4)

    async def test_records_the_api_default_when_unset(self):
        event = await self._event(model="bulbul:v2", params={})
        assert event["loudness"] == 1.0

    async def test_records_none_for_a_model_without_loudness(self):
        event = await self._event(model="bulbul:v3", params={"temperature": 0.3})
        assert (event["model"], event["loudness"]) == ("bulbul:v3", None)
