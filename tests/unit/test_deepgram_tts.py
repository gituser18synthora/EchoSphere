"""Deepgram TTS (Aura / Aura-2): config reuse, catalog, languages, REST, WS.

No external services: REST goes through a transport mock, streaming through
the scriptable mock /v1/speak server in tests/mock_tts_servers.py.

The recurring theme is that Deepgram TTS has no language parameter — the
language is the voice id's suffix — so every "unsupported language" guard has
to be OURS. A missing guard does not produce an API error; it produces an
English voice reading Devanagari.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import shared.providers.deepgram_common as deepgram_common
from shared.providers.base import ProviderConfig, ProviderError
from shared.providers.factory import _REGISTRY, clear_provider_cache
from shared.providers.languages import (
    deepgram_models_speaking,
    deepgram_supports_language,
    deepgram_tts_language_tag,
    deepgram_voice_language_tag,
    deepgram_voice_model_family,
    tts_supports_language,
    tts_unsupported_language_message,
)
from shared.providers.tts.deepgram import DeepgramTTS
from shared.providers.tts.deepgram_voices import (
    encoding_for_codec,
    is_native_sample_rate,
    wire_sample_rate,
)
from shared.providers.tts.deepgram_ws import DeepgramWebSocketTTSProvider
from shared.providers.tts.streaming import TTSStreamSettings
from tests.mock_tts_servers import API_KEY, PCM_CHUNK, MockDeepgramTTSServer

PCM = b"\x11\x22" * 400


@pytest.fixture(autouse=True)
def _deepgram_env(monkeypatch):
    """A resolvable Deepgram key and a clean region for every test."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", API_KEY)
    monkeypatch.delenv("DEEPGRAM_REGION", raising=False)
    monkeypatch.delenv("DEEPGRAM_API_BASE", raising=False)
    monkeypatch.delenv("DEEPGRAM_WS_BASE", raising=False)
    clear_provider_cache()
    yield
    clear_provider_cache()


def rest_client(handler, **overrides) -> DeepgramTTS:
    """A DeepgramTTS whose httpx transport is the given handler."""
    values = dict(
        provider="deepgram", model="aura-2", voice="aura-2-thalia-en",
        language="en-IN", api_key_reference="env:DEEPGRAM_API_KEY",
        timeout_seconds=5.0, extra={},
    )
    values.update(overrides)
    client = DeepgramTTS(ProviderConfig(**values))
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers=client._client.headers,
        timeout=5.0,
    )
    return client


def stream_settings(**overrides) -> TTSStreamSettings:
    values = dict(
        provider="deepgram", model="aura-2", voice="aura-2-thalia-en",
        language="en-IN", sample_rate=24000, codec="pcm", params={},
        api_key=API_KEY, timeout_seconds=3.0,
    )
    values.update(overrides)
    return TTSStreamSettings(**values)


async def collect_until_final(provider, *, timeout=5.0, generation="g1"):
    audio, errors, got_final = [], [], False
    async with asyncio.timeout(timeout):
        while True:
            event = await provider.events.get()
            if event.kind == "audio" and event.generation_id == generation:
                audio.append(event.audio)
            elif event.kind == "final" and event.generation_id == generation:
                got_final = True
                break
            elif event.kind == "error":
                errors.append(event.error)
                break
    return audio, errors, got_final


# ── registration ────────────────────────────────────────────────────────────

class TestProviderRegistration:
    def test_factory_builds_the_deepgram_tts_adapter(self):
        assert _REGISTRY[("tts", "deepgram")] == (
            "shared.providers.tts.deepgram:DeepgramTTS"
        )

    def test_factory_builds_a_working_instance(self):
        from shared.providers.factory import get_tts_provider

        provider = get_tts_provider(ProviderConfig(
            provider="deepgram", model="aura-2", voice="aura-2-thalia-en",
            api_key_reference="env:DEEPGRAM_API_KEY",
        ))
        assert isinstance(provider, DeepgramTTS)
        assert provider.name == "deepgram"

    def test_registered_as_a_streaming_router_engine(self):
        from voice_runtime.tts_router import _STREAMING_PROVIDERS, _SUPPORTED_RATES

        assert _STREAMING_PROVIDERS["deepgram"] is DeepgramWebSocketTTSProvider
        # The telephony leg is 8 kHz and the browser leg 24 kHz: both must be
        # native, or every reply pays an avoidable resample.
        assert {8000, 24000} <= _SUPPORTED_RATES["deepgram"]

    def test_preview_endpoint_knows_the_streaming_client(self):
        from backend.routers.providers import _STREAMING_PREVIEW_CLIENTS

        assert _STREAMING_PREVIEW_CLIENTS["deepgram"] is DeepgramWebSocketTTSProvider

    def test_stt_registration_is_untouched(self):
        assert _REGISTRY[("stt", "deepgram")] == (
            "shared.providers.stt.deepgram:DeepgramSTT"
        )


# ── credential / region reuse ───────────────────────────────────────────────

