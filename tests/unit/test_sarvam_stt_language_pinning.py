"""Sarvam REST STT must pin the recognizer for locale-form languages.

Live calls vs_o9Th_dw7qZgJjimwMOhdYtdx / vs_8XwziCc2eJEMYitByYfMnPpX
(2026-09-16/17): every unsupported-language rescue re-transcription ended in
``retranscribe_failed reason=gate:unsupported_script`` because the brain hands
the provider the conversation LOCALE ("hi-IN") while the code→wire table was
keyed by short codes ("hi"); the miss became ``language_code="unknown"`` —
auto-detect — and Sarvam answered with the same misdetected script again.
"""

import asyncio
from types import SimpleNamespace

import pytest

from shared.providers.base import ProviderConfig
from shared.providers.stt import sarvam as sarvam_mod
from shared.providers.stt.sarvam import SarvamSTT, sarvam_language_code


class _FakeSpeechToText:
    def __init__(self):
        self.calls = []

    async def transcribe(self, *, file, model, language_code):
        self.calls.append({"model": model, "language_code": language_code})
        return SimpleNamespace(transcript="हाँ जी हो रही है।", language_code=None,
                               language_probability=None)


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.speech_to_text = _FakeSpeechToText()


@pytest.fixture()
def provider(monkeypatch):
    import sarvamai

    monkeypatch.setattr(sarvamai, "AsyncSarvamAI", _FakeClient)
    monkeypatch.setattr(
        sarvam_mod, "get_settings",
        lambda: SimpleNamespace(resolve_secret=lambda ref: "test-key",
                                stt_api_key_reference="x"),
    )
    return SarvamSTT(ProviderConfig(provider="sarvam", model="saaras:v3",
                                    language="hi-IN", api_key_reference="x"))


class TestWireLanguageCode:
    @pytest.mark.parametrize("language,expected", [
        ("hi", "hi-IN"), ("hi-IN", "hi-IN"), ("HI-in", "hi-IN"), ("en-IN", "en-IN"),
        ("ml-IN", "ml-IN"),
        # Urdu is an enabled platform language the private table never knew:
        # it used to fall through to auto-detect, i.e. exactly the misdetection
        # the rescue path exists to correct.
        ("ur-IN", "ur-IN"), ("ur", "ur-IN"),
        # Odia now follows the module's canonical alias (or-IN → od-IN) instead
        # of the adapter's private spelling. Odia is not an enabled language.
        ("or", "od-IN"), ("od-IN", "od-IN"),
        ("", "unknown"), (None, "unknown"), ("auto", "unknown"), ("unknown", "unknown"),
        ("xx-YY", "unknown"),
    ])
    def test_locale_and_short_code_both_pin(self, language, expected):
        assert sarvam_language_code(language) == expected


class TestTranscribePinsTheRequest:
    def test_conversation_locale_pins_hindi(self, provider):
        result = asyncio.run(provider.transcribe(b"\x00\x01" * 8000, sample_rate=8000,
                                                 language="hi-IN"))
        call = provider._client.speech_to_text.calls[-1]
        assert call["language_code"] == "hi-IN"
        assert result.text == "हाँ जी हो रही है।"
        # A pinned response carries no detected language: the label falls
        # back to the requested base code, never to a locale-shaped string.
        assert result.language == "hi"

    def test_configured_locale_is_the_default_pin(self, provider):
        asyncio.run(provider.transcribe(b"\x00\x01" * 8000, sample_rate=8000))
        assert provider._client.speech_to_text.calls[-1]["language_code"] == "hi-IN"

    def test_auto_detect_stays_available(self, provider):
        asyncio.run(provider.transcribe(b"\x00\x01" * 8000, sample_rate=8000,
                                        language="unknown"))
        assert provider._client.speech_to_text.calls[-1]["language_code"] == "unknown"
