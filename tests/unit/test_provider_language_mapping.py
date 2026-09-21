"""Canonical EchoSphere locale → provider wire code, for OUR languages only.

Scope: the nine languages enabled in ``supported_languages`` (all Indian) —
en-IN, hi-IN, mr-IN, te-IN, ta-IN, ml-IN, pa-IN, gu-IN, ur-IN. This file
deliberately does not assert anything about the rest of either vendor's
catalog: that is what ``provider_models.languages`` in the DB is for.

Two vendor facts drive every case, verified 2026-09-21 against the live APIs:

- ElevenLabs takes a bare lowercase ISO 639-1 ``language_code`` and *rejects*
  anything the selected model cannot speak — HTTP 400
  ``{"status":"unsupported_language"}`` on REST, and on the multi-stream
  WebSocket a ``{"error":"unsupported_language","code":1008}`` frame with no
  audio at all (a mute bot). A locale form such as ``en-US`` is rejected too.
  Of our nine, Flash/Turbo v2.5 speaks only English, Hindi and Tamil.
- Sarvam STT transcribes all nine; Sarvam TTS cannot speak Urdu, which is the
  only enabled language whose STT and TTS support differ.
"""

import json

import httpx
import pytest

from shared.providers.base import ProviderConfig, ProviderError
from shared.providers.languages import (
    SARVAM_STT_SUPPORTED_LOCALES,
    SARVAM_SUPPORTED_LOCALES,
    elevenlabs_language_code,
    elevenlabs_models_speaking,
    elevenlabs_supports_language,
    elevenlabs_unsupported_language_message,
    sarvam_stt_language_code,
)
from shared.providers.tts.elevenlabs import ElevenLabsTTS
from shared.providers.tts.elevenlabs_ws import ElevenLabsWebSocketTTSProvider
from shared.providers.tts.streaming import TTSStreamSettings

#: The platform's enabled languages (supported_languages.enabled = 1).
ECHOSPHERE_LOCALES = [
    "en-IN", "hi-IN", "mr-IN", "te-IN", "ta-IN",
    "ml-IN", "pa-IN", "gu-IN", "ur-IN",
]

#: Of those, the ones ElevenLabs Flash/Turbo v2.5 can actually speak.
ELEVENLABS_V2_5_SPEAKS = {"en-IN": "en", "hi-IN": "hi", "ta-IN": "ta"}


class TestElevenLabsScopedMapping:
    @pytest.mark.parametrize("model", ["eleven_flash_v2_5", "eleven_turbo_v2_5"])
    @pytest.mark.parametrize("locale", ECHOSPHERE_LOCALES)
    def test_supported_languages_map_and_unsupported_ones_do_not(self, model, locale):
        assert elevenlabs_language_code(model, locale) == (
            ELEVENLABS_V2_5_SPEAKS.get(locale)
        )

    def test_no_locale_form_is_ever_produced(self):
        for locale in ECHOSPHERE_LOCALES:
            code = elevenlabs_language_code("eleven_flash_v2_5", locale)
            assert code is None or ("-" not in code and code == code.lower())

    def test_eleven_v3_takes_no_language_code(self):
        # v3 rejects the parameter outright; the model's language ability is
        # carried by the voice and the text, not by a wire code.
        for locale in ECHOSPHERE_LOCALES:
            assert elevenlabs_language_code("eleven_v3", locale) is None