class TestSharedDeepgramConfig:
    def test_tts_resolves_the_same_env_key_as_stt(self, monkeypatch):
        from shared.providers.stt.deepgram import DeepgramSTT

        monkeypatch.setenv("DEEPGRAM_API_KEY", "shared-secret")
        stt = DeepgramSTT(ProviderConfig(
            provider="deepgram", api_key_reference="env:DEEPGRAM_API_KEY"))
        tts = DeepgramTTS(ProviderConfig(
            provider="deepgram", voice="aura-2-thalia-en",
            api_key_reference="env:DEEPGRAM_API_KEY"))
        assert stt._client.headers["Authorization"] == "Token shared-secret"
        assert tts._client.headers["Authorization"] == "Token shared-secret"

    def test_blank_reference_falls_back_to_the_deepgram_key(self, monkeypatch):
        """No duplicate env var: an unset reference still finds DEEPGRAM_API_KEY
        rather than another vendor's TTS key."""
        monkeypatch.setenv("DEEPGRAM_API_KEY", "fallback-secret")
        tts = DeepgramTTS(ProviderConfig(
            provider="deepgram", voice="aura-2-thalia-en", api_key_reference=""))
        assert tts._client.headers["Authorization"] == "Token fallback-secret"

    def test_missing_credentials_fail_closed(self, monkeypatch):
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        with pytest.raises(ProviderError) as exc:
            DeepgramTTS(ProviderConfig(
                provider="deepgram", voice="aura-2-thalia-en", api_key_reference=""))
        assert exc.value.category == "auth"

    def test_auth_header_shape(self):
        assert deepgram_common.auth_headers("k") == {"Authorization": "Token k"}

    @pytest.mark.parametrize("value,expected", [
        (None, "global"), ("", "global"), ("in", "in"), ("IN", "in"),
        ("india", "in"), ("eu", "eu"), ("au", "au"), ("default", "global"),
        ("mars", "global"),
    ])
    def test_region_normalization(self, value, expected):
        assert deepgram_common.normalize_region(value) == expected

    def test_default_region_is_global(self):
        assert deepgram_common.rest_base_url() == "https://api.deepgram.com"
        assert deepgram_common.ws_base_url() == "wss://api.deepgram.com"

    def test_india_endpoint_is_the_official_host(self):
        assert deepgram_common.rest_base_url("in") == "https://api.in.deepgram.com"
        assert deepgram_common.ws_base_url("in") == "wss://api.in.deepgram.com"

    def test_platform_region_env_applies_to_both_transports(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_REGION", "in")
        assert deepgram_common.rest_base_url() == "https://api.in.deepgram.com"
        assert deepgram_common.ws_base_url() == "wss://api.in.deepgram.com"

    def test_explicit_base_override_wins_over_region(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_REGION", "in")
        monkeypatch.setenv("DEEPGRAM_WS_BASE", "ws://127.0.0.1:9")
        assert deepgram_common.ws_base_url("eu") == "ws://127.0.0.1:9"

    def test_rest_adapter_uses_the_india_host(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_REGION", "in")
        client = DeepgramTTS(ProviderConfig(
            provider="deepgram", voice="aura-2-thalia-en",
            api_key_reference="env:DEEPGRAM_API_KEY"))
        assert client._base_url == "https://api.in.deepgram.com"

    def test_engine_region_param_overrides_the_platform_default(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_REGION", "global")
        client = DeepgramTTS(ProviderConfig(
            provider="deepgram", voice="aura-2-thalia-en",
            api_key_reference="env:DEEPGRAM_API_KEY", extra={"region": "in"}))
        assert client._base_url == "https://api.in.deepgram.com"


# ── catalog ─────────────────────────────────────────────────────────────────

class TestCatalog:
    def test_models_are_catalogued_with_deepgram_wire_languages(self):
        from backend.seeds.provider_catalog_seed import PROVIDER_MODELS

        rows = {
            code: row for row in PROVIDER_MODELS
            if (row[0], row[1]) == ("deepgram", "tts") for code in [row[2]]
        }
        assert set(rows) == {"aura-2", "aura"}
        aura2, aura = rows["aura-2"], rows["aura"]
        assert aura2[4] == ["en", "es", "de", "nl", "fr", "it", "ja"]
        assert aura[4] == ["en"]
        # Both stream, and both offer the two pipeline rates natively.
        assert aura2[7] is True and aura[7] is True
        assert {8000, 24000} <= set(aura2[6])
        assert aura2[9] is True  # aura-2 is the Deepgram default

    def test_no_catalogued_model_claims_an_indian_language(self):
        from backend.seeds.provider_catalog_seed import PROVIDER_MODELS

        indic = {"hi", "ta", "te", "ml", "mr", "gu", "pa", "ur", "bn", "kn", "or"}
        for row in PROVIDER_MODELS:
            if (row[0], row[1]) != ("deepgram", "tts"):
                continue
            assert not (set(row[4]) & indic), row[2]

    def test_voices_carry_the_wire_model_id_and_english_locales_only(self):
        from backend.seeds.provider_catalog_seed import (
            DEEPGRAM_AURA1_VOICES,
            DEEPGRAM_AURA2_VOICES,
        )

        assert DEEPGRAM_AURA2_VOICES and DEEPGRAM_AURA1_VOICES
        for _vid, _name, gender, wire_id, _accent in DEEPGRAM_AURA2_VOICES:
            assert wire_id.startswith("aura-2-") and wire_id.endswith("-en")
            assert gender in ("male", "female")
        for _vid, _name, _g, wire_id, _accent in DEEPGRAM_AURA1_VOICES:
            assert wire_id.startswith("aura-") and not wire_id.startswith("aura-2-")

    def test_voice_locales_exclude_every_indian_language(self):
        from backend.seeds.provider_catalog_seed import _DEEPGRAM_VOICE_LOCALES

        assert _DEEPGRAM_VOICE_LOCALES == ["en-IN", "en-US", "en-GB"]

    def test_governance_matrix_allows_deepgram_tts(self):
        from backend.seeds.provider_catalog_seed import ALLOWED_ACTIVE_PROVIDERS

        assert "deepgram" in ALLOWED_ACTIVE_PROVIDERS["tts"]
        # The other vendors keep their places.
        assert {"sarvam", "elevenlabs"} <= ALLOWED_ACTIVE_PROVIDERS["tts"]

    def test_provider_row_reuses_the_deepgram_credential(self):
        from backend.seeds.base_seed import PROVIDERS

        tts_rows = [p for p in PROVIDERS if p[0] == "tts" and p[1] == "deepgram"]
        assert len(tts_rows) == 1 and tts_rows[0][5] == "active"
        # secret_ref is derived as env:<CODE>_API_KEY, the same reference the
        # STT row already uses — one Deepgram key, two capabilities.
        assert f"env:{tts_rows[0][1].upper()}_API_KEY" == "env:DEEPGRAM_API_KEY"

    def test_official_prices_are_seeded(self):
        from backend.seeds.base_seed import PROVIDER_PRICING

        prices = {
            row[2]: (row[3], row[4], row[5], row[6])
            for row in PROVIDER_PRICING
            if row[0] == "deepgram" and row[1] == "tts"
        }
        assert prices["aura-2"] == ("characters", "per_1k_characters", "0.030", "USD")
        assert prices["aura"] == ("characters", "per_1k_characters", "0.0150", "USD")

    def test_migration_chains_onto_an_existing_head(self):
        import importlib

        module = importlib.import_module(
            "backend.alembic.versions.c7e9a1b3d5f7_deepgram_tts_catalog"
        )
        assert module.revision == "c7e9a1b3d5f7"
        assert module.down_revision == "a1b2c3d4e5f6"
        assert {m[0] for m in module._TTS_MODELS} == {"aura-2", "aura"}
        assert module._VOICE_LOCALES == ["en-IN", "en-US", "en-GB"]


# ── language mapping ────────────────────────────────────────────────────────

class TestLanguageMapping:
    def test_english_india_maps_to_the_english_voices(self):
        assert deepgram_tts_language_tag("en-IN") == "en"
        assert deepgram_supports_language("aura-2", "en-IN") is True
        assert deepgram_supports_language("aura", "en-IN") is True

    @pytest.mark.parametrize(
        "locale", ["hi-IN", "ta-IN", "te-IN", "ml-IN", "mr-IN", "gu-IN",
                   "pa-IN", "ur-IN"])
    def test_no_indian_language_is_faked(self, locale):
        """The India endpoint is data residency, not Indic support."""
        assert deepgram_tts_language_tag(locale) is None
        assert deepgram_supports_language("aura-2", locale) is False
        assert deepgram_supports_language("aura", locale) is False
        assert deepgram_models_speaking(locale) == []

    def test_aura2_extra_languages_are_the_documented_ones(self):
        for locale in ("es-MX", "de-DE", "nl-NL", "fr-FR", "it-IT", "ja-JP"):
            assert deepgram_supports_language("aura-2", locale) is True
            # Aura v1 is English-only.
            assert deepgram_supports_language("aura", locale) is False

    def test_locale_case_and_separator_do_not_matter(self):
        assert deepgram_tts_language_tag("EN_in") == "en"
        assert deepgram_supports_language("aura-2", "en_IN") is True

    def test_internal_locale_is_never_sent_as_a_language_value(self, monkeypatch):
        """en-IN must not leak onto the wire: Deepgram has no language field,
        and the locale is not a Deepgram concept."""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, content=PCM)

        client = rest_client(handler)
        asyncio.run(client.synthesize("Hello there.", language="en-IN"))
        assert "language" not in seen["params"]
        assert "en-IN" not in json.dumps(seen["params"])
        assert set(seen["body"]) == {"text"}

    def test_unknown_model_is_not_a_rejection(self):
        assert deepgram_supports_language("aura-9", "en-IN") is None

    def test_voice_id_parsing(self):
        assert deepgram_voice_language_tag("aura-2-estrella-es") == "es"
        assert deepgram_voice_model_family("aura-2-thalia-en") == "aura-2"
        assert deepgram_voice_model_family("aura-asteria-en") == "aura"
        assert deepgram_voice_model_family("eleven_flash_v2_5") is None

    def test_provider_neutral_dispatch(self):
        assert tts_supports_language("deepgram", "aura-2", "hi-IN") is False
        assert tts_supports_language("deepgram", "aura-2", "en-IN") is True
        # Providers this module does not model must answer None, never False —
        # the DB catalog stays their gate.
        assert tts_supports_language("sarvam", "bulbul:v3", "hi-IN") is None
        assert tts_supports_language("mock", "mock", "hi-IN") is None

    def test_unsupported_message_names_the_real_constraint(self):
        message = tts_unsupported_language_message("deepgram", "aura-2", "hi-IN")
        assert "aura-2" in message and "hi-IN" in message
        assert "api.in.deepgram.com" in message  # the India-endpoint myth
        assert "per-language voice settings" in message

    def test_unsupported_message_points_at_aura2_for_a_v1_gap(self):
        message = tts_unsupported_language_message("deepgram", "aura", "de-DE")
        assert "Use aura-2" in message

    def test_elevenlabs_dispatch_is_unchanged(self):
        assert tts_supports_language("elevenlabs", "eleven_flash_v2_5", "hi-IN") is True
        assert tts_supports_language("elevenlabs", "eleven_flash_v2_5", "ml-IN") is False
        assert "eleven_v3" in tts_unsupported_language_message(
            "elevenlabs", "eleven_flash_v2_5", "ml-IN")


# ── output format ───────────────────────────────────────────────────────────

class TestOutputFormat:
    def test_pipeline_rates_are_native(self):
        assert is_native_sample_rate(8000)   # telephony leg
        assert is_native_sample_rate(24000)  # browser leg
        assert not is_native_sample_rate(22050)

    def test_codec_translation(self):
        assert encoding_for_codec("pcm") == "linear16"
        assert encoding_for_codec("linear16") == "linear16"
        assert encoding_for_codec("mulaw") == "mulaw"
        assert encoding_for_codec("ulaw") == "mulaw"
        assert encoding_for_codec(None) == "linear16"

    def test_companded_codecs_pin_8k(self):
        assert wire_sample_rate("mulaw", 24000) == 8000
        assert wire_sample_rate("pcm", 8000) == 8000
        assert wire_sample_rate("pcm", 22050) == 24000


# ── REST / preview synthesis ────────────────────────────────────────────────

class TestRestSynthesis:
    def test_requests_headerless_pcm_at_the_consumer_rate(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["params"] = dict(request.url.params)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, content=PCM)

        client = rest_client(handler, extra={"output_sample_rate": 8000})
        result = asyncio.run(client.synthesize("Hello.", language="en-IN"))

        assert result.audio == PCM
        assert result.sample_rate == 8000
        assert seen["params"]["model"] == "aura-2-thalia-en"
        assert seen["params"]["encoding"] == "linear16"
        assert seen["params"]["sample_rate"] == "8000"
        # container=none is what keeps the response headerless PCM.
        assert seen["params"]["container"] == "none"
        assert seen["auth"] == f"Token {API_KEY}"
        assert seen["url"].startswith("https://api.deepgram.com/v1/speak")

    def test_non_native_rate_falls_back_instead_of_asking_for_it(self, monkeypatch):
        def handler(request):
            assert request.url.params["sample_rate"] == "24000"
            return httpx.Response(200, content=PCM)

        client = rest_client(handler, extra={"output_sample_rate": 22050})
        assert asyncio.run(client.synthesize("Hi.")).sample_rate == 24000

    def test_delivery_speed_is_clamped_into_the_documented_range(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["speed"] = request.url.params.get("speed")
            return httpx.Response(200, content=PCM)

        client = rest_client(handler)
        asyncio.run(client.synthesize("Hi.", speed=1.9))
        assert seen["speed"] == "1.5"

    def test_default_speed_is_omitted(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, content=PCM)

        client = rest_client(handler)
        asyncio.run(client.synthesize("Hi.", speed=1.0))
        assert "speed" not in seen["params"]

    def test_india_region_changes_the_host(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, content=PCM)

        monkeypatch.setenv("DEEPGRAM_REGION", "in")
        client = rest_client(handler)
        asyncio.run(client.synthesize("Hi."))
        assert seen["url"].startswith("https://api.in.deepgram.com/v1/speak")

    def test_unsupported_language_is_refused_before_any_call(self, monkeypatch):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=PCM)

        client = rest_client(handler, language="hi-IN")
        with pytest.raises(ProviderError) as exc:
            asyncio.run(client.synthesize("नमस्ते", language="hi-IN"))
        assert exc.value.category == "invalid_input"
        assert not calls, "no audio must be billed for a refused language"

    def test_voice_from_the_wrong_generation_is_refused(self, monkeypatch):
        client = rest_client(
            lambda r: httpx.Response(200, content=PCM),
            model="aura-2", voice="aura-asteria-en",
        )
        with pytest.raises(ProviderError) as exc:
            asyncio.run(client.synthesize("Hi."))
        assert exc.value.category == "invalid_input"
        assert "aura" in str(exc.value)

    def test_voice_language_contradicting_the_locale_is_refused(self, monkeypatch):
        client = rest_client(
            lambda r: httpx.Response(200, content=PCM),
            voice="aura-2-estrella-es", language="en-IN",
        )
        with pytest.raises(ProviderError) as exc:
            asyncio.run(client.synthesize("Hello."))
        assert "speaks 'es'" in str(exc.value)

    def test_missing_voice_uses_the_model_default(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["model"] = request.url.params["model"]
            return httpx.Response(200, content=PCM)

        client = rest_client(handler, voice="")
        asyncio.run(client.synthesize("Hi."))
        assert seen["model"] == "aura-2-thalia-en"

    def test_empty_text_never_calls_the_provider(self, monkeypatch):
        calls = []
        client = rest_client(
            lambda r: calls.append(r) or httpx.Response(200, content=PCM))
        assert asyncio.run(client.synthesize("   ")).audio == b""
        assert not calls

    @pytest.mark.parametrize("status,category", [
        (401, "auth"), (403, "auth"), (429, "rate_limit"),
        (400, "invalid_input"), (404, "invalid_input"), (500, "upstream"),
    ])
    def test_http_failures_are_categorized(self, monkeypatch, status, category):
        client = rest_client(lambda r: httpx.Response(status, text="nope"))
        with pytest.raises(ProviderError) as exc:
            asyncio.run(client.synthesize("Hi."))
        assert exc.value.category == category

    def test_timeout_is_reported_as_a_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.TimeoutException("too slow", request=request)

        client = rest_client(handler)
        with pytest.raises(ProviderError) as exc:
            asyncio.run(client.synthesize("Hi."))
        assert exc.value.category == "timeout"


# ── WebSocket streaming ─────────────────────────────────────────────────────

class TestWebSocketStreaming:
    async def test_happy_path_streams_audio_then_final(self, monkeypatch):
        async with MockDeepgramTTSServer(chunks=3) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.synthesize_stream("Hello there.", generation_id="g1")
            await provider.flush("g1")
            audio, errors, final = await collect_until_final(provider)
            await provider.close()

        assert final and not errors
        assert audio == [PCM_CHUNK] * 3
        assert server.texts() == ["Hello there."]
        # The wire "model" is the VOICE id; encoding/rate come from the
        # consumer's transport, and no locale appears anywhere.
        query = server.queries[0]
        assert query["model"] == "aura-2-thalia-en"
        assert query["encoding"] == "linear16"
        assert query["sample_rate"] == "24000"
        assert "en-IN" not in query["_raw"] and "language" not in query

    async def test_chunks_are_delivered_as_they_arrive(self, monkeypatch):
        """Playback must not wait for the whole response.

        The server spaces its chunks out, so the first audio event has to
        reach the consumer measurably before the generation completes — a
        provider that buffered until the end would emit both at once.
        """
        async with MockDeepgramTTSServer(chunks=3, chunk_delay=0.15) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.synthesize_stream("Streamed.", generation_id="g1")
            await provider.flush("g1")
            first = await asyncio.wait_for(provider.events.get(), timeout=5)
            first_at = asyncio.get_running_loop().time()
            assert first.kind == "audio" and first.audio == PCM_CHUNK
            rest, errors, final = await collect_until_final(provider)
            final_at = asyncio.get_running_loop().time()
            await provider.close()

        assert final and not errors
        assert rest == [PCM_CHUNK] * 2, "every chunk arrives as its own event"
        assert final_at - first_at > 0.2, (
            "first audio was not delivered ahead of the rest of the response"
        )

    async def test_telephony_8k_is_requested_natively(self, monkeypatch):
        async with MockDeepgramTTSServer() as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(
                stream_settings(sample_rate=8000))
            await provider.synthesize_stream("Hi.", generation_id="g1")
            await provider.flush("g1")
            await collect_until_final(provider)
            await provider.close()
        assert server.queries[0]["sample_rate"] == "8000"
        assert server.queries[0]["encoding"] == "linear16"

    async def test_mulaw_pins_8k(self, monkeypatch):
        async with MockDeepgramTTSServer() as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(
                stream_settings(codec="mulaw", sample_rate=24000))
            await provider.synthesize_stream("Hi.", generation_id="g1")
            await provider.flush("g1")
            await collect_until_final(provider)
            await provider.close()
        assert server.queries[0]["encoding"] == "mulaw"
        assert server.queries[0]["sample_rate"] == "8000"

    async def test_india_region_is_used_for_the_socket(self, monkeypatch):
        """No live call: the URL is asserted without connecting."""
        monkeypatch.setenv("DEEPGRAM_REGION", "in")
        provider = DeepgramWebSocketTTSProvider(stream_settings())
        assert provider._build_url().startswith(
            "wss://api.in.deepgram.com/v1/speak?")

    async def test_engine_region_param_overrides_the_default(self, monkeypatch):
        provider = DeepgramWebSocketTTSProvider(
            stream_settings(params={"region": "eu"}))
        assert provider._build_url().startswith("wss://api.eu.deepgram.com/")

    async def test_delivery_speed_reaches_the_url(self, monkeypatch):
        provider = DeepgramWebSocketTTSProvider(
            stream_settings(params={"speed": 1.2}))
        assert "speed=1.2" in provider._build_url()

    async def test_several_generations_complete_in_dispatch_order(self, monkeypatch):
        """Deepgram puts no id on the wire — order is the correspondence."""
        async with MockDeepgramTTSServer(chunks=1) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            for gid, text in (("g1", "One."), ("g2", "Two.")):
                await provider.synthesize_stream(text, generation_id=gid)
                await provider.flush(gid)
                audio, errors, final = await collect_until_final(
                    provider, generation=gid)
                assert final and not errors and audio == [PCM_CHUNK]
            await provider.close()
        assert server.texts() == ["One.", "Two."]

    async def test_flush_is_what_completes_the_generation(self, monkeypatch):
        async with MockDeepgramTTSServer(chunks=1) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.synthesize_stream("Buffered.", generation_id="g1")
            await asyncio.sleep(0.1)
            assert provider.events.empty(), "nothing renders before the flush"
            await provider.flush("g1")
            _, errors, final = await collect_until_final(provider)
            await provider.close()
        assert final and not errors
        # Speak buffers, Flush renders (the trailing Close is the teardown).
        assert [m["type"] for m in server.received][:2] == ["Speak", "Flush"]

    async def test_cancel_clears_server_side_and_keeps_the_socket(self, monkeypatch):
        async with MockDeepgramTTSServer(chunks=2) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.synthesize_stream("Interrupted.", generation_id="g1")
            await provider.cancel("g1")
            assert not provider.generation_alive("g1")
            # Barge-in must NOT cost a reconnect: the next reply reuses it.
            await provider.synthesize_stream("Next reply.", generation_id="g2")
            await provider.flush("g2")
            audio, errors, final = await collect_until_final(
                provider, generation="g2")
            await provider.close()
        assert server.clears == 1
        assert server.connections == 1, "cancel must not drop the connection"
        assert final and not errors and audio == [PCM_CHUNK] * 2

    async def test_late_audio_after_cancel_is_dropped(self, monkeypatch):
        async with MockDeepgramTTSServer(behavior="late_after_clear", chunks=3) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.synthesize_stream("Interrupted.", generation_id="g1")
            await provider.cancel("g1")
            await asyncio.sleep(0.2)
            await provider.close()
        assert server.clears == 1
        # Nothing for the cancelled generation may reach the consumer.
        events = []
        while not provider.events.empty():
            events.append(provider.events.get_nowait())
        assert not [e for e in events if e.kind == "audio"]

    async def test_graceful_close_sends_close_and_is_idempotent(self, monkeypatch):
        async with MockDeepgramTTSServer(chunks=1) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.synthesize_stream("Bye.", generation_id="g1")
            await provider.flush("g1")
            await collect_until_final(provider)
            await provider.close()
            await provider.close()  # idempotent
        assert server.closes == 1
        assert provider._ws is None and not provider._pending

    async def test_cleanup_leaves_no_receive_task(self, monkeypatch):
        async with MockDeepgramTTSServer(chunks=1) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.connect()
            assert provider._receive_task is not None
            await provider.close()
        assert provider._receive_task is None
        with pytest.raises(RuntimeError):
            await provider.connect()

    async def test_voice_change_reconnects_param_change_does_not(self, monkeypatch):
        async with MockDeepgramTTSServer(chunks=1) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.connect()
            await provider.configure(stream_settings(params={"mip_opt_out": False}))
            assert provider._ws is not None, "a no-op change must not reconnect"
            await provider.configure(stream_settings(voice="aura-2-zeus-en"))
            await provider.synthesize_stream("Hi.", generation_id="g1")
            await provider.flush("g1")
            await collect_until_final(provider)
            await provider.close()
        assert server.connections == 2
        assert server.queries[-1]["model"] == "aura-2-zeus-en"

    async def test_auth_rejection_surfaces_and_never_retries_into_success(
            self, monkeypatch):
        async with MockDeepgramTTSServer(behavior="auth_fail") as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            with pytest.raises(ProviderError) as exc:
                await provider.connect()
            await provider.close()
        assert exc.value.category == "auth"

    async def test_bad_stream_configuration_is_not_transient(self, monkeypatch):
        """A 400 handshake must not burn the engine fallback on a retry that
        cannot succeed."""
        from shared.providers.tts.streaming import TRANSIENT_ERROR_CATEGORIES

        async with MockDeepgramTTSServer(behavior="bad_config") as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            with pytest.raises(ProviderError) as exc:
                await provider.connect()
            await provider.close()
        assert exc.value.category == "invalid_input"
        assert exc.value.category not in TRANSIENT_ERROR_CATEGORIES

    async def test_rate_limit_is_transient(self, monkeypatch):
        from shared.providers.tts.streaming import TRANSIENT_ERROR_CATEGORIES

        async with MockDeepgramTTSServer(behavior="rate_limit") as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            with pytest.raises(ProviderError) as exc:
                await provider.connect()
            await provider.close()
        assert exc.value.category in TRANSIENT_ERROR_CATEGORIES

    async def test_unsupported_language_is_refused_before_connecting(
            self, monkeypatch):
        async with MockDeepgramTTSServer() as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(
                stream_settings(language="hi-IN"))
            with pytest.raises(ProviderError) as exc:
                await provider.connect()
            await provider.close()
        assert exc.value.category == "invalid_input"
        assert server.connections == 0, "no socket for a language it cannot speak"
        # The consumer draining events must also learn why.
        event = provider.events.get_nowait()
        assert event.kind == "error" and "hi-IN" in str(event.error)

    async def test_provider_error_message_reaches_the_event_queue(self, monkeypatch):
        async with MockDeepgramTTSServer(behavior="error_message") as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.synthesize_stream("Hi.", generation_id="g1")
            await provider.flush("g1")
            _, errors, final = await collect_until_final(provider)
            await provider.close()
        assert not final and errors
        assert "synthesis backend unavailable" in str(errors[0])

    async def test_warning_frames_do_not_fail_the_generation(self, monkeypatch):
        async with MockDeepgramTTSServer(behavior="warning", chunks=2) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.synthesize_stream("Hi.", generation_id="g1")
            await provider.flush("g1")
            audio, errors, final = await collect_until_final(provider)
            await provider.close()
        assert final and not errors and audio == [PCM_CHUNK] * 2

    async def test_invalid_json_frame_is_discarded(self, monkeypatch):
        async with MockDeepgramTTSServer(behavior="invalid_json", chunks=2) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.synthesize_stream("Hi.", generation_id="g1")
            await provider.flush("g1")
            audio, errors, final = await collect_until_final(provider)
            await provider.close()
        assert final and not errors and audio == [PCM_CHUNK] * 2

    async def test_flush_with_no_audio_is_reported_not_silently_final(
            self, monkeypatch):
        """A Flushed carrying zero bytes would otherwise render as dead air."""
        async with MockDeepgramTTSServer(behavior="silent") as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.synthesize_stream("Hi.", generation_id="g1")
            await provider.flush("g1")
            _, errors, final = await collect_until_final(provider)
            await provider.close()
        assert not final and errors
        assert "without returning any audio" in str(errors[0])

    async def test_connection_drop_mid_generation_is_reported(self, monkeypatch):
        async with MockDeepgramTTSServer(behavior="drop_conn") as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.synthesize_stream("Hi.", generation_id="g1")
            _, errors, final = await collect_until_final(provider)
            await provider.close()
        assert not final and errors
        assert "closed mid-generation" in str(errors[0])

    async def test_connect_timeout_is_bounded(self, monkeypatch):
        # Unroutable port: the two short attempts must give up quickly.
        monkeypatch.setenv("DEEPGRAM_WS_BASE", "ws://127.0.0.1:1")
        provider = DeepgramWebSocketTTSProvider(stream_settings())
        started = asyncio.get_running_loop().time()
        with pytest.raises(ProviderError) as exc:
            await provider.connect()
        elapsed = asyncio.get_running_loop().time() - started
        await provider.close()
        assert exc.value.category == "timeout"
        assert elapsed < 8, f"connect took {elapsed:.1f}s — the budget is two short tries"


# ── migration round trip ────────────────────────────────────────────────────

class TestMigrationRoundTrip:
    """upgrade() then downgrade() against a schema shaped like the tables it
    touches — no MySQL needed, and nothing the migration did may survive."""

    @staticmethod
    def _schema(conn):
        import sqlalchemy as sa

        conn.execute(sa.text(
            "create table provider_defs (id varchar(40) primary key, kind varchar(20), "
            "code varchar(50), name varchar(150), description text, requires_api_key int, "
            "secret_ref varchar(300), status varchar(20), sort_order int, "
            "created_at datetime, updated_at datetime, is_deleted int default 0)"))
        conn.execute(sa.text(
            "create table provider_models (id varchar(40) primary key, provider_code varchar(50), "
            "capability varchar(20), code varchar(80), display_name varchar(150), description text, "
            "languages text, codecs text, sample_rates text, streaming int, params_schema text, "
            "is_default int, status varchar(20), sort_order int, created_at datetime, "
            "updated_at datetime, is_deleted int default 0)"))
        conn.execute(sa.text(
            "create table voice_profiles (id varchar(40) primary key, source varchar(20), "
            "name varchar(100), gender varchar(10), languages text, accent varchar(100), "
            "styles text, latency_ms int, premium int, sample_text text, provider varchar(100), "
            "provider_voice_id varchar(100), speaking_rate float, pitch float, model_codes text, "
            "provider_settings text, is_default int, status varchar(20), sort_order int, "
            "created_at datetime, updated_at datetime, is_deleted int default 0)"))
        conn.execute(sa.text(
            "create table provider_pricing (id varchar(40) primary key, provider_code varchar(50), "
            "capability varchar(20), model_code varchar(80), component varchar(40), unit varchar(30), "
            "unit_price numeric, currency_code varchar(3), effective_from datetime, status varchar(20), "
            "sort_order int, created_at datetime, updated_at datetime, is_deleted int default 0)"))
        conn.execute(sa.text(
            "create table supported_languages (code varchar(15) primary key, "
            "provider_support text, updated_at datetime)"))
        conn.execute(sa.text("create table currencies (code varchar(3) primary key)"))
        conn.execute(sa.text("insert into currencies (code) values ('USD')"))
        conn.execute(sa.text(
            "insert into supported_languages (code, provider_support) values "
            "('en-IN', '{\"stt\": [\"sarvam\"], \"tts\": [\"sarvam\"]}'), "
            "('hi-IN', '{\"stt\": [\"sarvam\"], \"tts\": [\"sarvam\"]}')"))

    def test_upgrade_then_downgrade(self):
        import importlib

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        module = importlib.import_module(
            "backend.alembic.versions.c7e9a1b3d5f7_deepgram_tts_catalog")
        engine = sa.create_engine("sqlite://")
        with engine.begin() as conn:
            self._schema(conn)

        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                module.upgrade()
                module.upgrade()  # idempotent

            provider = conn.execute(sa.text(
                "select secret_ref, status from provider_defs where kind='tts' "
                "and code='deepgram'")).first()
            # The same credential the STT row uses — no second Deepgram key.
            assert provider == ("env:DEEPGRAM_API_KEY", "active")

            models = dict(conn.execute(sa.text(
                "select code, languages from provider_models where "
                "provider_code='deepgram' and capability='tts'")).all())
            assert set(models) == {"aura-2", "aura"}
            assert json.loads(models["aura"]) == ["en"]

            voices = conn.execute(sa.text(
                "select count(*) from voice_profiles where provider='deepgram'")).scalar()
            assert voices == 17

            prices = dict(conn.execute(sa.text(
                "select model_code, unit_price from provider_pricing where "
                "provider_code='deepgram' and capability='tts'")).all())
            assert float(prices["aura-2"]) == 0.030
            assert float(prices["aura"]) == 0.0150

            # en-IN gains Deepgram for TTS; hi-IN must NOT.
            en = json.loads(conn.execute(sa.text(
                "select provider_support from supported_languages where code='en-IN'")).scalar())
            hi = json.loads(conn.execute(sa.text(
                "select provider_support from supported_languages where code='hi-IN'")).scalar())
            assert "deepgram" in en["tts"] and "sarvam" in en["tts"]
            assert "deepgram" not in hi["tts"]

        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                module.downgrade()
            assert conn.execute(sa.text(
                "select count(*) from provider_models where provider_code='deepgram' "
                "and capability='tts'")).scalar() == 0
            assert conn.execute(sa.text(
                "select count(*) from voice_profiles where provider='deepgram'")).scalar() == 0
            assert conn.execute(sa.text(
                "select status from provider_defs where kind='tts' and "
                "code='deepgram'")).scalar() == "inactive"


# ── the sequence the live router actually drives ────────────────────────────

class TestRouterLifecycleSequence:
    """The StreamingTTSRouter's real call pattern, end to end on the socket.

    In streaming mode the router sends several sentences into ONE generation
    and may flush mid-turn (TTSFlushHintFrame) before the turn completes.
    Deepgram answers every Flush with a Flushed, so a mid-turn flush produces
    an early ``final`` — exactly as Sarvam does, which is the case the router
    already handles (``midturn_final_seen``). What must NOT happen is audio
    for the rest of the turn being dropped after that early final.
    """

    async def test_midturn_flush_then_more_text_still_renders(self, monkeypatch):
        async with MockDeepgramTTSServer(chunks=2) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())

            # First sentence + the mid-turn flush hint.
            await provider.synthesize_stream("First sentence.", generation_id="ctx1")
            await provider.flush("ctx1")
            audio1, errors1, final1 = await collect_until_final(
                provider, generation="ctx1")
            assert final1 and not errors1 and audio1 == [PCM_CHUNK] * 2

            # More of the SAME turn arrives after that early final.
            await provider.synthesize_stream("Second sentence.", generation_id="ctx1")
            await provider.flush("ctx1")
            await provider.finish("ctx1")
            audio2, errors2, final2 = await collect_until_final(
                provider, generation="ctx1")
            await provider.close()

        assert final2 and not errors2
        assert audio2 == [PCM_CHUNK] * 2, "post-flush audio must not be dropped"
        assert server.texts() == ["First sentence.", "Second sentence."]
        assert server.connections == 1

    async def test_barge_in_midturn_then_a_fresh_reply(self, monkeypatch):
        """Interruption cancels, the socket survives, the next reply speaks."""
        async with MockDeepgramTTSServer(chunks=2) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.synthesize_stream("Long reply.", generation_id="ctx1")
            await provider.cancel("ctx1")

            await provider.synthesize_stream("New answer.", generation_id="ctx2")
            await provider.flush("ctx2")
            await provider.finish("ctx2")
            audio, errors, final = await collect_until_final(
                provider, generation="ctx2")
            await provider.close()

        assert final and not errors and audio == [PCM_CHUNK] * 2
        assert server.clears == 1 and server.connections == 1

    async def test_language_switch_reuses_or_rebuilds_deliberately(self, monkeypatch):
        """SwitchVoiceLanguageFrame → configure(): only a URL change reconnects."""
        async with MockDeepgramTTSServer(chunks=1) as server:
            monkeypatch.setenv("DEEPGRAM_WS_BASE", server.url)
            provider = DeepgramWebSocketTTSProvider(stream_settings())
            await provider.connect()
            # en-IN → en-IN on another voice: new URL, so a reconnect.
            await provider.configure(stream_settings(voice="aura-2-pandora-en"))
            await provider.synthesize_stream("Hello.", generation_id="g1")
            await provider.flush("g1")
            _, errors, final = await collect_until_final(provider)
            await provider.close()
        assert final and not errors
        assert server.connections == 2
        assert server.queries[-1]["model"] == "aura-2-pandora-en"

    async def test_router_settings_carry_no_foreign_provider_params(self):
        """resolve_engine_params must not leak Sarvam/ElevenLabs keys into a
        Deepgram URL — only ``speed`` and ``region`` are Deepgram's."""
        from shared.providers.tts.delivery import resolve_engine_params

        params = resolve_engine_params(
            {"provider": "sarvam", "model": "bulbul:v3",
             "settings": {"pace": 1.4, "temperature": 0.7}},
            {"provider": "deepgram", "model": "aura-2",
             "voice": "aura-2-thalia-en", "params": {}},
            speed=1.3, energy=80,
        )
        assert params == {"speed": 1.3}
        url = DeepgramWebSocketTTSProvider(stream_settings(params=params))._build_url()
        assert "speed=1.3" in url
        for leaked in ("pace", "temperature", "stability", "style"):
            assert leaked not in url


# ── the STT adapter keeps its behaviour ─────────────────────────────────────

class TestSttBehaviourPreserved:
    """The STT adapter now shares Deepgram's credentials/host helpers. What
    it must NOT have picked up is the TTS error semantics."""

    @staticmethod
    def _stt(handler, **overrides):
        from shared.providers.stt.deepgram import DeepgramSTT

        values = dict(
            provider="deepgram", model="nova-2",
            api_key_reference="env:DEEPGRAM_API_KEY", timeout_seconds=5.0,
        )
        values.update(overrides)
        client = DeepgramSTT(ProviderConfig(**values))
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers=client._client.headers, timeout=5.0,
        )
        return client

    def test_default_host_and_path_are_unchanged(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"results": {"channels": [
                {"alternatives": [{"transcript": "hello", "confidence": 0.9}]}]}})

        client = self._stt(handler)
        result = asyncio.run(client.transcribe(b"\x00\x01" * 100, language="en"))
        assert result.text == "hello"
        assert seen["url"].startswith("https://api.deepgram.com/v1/listen")
        assert "model=nova-2" in seen["url"] and "smart_format=true" in seen["url"]

    def test_400_still_reports_upstream_not_invalid_input(self):
        """The TTS helper maps 400 → invalid_input; STT must keep 'upstream'."""
        client = self._stt(lambda r: httpx.Response(400, text="bad"))
        with pytest.raises(ProviderError) as exc:
            asyncio.run(client.transcribe(b"\x00\x01" * 100))
        assert exc.value.category == "upstream"

    @pytest.mark.parametrize("status,category", [
        (401, "auth"), (403, "auth"), (429, "rate_limit"), (500, "upstream"),
    ])
    def test_other_status_mappings_are_unchanged(self, status, category):
        client = self._stt(lambda r: httpx.Response(status, text="x"))
        with pytest.raises(ProviderError) as exc:
            asyncio.run(client.transcribe(b"\x00\x01" * 100))
        assert exc.value.category == category

    def test_stt_also_honours_the_region(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"results": {"channels": []}})

        monkeypatch.setenv("DEEPGRAM_REGION", "in")
        client = self._stt(handler)
        asyncio.run(client.transcribe(b"\x00\x01" * 100))
        assert seen["url"].startswith("https://api.in.deepgram.com/v1/listen")

    def test_flux_realtime_path_has_no_tts_coupling(self):
        """The live STT path is pipecat's Flux service. STT and TTS share
        vendor CONFIG, never runtime logic, so the Flux module must not
        import anything from the TTS side."""
        import inspect

        import voice_runtime.deepgram_stt as flux

        imports = [
            line for line in inspect.getsource(flux).splitlines()
            if line.startswith(("import ", "from "))
        ]
        assert not [line for line in imports if "providers.tts" in line]
