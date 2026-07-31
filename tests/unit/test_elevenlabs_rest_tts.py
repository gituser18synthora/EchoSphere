"""ElevenLabs REST TTS adapter: dynamic model_id and model-specific payloads.

The adapter must send exactly the selected model (eleven_v3 or
eleven_flash_v2_5), filter voice settings to what the model supports, apply
language_code enforcement only where the API accepts it, and surface API
failures as typed ProviderErrors — never silently switching models.
"""

import json

import httpx
import pytest

from shared.providers.base import ProviderConfig, ProviderError
from shared.providers.tts.elevenlabs import ElevenLabsTTS, voice_setting_keys

KEY_REF = "env:ELEVENLABS_UNIT_TEST_KEY"


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_UNIT_TEST_KEY", "sk-unit-test")


def make_adapter(monkeypatch=None, *, model: str, extra: dict | None = None,
                 status_code: int = 200, body: bytes = b"\x00\x01" * 160):
    """Adapter with its HTTP client swapped for a request-capturing mock."""
    adapter = ElevenLabsTTS(ProviderConfig(
        provider="elevenlabs", model=model, voice="voice-xyz",
        api_key_reference=KEY_REF, extra=extra or {},
    ))
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if status_code >= 400:
            return httpx.Response(status_code, text='{"detail": "nope"}')
        return httpx.Response(200, content=body)

    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"xi-api-key": "sk-unit-test"}
    )
    return adapter, captured


class TestDynamicModelSelection:
    async def test_eleven_v3_request(self):
        adapter, captured = make_adapter(
            model="eleven_v3",
            extra={"stability": 0.5, "similarity_boost": 0.9, "style": 0.2,
                   # unsupported on v3 — must never reach the wire
                   "use_speaker_boost": True, "speed": 1.1},
        )
        result = await adapter.synthesize("Hello world", language="hi-IN")
        await adapter.aclose()

        assert result.sample_rate == 16000 and result.audio
        request = captured[0]
        assert request.url.path == "/v1/text-to-speech/voice-xyz"
        assert request.url.params["output_format"] == "pcm_16000"
        payload = json.loads(request.content)
        assert payload["model_id"] == "eleven_v3"
        assert payload["voice_settings"] == {
            "stability": 0.5, "similarity_boost": 0.9, "style": 0.2,
        }
        # v3 does not accept language enforcement.
        assert "language_code" not in payload

    async def test_eleven_flash_v2_5_request(self):
        adapter, captured = make_adapter(
            model="eleven_flash_v2_5",
            extra={"stability": 0.0, "similarity_boost": 1.0,
                   "use_speaker_boost": True},
        )
        await adapter.synthesize("Hello world", language="hi-IN", speed=1.1)
        await adapter.aclose()

        payload = json.loads(captured[0].content)
        assert payload["model_id"] == "eleven_flash_v2_5"
        assert payload["language_code"] == "hi"
        settings = payload["voice_settings"]
        assert settings["use_speaker_boost"] is True
        assert settings["speed"] == 1.1  # delivery tuning lands on flash

    async def test_v3_ignores_delivery_speed(self):
        adapter, captured = make_adapter(model="eleven_v3", extra={})
        await adapter.synthesize("Hello", speed=1.2)
        await adapter.aclose()
        payload = json.loads(captured[0].content)
        assert "voice_settings" not in payload  # nothing supported was set

    async def test_canonical_delivery_speed_overrides_legacy_param(self):
        # Delivery tuning is the single speed control: a legacy `speed` left
        # in stored provider params must never shadow the canonical value.
        adapter, captured = make_adapter(
            model="eleven_flash_v2_5", extra={"speed": 0.9},
        )
        await adapter.synthesize("Hello", speed=1.2)
        await adapter.aclose()
        payload = json.loads(captured[0].content)
        assert payload["voice_settings"]["speed"] == 1.2

    async def test_canonical_speed_is_clamped_to_model_range(self):
        # ElevenLabs accepts 0.7–1.2 — a wider platform speed is clamped,
        # never sent out of range.
        adapter, captured = make_adapter(model="eleven_flash_v2_5", extra={})
        await adapter.synthesize("Hello", speed=1.8)
        await adapter.aclose()
        payload = json.loads(captured[0].content)
        assert payload["voice_settings"]["speed"] == 1.2

    async def test_missing_model_defaults_to_flash(self):
        # Backward compat: configurations without an explicit model keep the
        # current effective default — never silently upgraded to eleven_v3.
        adapter, captured = make_adapter(model="", extra={})
        await adapter.synthesize("Hello")
        await adapter.aclose()
        assert json.loads(captured[0].content)["model_id"] == "eleven_flash_v2_5"

    async def test_voice_setting_keys_matrix(self):
        assert voice_setting_keys("eleven_v3") == (
            "stability", "similarity_boost", "style")
        assert set(voice_setting_keys("eleven_flash_v2_5")) == {
            "stability", "similarity_boost", "style", "use_speaker_boost", "speed"}


class TestErrorHandling:
    @pytest.mark.parametrize("status,category", [
        (401, "auth"), (403, "auth"), (429, "rate_limit"),
        (400, "invalid_input"), (422, "invalid_input"), (500, "upstream"),
    ])
    async def test_api_errors_are_categorized(self, status, category):
        adapter, _ = make_adapter(model="eleven_v3", status_code=status)
        with pytest.raises(ProviderError) as exc_info:
            await adapter.synthesize("Hello")
        await adapter.aclose()
        assert exc_info.value.category == category

    async def test_empty_text_short_circuits(self):
        adapter, captured = make_adapter(model="eleven_v3")
        result = await adapter.synthesize("   ")
        await adapter.aclose()
        assert result.audio == b"" and captured == []

    async def test_missing_voice_rejected(self):
        adapter, _ = make_adapter(model="eleven_v3")
        adapter._voice = ""
        with pytest.raises(ProviderError) as exc_info:
            await adapter.synthesize("Hello")
        await adapter.aclose()
        assert exc_info.value.category == "invalid_input"