class TestModelCapability:
    """Which model can actually speak each of our nine languages."""

    @pytest.mark.parametrize("locale", ECHOSPHERE_LOCALES)
    def test_v3_speaks_every_echosphere_language(self, locale):
        assert elevenlabs_supports_language("eleven_v3", locale) is True

    @pytest.mark.parametrize("locale", ECHOSPHERE_LOCALES)
    def test_v2_5_speaks_only_english_hindi_tamil(self, locale):
        assert elevenlabs_supports_language("eleven_flash_v2_5", locale) is (
            locale in ELEVENLABS_V2_5_SPEAKS
        )

    def test_unmodelled_combinations_are_not_rejections(self):
        # None must never be read as "unsupported": out-of-scope locales and
        # unknown models carry no capability data here.
        assert elevenlabs_supports_language("eleven_flash_v2_5", "en-US") is None
        assert elevenlabs_supports_language("some_future_model", "hi-IN") is None

    def test_alternatives_are_named_for_the_error_message(self):
        assert elevenlabs_models_speaking("ml-IN") == ["eleven_v3"]
        assert "eleven_flash_v2_5" in elevenlabs_models_speaking("hi-IN")

    def test_guidance_does_not_send_the_operator_into_a_dead_end(self):
        # eleven_v3 speaks Malayalam but has no realtime streaming, so a
        # per-language override to it is rejected by voice-settings
        # validation. The message must say where it CAN be selected rather
        # than just naming the model.
        message = elevenlabs_unsupported_language_message(
            "eleven_flash_v2_5", "ml-IN"
        )
        assert "eleven_v3" in message
        assert "DEFAULT TTS model" in message
        assert "no realtime streaming" in message

    def test_guidance_names_a_streaming_model_when_one_exists(self):
        message = elevenlabs_unsupported_language_message("eleven_v3", "hi-IN")
        assert "Use eleven_flash_v2_5 or eleven_turbo_v2_5" in message

    def test_short_form_and_casing(self):
        assert elevenlabs_language_code("eleven_flash_v2_5", "hi") == "hi"
        assert elevenlabs_language_code("eleven_flash_v2_5", "HI-in") == "hi"

    def test_blank_input_produces_no_code(self):
        for value in (None, ""):
            assert elevenlabs_language_code("eleven_flash_v2_5", value) is None

    def test_unknown_model_never_gets_a_code(self):
        assert elevenlabs_language_code("some_future_model", "hi-IN") is None


class TestSarvamScopedMapping:
    @pytest.mark.parametrize("locale", ECHOSPHERE_LOCALES)
    def test_stt_pins_every_enabled_language(self, locale):
        assert sarvam_stt_language_code(locale) == locale

    def test_urdu_is_stt_only(self):
        # The bug this fixes: Urdu used to fall through to auto-detect.
        assert sarvam_stt_language_code("ur-IN") == "ur-IN"
        assert "ur-IN" in SARVAM_STT_SUPPORTED_LOCALES
        # Sarvam TTS cannot speak it — the TTS locale set is untouched.
        assert "ur-IN" not in SARVAM_SUPPORTED_LOCALES

    def test_tts_locale_set_is_unchanged(self):
        # Every other enabled language is spoken by Sarvam TTS as before.
        for locale in ECHOSPHERE_LOCALES:
            assert (locale in SARVAM_SUPPORTED_LOCALES) == (locale != "ur-IN")

    @pytest.mark.parametrize("value", ["", None, "auto", "unknown", "fr-FR", "xx-YY"])
    def test_unpinnable_input_asks_for_auto_detect(self, value):
        assert sarvam_stt_language_code(value) == "unknown"


# ── adapter-level: what actually leaves the process ─────────────────────────

KEY_REF = "env:ELEVENLABS_LANG_TEST_KEY"


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_LANG_TEST_KEY", "sk-unit-test")


def _rest_adapter(model: str):
    adapter = ElevenLabsTTS(ProviderConfig(
        provider="elevenlabs", model=model, voice="voice-xyz",
        api_key_reference=KEY_REF,
    ))
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"\x00\x01" * 160)

    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return adapter, captured


def _ws_url(model: str, language: str) -> str:
    provider = ElevenLabsWebSocketTTSProvider(TTSStreamSettings(
        provider="elevenlabs", model=model, voice="voice-xyz",
        language=language, sample_rate=16000, codec="pcm", api_key="k",
    ))
    return provider._build_url()


