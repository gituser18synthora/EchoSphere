"""Dictionary selection reaches Sarvam without altering the synthesis text.

These tests use local fake clients only: no provider requests or dictionary
creation. The payload assertions cover both live streaming and REST previews
and cues, including the explicit per-language no-dictionary setting.
"""

import base64
import io
from types import SimpleNamespace
import wave

import pytest

from shared.providers.base import ProviderConfig
from shared.providers.tts.delivery import resolve_engine_params
from shared.providers.tts.sarvam import SarvamTTS
from shared.providers.tts.sarvam_ws import SarvamWebSocketTTSProvider
from shared.providers.tts.streaming import TTSStreamSettings


TEXT = "Haier Refrigerator के बारे में बताइए।"
MODELS = ("bulbul:v2", "bulbul:v3", "bulbul:v4-flash")


def _ws(model, params):
    return SarvamWebSocketTTSProvider(TTSStreamSettings(
        provider="sarvam", model=model, voice="shubh", language="hi-IN",
        sample_rate=16000, codec="linear16", api_key="unit-test-key-not-real",
        params=params,
    ))


def _wav_b64():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x01" * 160)
    return base64.b64encode(output.getvalue()).decode("ascii")


@pytest.fixture
def fake_sarvam(monkeypatch):
    """Replace the SDK before construction; accidental network use is impossible."""
    import sarvamai

    class FakeTTS:
        def __init__(self):
            self.requests = []

        async def convert(self, *, text, language_code, **kwargs):
            self.requests.append({"text": text, "language_code": language_code, **kwargs})
            return SimpleNamespace(audios=[_wav_b64()])

    class FakeClient:
        def __init__(self, **kwargs):
            self.text_to_speech = FakeTTS()

    monkeypatch.setenv("SARVAM_DICTIONARY_UNIT_TEST_KEY", "unit-test-key-not-real")
    monkeypatch.setattr(sarvamai, "AsyncSarvamAI", FakeClient)

    def build(model, params):
        return SarvamTTS(ProviderConfig(
            provider="sarvam", model=model, voice="shubh", language="hi-IN",
            api_key_reference="env:SARVAM_DICTIONARY_UNIT_TEST_KEY", extra=params,
        ))

    return build


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("dictionary_id", [None, "", "   ", 123, False])
def test_ws_omits_unconfigured_or_invalid_dictionary_id(model, dictionary_id):
    config = _ws(model, {"dict_id": dictionary_id})._build_config()
    assert "dict_id" not in config


@pytest.mark.parametrize("model", MODELS)
def test_ws_forwards_dictionary_only_on_supported_models(model):
    config = _ws(model, {"dict_id": "  haier-products  "})._build_config()
    if model == "bulbul:v2":
        assert "dict_id" not in config
    else:
        assert config["dict_id"] == "haier-products"


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("dictionary_id", [None, "", "   ", 123, False])
@pytest.mark.asyncio
async def test_rest_omits_unconfigured_or_invalid_dictionary_id(fake_sarvam, model, dictionary_id):
    provider = fake_sarvam(model, {"dict_id": dictionary_id})
    result = await provider.synthesize(TEXT)
    [request] = provider._client.text_to_speech.requests
    assert "dict_id" not in request
    assert request["text"] == TEXT
    assert result.audio


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.asyncio
async def test_rest_forwards_dictionary_only_on_supported_models(fake_sarvam, model):
    provider = fake_sarvam(model, {"dict_id": "  haier-products  "})
    await provider.synthesize(TEXT)
    [request] = provider._client.text_to_speech.requests
    if model == "bulbul:v2":
        assert "dict_id" not in request
    else:
        assert request["dict_id"] == "haier-products"
    assert request["text"] == TEXT
    assert request["language_code"] == "hi-IN"


@pytest.mark.parametrize("override, expected", [
    ({}, "haier-products"),
    ({"dict_id": None}, "haier-products"),
    ({"dict_id": "hindi-products"}, "hindi-products"),
    ({"dict_id": ""}, None),
])
@pytest.mark.asyncio
async def test_resolved_language_dictionary_matches_rest_and_ws(fake_sarvam, override, expected):
    engine = {"provider": "sarvam", "model": "bulbul:v3", "params": override}
    tts = {"provider": "sarvam", "model": "bulbul:v3", "settings": {"dict_id": "haier-products"}}
    params = resolve_engine_params(tts, engine)
    config = _ws("bulbul:v3", params)._build_config()
    provider = fake_sarvam("bulbul:v3", params)
    await provider.synthesize(TEXT)
    [request] = provider._client.text_to_speech.requests
    assert config.get("dict_id") == request.get("dict_id") == expected
    if expected is None:
        assert "dict_id" not in config and "dict_id" not in request
    assert request["text"] == TEXT


@pytest.mark.asyncio
async def test_no_dictionary_keeps_original_request_and_voice(fake_sarvam):
    provider = fake_sarvam("bulbul:v3", {"temperature": 0.35})
    await provider.synthesize(TEXT)
    [request] = provider._client.text_to_speech.requests
    assert "dict_id" not in request
    assert request["text"] == TEXT
    assert request["speaker"] == "shubh"
    assert request["model"] == "bulbul:v3"
    assert request["temperature"] == pytest.approx(0.35)
