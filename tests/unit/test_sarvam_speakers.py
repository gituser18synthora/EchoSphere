"""Sarvam bulbul speaker handling — normalization, defaults, no silent
replacement, error categorization, audio-payload robustness.

Regression suite for the bulbul:v2→v3 speaker mismatch: the provider used to
carry a stale v2 allowlist and silently rewrote any other speaker to "rohan",
which broke valid v3 speakers and masked configuration errors.
"""

import base64
import io
import wave

import pytest

from shared.providers.base import ProviderConfig, ProviderError
from shared.providers.tts.sarvam import SarvamTTS, _normalize_speaker
from shared.providers.tts.sarvam_ws import SarvamWebSocketTTSProvider
from shared.providers.tts.streaming import TRANSIENT_ERROR_CATEGORIES, TTSStreamSettings


def _tiny_wav_b64(rate: int = 16000, samples: int = 320) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00" * samples)
    return base64.b64encode(buf.getvalue()).decode()


class _SdkError(Exception):
    """Shape-compatible with sarvamai SDK errors (status_code + body)."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"headers: {{...}}, status_code: {status_code}")
        self.status_code = status_code
        self.body = {"error": {"message": message, "code": "invalid_request_error"}}


class _StubResponse:
    def __init__(self, audios):
        self.audios = audios


class _StubTTS:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def convert(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class _StubClient:
    def __init__(self, response=None, error=None):
        self.text_to_speech = _StubTTS(response=response, error=error)


@pytest.fixture
def make_provider(monkeypatch):
    monkeypatch.setenv("SARVAM_UNIT_TEST_KEY", "unit-test-key-not-real")

    def build(*, model="bulbul:v3", voice="", response=None, error=None) -> SarvamTTS:
        provider = SarvamTTS(ProviderConfig(
            provider="sarvam", model=model, voice=voice,
            api_key_reference="env:SARVAM_UNIT_TEST_KEY", timeout_seconds=5,
        ))
        if response is None and error is None:
            response = _StubResponse([_tiny_wav_b64()])
        provider._client = _StubClient(response=response, error=error)
        return provider

    return build


class TestSpeakerNormalization:
    def test_normalize_lowercases_and_strips(self):
        assert _normalize_speaker("  ADITYA  ") == "aditya"
        assert _normalize_speaker("Shubh") == "shubh"
        assert _normalize_speaker(None) == ""
        assert _normalize_speaker("   ") == ""

    @pytest.mark.asyncio
    async def test_valid_v3_speaker_sent_unchanged(self, make_provider):
        provider = make_provider(voice="aditya")
        result = await provider.synthesize("Hello there")
        call = provider._client.text_to_speech.calls[0]
        assert call["speaker"] == "aditya"
        assert call["model"] == "bulbul:v3"
        assert result.audio

    @pytest.mark.asyncio
    async def test_uppercase_and_padded_input_normalized(self, make_provider):
        provider = make_provider(voice="  SHUBH  ")
        await provider.synthesize("Hello there")
        assert provider._client.text_to_speech.calls[0]["speaker"] == "shubh"

    @pytest.mark.asyncio
    async def test_call_time_voice_overrides_config(self, make_provider):
        provider = make_provider(voice="aditya")
        await provider.synthesize("Hello there", voice=" Priya ")
        assert provider._client.text_to_speech.calls[0]["speaker"] == "priya"


class TestNoSilentReplacement:
    @pytest.mark.asyncio
    async def test_unknown_speaker_is_not_rewritten(self, make_provider):
        # Pre-fix behavior rewrote anything outside the stale v2 list to
        # "rohan". The provider must send exactly what was configured and let
        # the catalog/API reject it.
        provider = make_provider(voice="anushka")
        await provider.synthesize("Hello there")
        assert provider._client.text_to_speech.calls[0]["speaker"] == "anushka"

    @pytest.mark.asyncio
    async def test_missing_speaker_uses_model_default_and_logs(self, make_provider, caplog):
        provider = make_provider(voice="")
        with caplog.at_level("INFO"):
            await provider.synthesize("Hello there")
        assert provider._client.text_to_speech.calls[0]["speaker"] == "shubh"
        assert any("no speaker configured" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_v2_model_gets_v2_default(self, make_provider):
        provider = make_provider(model="bulbul:v2", voice="")
        await provider.synthesize("Hello there")
        assert provider._client.text_to_speech.calls[0]["speaker"] == "anushka"


class TestErrorCategorization:
    @pytest.mark.asyncio
    async def test_v2_speaker_on_v3_is_invalid_input_and_never_falls_back(self, make_provider):
        provider = make_provider(voice="anushka", error=_SdkError(
            400, "Speaker 'anushka' is not compatible with model bulbul:v3. "
                 "Available speakers for bulbul:v3 are: aditya, ritu, ..."))
        with pytest.raises(ProviderError) as excinfo:
            await provider.synthesize("Hello there")
        assert excinfo.value.category == "invalid_input"
        assert "not compatible with model bulbul:v3" in str(excinfo.value)
        # invalid configuration must surface, not trigger engine fallback
        assert excinfo.value.category not in TRANSIENT_ERROR_CATEGORIES

    @pytest.mark.asyncio
    async def test_unrecognized_speaker_is_invalid_input(self, make_provider):
        provider = make_provider(voice="niharika", error=_SdkError(
            400, "Speaker 'niharika' is not recognized."))
        with pytest.raises(ProviderError) as excinfo:
            await provider.synthesize("Hello there")
        assert excinfo.value.category == "invalid_input"
        assert "niharika" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_auth_failure_is_auth_and_leaks_no_key(self, make_provider):
        provider = make_provider(error=_SdkError(403, "invalid subscription key"))
        with pytest.raises(ProviderError) as excinfo:
            await provider.synthesize("Hello there")
        assert excinfo.value.category == "auth"
        assert "unit-test-key-not-real" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_rate_limit_is_transient(self, make_provider):
        provider = make_provider(error=_SdkError(429, "rate limit exceeded"))
        with pytest.raises(ProviderError) as excinfo:
            await provider.synthesize("Hello there")
        assert excinfo.value.category == "rate_limit"
        assert excinfo.value.category in TRANSIENT_ERROR_CATEGORIES


class TestAudioPayloadValidation:
    @pytest.mark.asyncio
    async def test_empty_audio_list_returns_empty_result_with_warning(self, make_provider, caplog):
        provider = make_provider(voice="shubh", response=_StubResponse([]))
        with caplog.at_level("WARNING"):
            result = await provider.synthesize("Hello there")
        assert result.audio == b""
        assert any("returned no audio" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_invalid_base64_raises_upstream(self, make_provider):
        provider = make_provider(voice="shubh", response=_StubResponse(["%%not-base64%%"]))
        with pytest.raises(ProviderError) as excinfo:
            await provider.synthesize("Hello there")
        assert excinfo.value.category == "upstream"

    @pytest.mark.asyncio
    async def test_non_wav_payload_raises_upstream(self, make_provider):
        garbage = base64.b64encode(b"this is not a RIFF wave container").decode()
        provider = make_provider(voice="shubh", response=_StubResponse([garbage]))
        with pytest.raises(ProviderError) as excinfo:
            await provider.synthesize("Hello there")
        assert excinfo.value.category == "upstream"
        assert "not a 16-bit PCM WAV" in str(excinfo.value)


class TestStreamingWsSpeaker:
    def _settings(self, **kw) -> TTSStreamSettings:
        base = dict(provider="sarvam", model="bulbul:v3", voice="shubh",
                    language="en-IN", sample_rate=16000, api_key="unit-test-key")
        base.update(kw)
        return TTSStreamSettings(**base)

    def test_ws_config_normalizes_case_and_spaces(self):
        provider = SarvamWebSocketTTSProvider(self._settings(voice="  ADITYA  "))
        assert provider._build_config()["speaker"] == "aditya"

    def test_ws_missing_speaker_defaults_by_model(self, caplog):
        with caplog.at_level("INFO"):
            v3 = SarvamWebSocketTTSProvider(self._settings(voice=""))._build_config()
            v2 = SarvamWebSocketTTSProvider(
                self._settings(voice="", model="bulbul:v2"))._build_config()
        assert v3["speaker"] == "shubh"
        assert v2["speaker"] == "anushka"
        assert any("no speaker configured" in r.message for r in caplog.records)

    def test_ws_unknown_speaker_not_rewritten(self):
        provider = SarvamWebSocketTTSProvider(self._settings(voice="anushka"))
        assert provider._build_config()["speaker"] == "anushka"