class TestElevenLabsRestPayload:
    async def test_supported_language_sends_bare_iso(self):
        adapter, captured = _rest_adapter("eleven_flash_v2_5")
        await adapter.synthesize("hello", language="hi-IN")
        await adapter.aclose()
        assert json.loads(captured[0].content)["language_code"] == "hi"

    async def test_unsupported_language_is_refused_before_any_request(self):
        # Omitting the code is not a workaround — the model still cannot speak
        # Malayalam, so the combination is refused and the operator is told
        # which model can.
        adapter, captured = _rest_adapter("eleven_flash_v2_5")
        with pytest.raises(ProviderError) as excinfo:
            await adapter.synthesize("hello", language="ml-IN")
        await adapter.aclose()
        assert captured == []
        assert excinfo.value.category == "invalid_input"
        assert "eleven_v3" in str(excinfo.value)
        assert "DEFAULT TTS model" in str(excinfo.value)

    async def test_eleven_v3_accepts_malayalam(self):
        adapter, captured = _rest_adapter("eleven_v3")
        await adapter.synthesize("hello", language="ml-IN")
        await adapter.aclose()
        payload = json.loads(captured[0].content)
        assert payload["model_id"] == "eleven_v3"
        assert "language_code" not in payload  # v3 takes no language_code


class TestElevenLabsWebSocketUrl:
    def test_supported_language_is_enforced(self):
        assert "language_code=hi" in _ws_url("eleven_flash_v2_5", "hi-IN")

    def test_no_locale_reaches_the_url(self):
        for locale in ELEVENLABS_V2_5_SPEAKS:
            assert f"language_code={locale}" not in _ws_url(
                "eleven_flash_v2_5", locale
            )

    @pytest.mark.parametrize("locale", ["ml-IN", "mr-IN", "gu-IN", "te-IN",
                                       "pa-IN", "ur-IN"])
    async def test_unsupported_language_refuses_to_connect(self, locale):
        provider = ElevenLabsWebSocketTTSProvider(TTSStreamSettings(
            provider="elevenlabs", model="eleven_flash_v2_5", voice="voice-xyz",
            language=locale, sample_rate=16000, codec="pcm", api_key="k",
        ))
        with pytest.raises(ProviderError) as excinfo:
            await provider.connect()
        assert excinfo.value.category == "invalid_input"
        assert "eleven_v3" in str(excinfo.value)


class TestSilentGenerationIsAFailure:
    """A final carrying zero audio bytes must not look like success.

    ElevenLabs reports some account-level failures (an unpaid invoice) by
    closing the generation with no audio and no error frame — verified live
    2026-09-21 — which used to surface as "the provider returned no audio".
    """

    async def _drain(self, provider):
        events = []
        while not provider.events.empty():
            events.append(await provider.events.get())
        return events

    async def test_final_without_audio_emits_an_error(self):
        provider = ElevenLabsWebSocketTTSProvider(TTSStreamSettings(
            provider="elevenlabs", model="eleven_flash_v2_5", voice="v",
            language="hi-IN", sample_rate=16000, codec="pcm", api_key="k",
        ))
        provider._begin_generation("g1")
        await provider._handle_message({"isFinal": True, "contextId": "g1"})
        events = await self._drain(provider)
        assert [e.kind for e in events] == ["error"]
        assert "without returning any audio" in str(events[0].error)

    async def test_final_after_audio_is_a_normal_final(self):
        import base64

        provider = ElevenLabsWebSocketTTSProvider(TTSStreamSettings(
            provider="elevenlabs", model="eleven_flash_v2_5", voice="v",
            language="hi-IN", sample_rate=16000, codec="pcm", api_key="k",
        ))
        provider._begin_generation("g1")
        await provider._handle_message({
            "audio": base64.b64encode(b"\x00\x01" * 64).decode(), "contextId": "g1",
        })
        await provider._handle_message({"isFinal": True, "contextId": "g1"})
        events = await self._drain(provider)
        assert [e.kind for e in events] == ["audio", "final"]
