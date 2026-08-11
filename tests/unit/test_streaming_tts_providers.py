"""WebSocket TTS provider clients against scriptable mock provider servers.

Covers the connection lifecycle, config translation, audio chunk streaming,
invalid payload handling, auth/rate-limit/timeout failures, cancellation,
late-audio rejection and reconnection — all without external services.
"""

import asyncio

import pytest

import shared.providers.tts.elevenlabs_ws as elevenlabs_ws
import shared.providers.tts.sarvam_ws as sarvam_ws
from shared.providers.base import ProviderError
from shared.providers.tts.elevenlabs_ws import ElevenLabsWebSocketTTSProvider
from shared.providers.tts.sarvam_ws import SarvamWebSocketTTSProvider
from shared.providers.tts.streaming import TTSStreamSettings
from tests.mock_tts_servers import (
    API_KEY,
    PCM_CHUNK,
    MockElevenLabsServer,
    MockSarvamTTSServer,
)


def sarvam_settings(**overrides) -> TTSStreamSettings:
    values = dict(
        provider="sarvam", model="bulbul:v3", voice="shubh", language="hi-IN",
        sample_rate=24000, codec="linear16",
        params={"pace": 1.0, "temperature": 0.6, "min_buffer_size": 40},
        api_key=API_KEY, timeout_seconds=3.0,
    )
    values.update(overrides)
    return TTSStreamSettings(**values)


