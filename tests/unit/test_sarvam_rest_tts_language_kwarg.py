"""REST Sarvam TTS: the language keyword follows the installed sarvamai SDK.

sarvamai renamed ``convert(target_language_code=…)`` to ``language_code`` in
0.1.3x. The adapter used to hard-code the old name, so on 0.1.35 every REST
render (voiced latency cues, one-shot previews) died with
``TypeError: unexpected keyword argument 'target_language_code'`` before a
request was made. The adapter now reads the keyword from the SDK signature.
"""

import base64
import inspect
import io
import wave

import pytest

from shared.providers.base import ProviderConfig
from shared.providers.tts import sarvam as sarvam_tts
from shared.providers.tts.sarvam import SarvamTTS, _tts_language_kwarg


def _tiny_wav_b64() -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * 160)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class _Response:
    def __init__(self):
        self.audios = [_tiny_wav_b64()]


class _NewSdkTTS:
    """Shape of sarvamai >= 0.1.3x: ``language_code`` keyword."""

    def __init__(self):
        self.calls: list[dict] = []

    async def convert(self, *, text, language_code, speaker=None, model=None, **kwargs):
        self.calls.append({"text": text, "language_code": language_code,
                           "speaker": speaker, "model": model, **kwargs})
        return _Response()


class _LegacySdkTTS:
    """Shape of sarvamai <= 0.1.2x: ``target_language_code`` keyword."""

    def __init__(self):
        self.calls: list[dict] = []

    async def convert(self, *, text, target_language_code, speaker=None, model=None, **kwargs):
        self.calls.append({"text": text, "target_language_code": target_language_code,
                           "speaker": speaker, "model": model, **kwargs})
        return _Response()


class _OpaqueTTS:
    """A client whose signature says nothing (``**kwargs`` only)."""

    def __init__(self):
        self.calls: list[dict] = []

    async def convert(self, **kwargs):
        self.calls.append(kwargs)
        return _Response()


class _Client:
    def __init__(self, tts):
        self.text_to_speech = tts


def _provider(monkeypatch, tts, *, language: str = "en") -> SarvamTTS:
    monkeypatch.setenv("SARVAM_UNIT_TEST_KEY", "unit-test-key-not-real")
    provider = SarvamTTS(ProviderConfig(
        provider="sarvam", model="bulbul:v3", voice="shubh", language=language,
        api_key_reference="env:SARVAM_UNIT_TEST_KEY", timeout_seconds=5,
    ))
    provider._client = _Client(tts)
    return provider


class TestLanguageKeyword:
    def test_installed_sdk_uses_language_code(self):
        from sarvamai.text_to_speech.client import AsyncTextToSpeechClient

        params = inspect.signature(AsyncTextToSpeechClient.convert).parameters
        assert "language_code" in params
        assert "target_language_code" not in params
        assert _tts_language_kwarg(AsyncTextToSpeechClient.convert) == "language_code"

    def test_keyword_detection_per_signature(self):
        assert _tts_language_kwarg(_NewSdkTTS().convert) == "language_code"
        assert _tts_language_kwarg(_LegacySdkTTS().convert) == "target_language_code"
        assert _tts_language_kwarg(_OpaqueTTS().convert) == "language_code"
        assert _tts_language_kwarg(object()) == "language_code"  # unsignaturable

    @pytest.mark.asyncio
    async def test_new_sdk_receives_language_code(self, monkeypatch):
        provider = _provider(monkeypatch, _NewSdkTTS())
        result = await provider.synthesize("नमस्ते", language="hi-IN")
        [call] = provider._client.text_to_speech.calls
        assert call["language_code"] == "hi-IN"
        assert "target_language_code" not in call
        assert call["speaker"] == "shubh" and call["model"] == "bulbul:v3"
        assert result.audio

    @pytest.mark.asyncio
    async def test_legacy_sdk_still_receives_target_language_code(self, monkeypatch):
        provider = _provider(monkeypatch, _LegacySdkTTS())
        await provider.synthesize("Hello", language="en-IN")
        [call] = provider._client.text_to_speech.calls
        assert call["target_language_code"] == "en-IN"
        assert "language_code" not in call

    @pytest.mark.asyncio
    async def test_opaque_signature_defaults_to_the_current_name(self, monkeypatch):
        provider = _provider(monkeypatch, _OpaqueTTS())
        await provider.synthesize("Hello", language="en-IN")
        [call] = provider._client.text_to_speech.calls
        assert call["language_code"] == "en-IN"

    @pytest.mark.asyncio
    async def test_script_detection_still_feeds_the_language_keyword(self, monkeypatch):
        # ProviderConfig.language defaults to "en"; blank it so the script
        # detector (not the configured default) chooses the language.
        provider = _provider(monkeypatch, _NewSdkTTS(), language="")
        await provider.synthesize("ഹലോ")  # Malayalam script, no explicit language
        [call] = provider._client.text_to_speech.calls
        assert call["language_code"] == "ml-IN"

    def test_module_has_no_hardcoded_legacy_keyword_in_requests(self):
        source = inspect.getsource(sarvam_tts.SarvamTTS.synthesize)
        assert '"target_language_code":' not in source