def eleven_settings(**overrides) -> TTSStreamSettings:
    values = dict(
        provider="elevenlabs", model="eleven_flash_v2_5",
        voice="f1abxvIEijusskcPWE5x", language="hi-IN",
        sample_rate=16000, codec="pcm",
        params={"stability": 0.0, "similarity_boost": 1.0, "auto_mode": True,
                "chunk_length_schedule": [120, 160], "inactivity_timeout": 60},
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


class TestSarvamProvider:
    async def test_happy_path_streams_audio_and_final(self, monkeypatch):
        async with MockSarvamTTSServer(chunks=3) as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.synthesize_stream("नमस्ते दुनिया.", generation_id="g1")
            await provider.flush("g1")
            audio, errors, final = await collect_until_final(provider)
            await provider.close()

        assert final and not errors
        assert audio == [PCM_CHUNK] * 3
        # Config translation: wire language code, lowercase speaker, v3 params.
        config = server.configs[0]
        assert config["target_language_code"] == "hi-IN"
        assert config["speaker"] == "shubh"
        assert config["model"] == "bulbul:v3"
        assert config["temperature"] == 0.6
        assert config["enable_preprocessing"] is True
        assert "pitch" not in config and "loudness" not in config
        assert "model=bulbul:v3" in server.queries[0]["_raw"]
        assert "send_completion_event=true" in server.queries[0]["_raw"]
        assert server.texts() == ["नमस्ते दुनिया."]

    async def test_delivery_energy_mapping_respects_model_capabilities(self, monkeypatch):
        """apply_delivery_params + the adapter together guarantee the wire
        contract: bulbul:v2 receives the conservative pitch/loudness energy
        mapping, bulbul:v3 (which does not document them) receives neither —
        even if a stale value sneaks into stored params."""
        from shared.providers.tts.delivery import apply_delivery_params

        async with MockSarvamTTSServer() as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            v2_params = apply_delivery_params(
                "sarvam", "bulbul:v2", {"min_buffer_size": 40}, speed=1.2, energy=90,
            )
            provider = SarvamWebSocketTTSProvider(
                sarvam_settings(model="bulbul:v2", voice="anushka", params=v2_params)
            )
            await provider.connect()
            await provider.close()
        v2_config = server.configs[0]
        assert v2_config["pace"] == 1.2
        assert v2_config["pitch"] == 0.1 and v2_config["loudness"] == 1.3

        async with MockSarvamTTSServer() as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            v3_params = apply_delivery_params(
                "sarvam", "bulbul:v3", {"pitch": 0.5}, speed=1.2, energy=90,
            )
            provider = SarvamWebSocketTTSProvider(
                sarvam_settings(params=v3_params)
            )
            await provider.connect()
            await provider.close()
        v3_config = server.configs[0]
        assert v3_config["pace"] == 1.2
        # The stale v2-only field is filtered at the adapter — never on the wire.
        assert "pitch" not in v3_config and "loudness" not in v3_config

    async def test_odia_language_alias(self, monkeypatch):
        async with MockSarvamTTSServer() as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings(language="or-IN"))
            await provider.connect()
            await provider.close()
        assert server.configs[0]["target_language_code"] == "od-IN"

    async def test_invalid_json_and_b64_are_skipped(self, monkeypatch):
        for behavior in ("invalid_json", "invalid_b64"):
            async with MockSarvamTTSServer(behavior=behavior, chunks=2) as server:
                monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
                provider = SarvamWebSocketTTSProvider(sarvam_settings())
                await provider.synthesize_stream("Hello.", generation_id="g1")
                await provider.flush("g1")
                audio, errors, final = await collect_until_final(provider)
                await provider.close()
            assert final and not errors, behavior
            assert audio == [PCM_CHUNK] * 2, behavior

    async def test_auth_failure_raises_and_emits_auth_error(self, monkeypatch):
        async with MockSarvamTTSServer(behavior="auth_fail") as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            with pytest.raises(Exception):
                await provider.connect()
            event = await provider.events.get()
            assert event.kind == "error" and event.error.category == "auth"
            await provider.close()

    async def test_rate_limit_categorized(self, monkeypatch):
        async with MockSarvamTTSServer(behavior="rate_limit") as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            with pytest.raises(Exception):
                await provider.connect()
            event = await provider.events.get()
            assert event.kind == "error" and event.error.category == "rate_limit"
            await provider.close()

    async def test_connect_timeout_unreachable(self, monkeypatch):
        monkeypatch.setattr(sarvam_ws, "_WS_URL", "ws://127.0.0.1:1")  # nothing listens
        provider = SarvamWebSocketTTSProvider(sarvam_settings(timeout_seconds=0.5))
        with pytest.raises(Exception):
            await provider.connect()
        await provider.close()

    async def test_server_error_message_categorized(self, monkeypatch):
        async with MockSarvamTTSServer(behavior="error_message") as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.synthesize_stream("Hi.", generation_id="g1")
            await provider.flush("g1")
            audio, errors, final = await collect_until_final(provider)
            await provider.close()
        assert not final and errors and errors[0].category == "upstream"

    async def test_connection_drop_mid_generation_reports_error(self, monkeypatch):
        async with MockSarvamTTSServer(behavior="drop_conn") as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.synthesize_stream("Hi.", generation_id="g1")
            async with asyncio.timeout(3):
                while True:
                    event = await provider.events.get()
                    if event.kind == "error":
                        assert event.generation_id == "g1"
                        break
            await provider.close()

    async def test_cancel_closes_socket_and_reconnects_next_turn(self, monkeypatch):
        async with MockSarvamTTSServer(behavior="silent") as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.synthesize_stream("First reply.", generation_id="g1")
            await provider.cancel("g1")  # barge-in: drops the connection
            assert not provider.generation_alive("g1")
            await provider.synthesize_stream("Second reply.", generation_id="g2")
            await asyncio.sleep(0.1)
            await provider.close()
        assert server.connections == 2  # lazy reconnect for the new generation
        assert len(server.configs) == 2  # config re-sent on the new connection

    async def test_cancel_schedules_background_reconnect(self, monkeypatch):
        """Barge-in tears the socket down (Sarvam has no server-side cancel);
        a background reconnect must rebuild it so the next reply doesn't pay
        the cold-connect handshake inline."""
        async with MockSarvamTTSServer(behavior="silent") as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.synthesize_stream("First reply.", generation_id="g1")
            await provider.cancel("g1")
            async with asyncio.timeout(3):
                while server.connections < 2:
                    await asyncio.sleep(0.02)
            await provider.close()
        assert server.connections == 2  # reconnected without a new dispatch
        assert provider._reconnect_task is None  # close() cleaned it up

    async def test_redundant_config_resend_is_skipped(self, monkeypatch):
        """A config resend auto-flushes server-side — identical settings on
        the same socket must not send a second config message."""
        async with MockSarvamTTSServer() as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.connect()
            await provider.configure(sarvam_settings())  # nothing changed
            await asyncio.sleep(0.1)
            assert len(server.configs) == 1
            await provider.configure(sarvam_settings(voice="ritu"))
            await asyncio.sleep(0.1)
            await provider.close()
        assert len(server.configs) == 2  # a real change still resends

    async def test_configure_reuses_connection_for_voice_switch(self, monkeypatch):
        async with MockSarvamTTSServer() as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.connect()
            await provider.configure(sarvam_settings(voice="ritu", language="en-IN"))
            await asyncio.sleep(0.1)
            await provider.close()
        assert server.connections == 1  # no reconnect — config resent instead
        assert server.configs[-1]["speaker"] == "ritu"
        assert server.configs[-1]["target_language_code"] == "en-IN"

    async def test_keepalive_ping(self, monkeypatch):
        monkeypatch.setattr(sarvam_ws, "_KEEPALIVE_SECONDS", 0.1)
        async with MockSarvamTTSServer() as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.connect()
            await asyncio.sleep(0.35)
            await provider.close()
        assert any(m.get("type") == "ping" for m in server.received)


class TestElevenLabsProvider:
    async def test_happy_path_contexts_and_final(self, monkeypatch):
        async with MockElevenLabsServer(chunks=2) as server:
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", server.url)
            provider = ElevenLabsWebSocketTTSProvider(eleven_settings())
            await provider.synthesize_stream("Hello there.", generation_id="g1")
            await provider.flush("g1")
            await provider.finish("g1")
            audio, errors, final = await collect_until_final(provider)
            await provider.close()

        assert final and not errors and audio == [PCM_CHUNK] * 2
        path = server.paths[0]
        assert "/v1/text-to-speech/f1abxvIEijusskcPWE5x/multi-stream-input" in path
        assert "model_id=eleven_flash_v2_5" in path
        assert "output_format=pcm_16000" in path
        assert "language_code=hi" in path
        init = server.inits[0]
        assert init["voice_settings"]["similarity_boost"] == 1.0
        assert init["generation_config"]["chunk_length_schedule"] == [120, 160]
        assert server.closed_contexts == ["g1"]

    async def test_snake_case_responses_parsed(self, monkeypatch):
        async with MockElevenLabsServer(behavior="snake_case", chunks=2) as server:
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", server.url)
            provider = ElevenLabsWebSocketTTSProvider(eleven_settings())
            await provider.synthesize_stream("Hello.", generation_id="g1")
            await provider.flush("g1")
            await provider.finish("g1")
            audio, errors, final = await collect_until_final(provider)
            await provider.close()
        assert final and audio == [PCM_CHUNK] * 2

    async def test_late_audio_after_cancel_is_rejected(self, monkeypatch):
        async with MockElevenLabsServer(behavior="late_after_close", chunks=2) as server:
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", server.url)
            provider = ElevenLabsWebSocketTTSProvider(eleven_settings())
            await provider.synthesize_stream("Long reply being spoken.", generation_id="g1")
            await provider.cancel("g1")  # server will still emit audio for g1
            await asyncio.sleep(0.3)
            # No audio events must surface for the cancelled generation.
            leaked = []
            while not provider.events.empty():
                event = provider.events.get_nowait()
                if event.kind == "audio":
                    leaked.append(event)
            await provider.close()
        assert server.closed_contexts == ["g1"]
        assert leaked == []

    async def test_auth_failure(self, monkeypatch):
        async with MockElevenLabsServer(behavior="auth_fail") as server:
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", server.url)
            provider = ElevenLabsWebSocketTTSProvider(eleven_settings())
            with pytest.raises(Exception):
                await provider.connect()
            event = await provider.events.get()
            assert event.kind == "error" and event.error.category == "auth"
            await provider.close()

    async def test_eleven_v3_rejected_before_connecting(self):
        """The ElevenLabs realtime WebSocket does not support eleven_v3 —
        the client refuses locally with a clear configuration error instead
        of producing a cryptic server-side close (and never silently falls
        back to another model)."""
        provider = ElevenLabsWebSocketTTSProvider(eleven_settings(model="eleven_v3"))
        with pytest.raises(ProviderError) as exc_info:
            await provider.connect()
        assert exc_info.value.category == "invalid_input"
        assert "eleven_v3" in str(exc_info.value)
        event = await provider.events.get()
        assert event.kind == "error" and event.error.category == "invalid_input"
        await provider.close()

    async def test_language_code_only_for_enforcing_models(self, monkeypatch):
        """language_code is a Flash/Turbo v2.5 parameter — other models must
        not receive it on the URL."""
        async with MockElevenLabsServer() as server:
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", server.url)
            provider = ElevenLabsWebSocketTTSProvider(eleven_settings())
            await provider.connect()
            await provider.close()
            assert "language_code=hi" in server.paths[0]

            other = ElevenLabsWebSocketTTSProvider(
                eleven_settings(model="eleven_multilingual_v2")
            )
            await other.connect()
            await other.close()
            assert "model_id=eleven_multilingual_v2" in server.paths[1]
            assert "language_code" not in server.paths[1]

    async def test_error_message_categorized_rate_limit(self, monkeypatch):
        async with MockElevenLabsServer(behavior="error_message") as server:
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", server.url)
            provider = ElevenLabsWebSocketTTSProvider(eleven_settings())
            await provider.synthesize_stream("Hi.", generation_id="g1")
            await provider.flush("g1")
            async with asyncio.timeout(3):
                while True:
                    event = await provider.events.get()
                    if event.kind == "error":
                        assert event.error.category == "rate_limit"
                        break
            await provider.close()

    async def test_multiple_generations_share_one_connection(self, monkeypatch):
        async with MockElevenLabsServer(chunks=1) as server:
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", server.url)
            provider = ElevenLabsWebSocketTTSProvider(eleven_settings())
            for generation in ("g1", "g2"):
                await provider.synthesize_stream("Sentence.", generation_id=generation)
                await provider.flush(generation)
                await provider.finish(generation)
                _, _, final = await collect_until_final(provider, generation=generation)
                assert final
            await provider.close()
        assert server.connections == 1

    async def test_voice_change_reconnects(self, monkeypatch):
        async with MockElevenLabsServer() as server:
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", server.url)
            provider = ElevenLabsWebSocketTTSProvider(eleven_settings())
            await provider.connect()
            await provider.configure(eleven_settings(voice="WQAp2s6GVJHv6IkTFqO0"))
            await provider.connect()
            await provider.close()
        assert server.connections == 2
        assert "WQAp2s6GVJHv6IkTFqO0" in server.paths[1]

    async def test_language_flip_on_non_enforcing_model_keeps_socket(self, monkeypatch):
        """language only reaches the URL for Flash/Turbo v2.5 — a language
        flip on any other model changes nothing on the wire and must not
        tear the connection down."""
        async with MockElevenLabsServer() as server:
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", server.url)
            provider = ElevenLabsWebSocketTTSProvider(
                eleven_settings(model="eleven_multilingual_v2")
            )
            await provider.connect()
            await provider.configure(
                eleven_settings(model="eleven_multilingual_v2", language="en-IN")
            )
            await provider.connect()
            await provider.close()
        assert server.connections == 1

    async def test_language_flip_on_enforcing_model_reconnects(self, monkeypatch):
        """Flash v2.5 carries language_code on the URL — a language flip
        changes the wire URL and must reconnect."""
        async with MockElevenLabsServer() as server:
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", server.url)
            provider = ElevenLabsWebSocketTTSProvider(eleven_settings())
            await provider.connect()
            await provider.configure(eleven_settings(language="en-IN"))
            await provider.connect()
            await provider.close()
        assert server.connections == 2
        assert "language_code=en" in server.paths[1]

    def test_event_queue_is_bounded(self):
        # A stalled consumer must backpressure the receive loop, not buffer
        # unbounded audio in memory.
        provider = ElevenLabsWebSocketTTSProvider(eleven_settings())
        assert provider.events.maxsize == 256


class TestCloseCategorization:
    """WS close → error category: configuration errors must never be treated
    as transient (no fallback), while exhausted credits must remain
    fallback-worthy."""

    def test_voice_does_not_exist_is_configuration(self):
        from shared.providers.tts.streaming import StreamingTTSProvider

        assert StreamingTTSProvider.categorize_close(
            1008, "A voice with voice_id VG7g… does not exist."
        ) == "invalid_input"

    def test_paid_plan_gate_is_configuration(self):
        from shared.providers.tts.streaming import StreamingTTSProvider

        assert StreamingTTSProvider.categorize_close(
            1008, "Free users cannot use this voice: paid plan required"
        ) == "invalid_input"

    def test_credits_exhausted_is_fallback_worthy(self):
        from shared.providers.tts.streaming import StreamingTTSProvider

        assert StreamingTTSProvider.categorize_close(
            1003, "Credits exhausted. Visit the API Dashboard…"
        ) == "rate_limit"

    def test_plain_1008_stays_rate_limited(self):
        from shared.providers.tts.streaming import StreamingTTSProvider

        assert StreamingTTSProvider.categorize_close(1008, "") == "rate_limit"

    def test_auth_still_wins(self):
        from shared.providers.tts.streaming import StreamingTTSProvider

        assert StreamingTTSProvider.categorize_close(4401, "bad key") == "auth"
