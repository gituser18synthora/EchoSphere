"""Sarvam TTS sockets: no cold handshake after a barge-in, at start-up, or unseen.

Sarvam cannot cancel synthesis server-side, so a barge-in closes the socket and
the next reply used to wait for a fresh handshake (measured against the API:
222-371 ms to first audio right after a cancel vs ~170 ms warm). These tests
pin the warm spare socket that is promoted on cancel, the ``cold_connect``
events a dispatch emits whenever it still has to wait (reason first /
after_cancel), the router's start-up connect (it only created the provider
since 2026-08-05, so every greeting paid the handshake) and its
``tts_cold_connect`` recording.
"""

import asyncio

import pytest

import shared.providers.tts.sarvam_ws as sarvam_ws
from shared.providers.tts.sarvam_ws import SarvamWebSocketTTSProvider
from shared.providers.tts.streaming import TTSStreamEvent
from tests.mock_tts_servers import MockSarvamTTSServer
from tests.unit.test_streaming_tts_providers import sarvam_settings
from tests.unit.test_tts_router_flush_finals import (
    KEY,
    FakeProvider,
    FakeRecorder,
    make_router,
)


async def _wait_for(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _drain(provider) -> list:
    events = []
    while not provider.events.empty():
        events.append(provider.events.get_nowait())
    return events


class TestColdConnectEvents:
    @pytest.fixture(autouse=True)
    def _no_warm_spare(self, monkeypatch):
        monkeypatch.setattr(sarvam_ws, "_WARM_SPARE", False)

    async def test_dispatch_on_a_closed_socket_reports_cold_connect(self, monkeypatch):
        async with MockSarvamTTSServer(behavior="silent") as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.synthesize_stream("First.", generation_id="g1")
            await provider.cancel("g1")
            # Dispatched before the background reconnect could finish.
            await provider.synthesize_stream("Second.", generation_id="g2")
            events = [e for e in _drain(provider) if e.kind == "cold_connect"]
            await provider.close()
        assert [(e.generation_id, e.info["reason"]) for e in events] == [
            ("g1", "first"), ("g2", "after_cancel"),
        ]
        assert all(e.info["waited_ms"] >= 0 for e in events)

    async def test_warm_dispatch_reports_nothing(self, monkeypatch):
        async with MockSarvamTTSServer(behavior="silent") as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.connect()
            await provider.synthesize_stream("Hello.", generation_id="g1")
            events = [e for e in _drain(provider) if e.kind == "cold_connect"]
            await provider.close()
        assert events == []


# ── warm spare socket ──────────────────────────────────────────────────────

class TestWarmSpare:
    @pytest.fixture(autouse=True)
    def _warm_spare_on(self, monkeypatch):
        monkeypatch.setattr(sarvam_ws, "_WARM_SPARE", True)

    async def test_spare_opens_configured_after_connect(self, monkeypatch):
        async with MockSarvamTTSServer() as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.connect()
            await _wait_for(lambda: provider._spare_ws is not None)
            await provider.close()
        assert server.connections == 2
        assert len(server.configs) == 2 and server.configs[0] == server.configs[1]

    async def test_cancel_promotes_the_spare_without_a_cold_connect(self, monkeypatch):
        async with MockSarvamTTSServer(behavior="silent") as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.synthesize_stream("First reply.", generation_id="g1")
            await _wait_for(lambda: provider._spare_ws is not None)
            spare = provider._spare_ws
            _drain(provider)
            await provider.cancel("g1")          # barge-in
            assert provider._ws is spare         # promoted at once
            await provider.synthesize_stream("Second reply.", generation_id="g2")
            cold = [e for e in _drain(provider) if e.kind == "cold_connect"]
            await _wait_for(lambda: server.connections == 3)  # a new spare
            await provider.close()
        assert cold == []
        assert server.texts() == ["First reply.", "Second reply."]

    async def test_promoted_spare_carries_a_voice_change(self, monkeypatch):
        async with MockSarvamTTSServer(behavior="silent") as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.synthesize_stream("First.", generation_id="g1")
            await _wait_for(lambda: provider._spare_ws is not None)
            await provider.configure(sarvam_settings(voice="ritu"))
            await provider.cancel("g1")
            await provider.synthesize_stream("Second.", generation_id="g2")
            await provider.close()
        assert server.configs[-1]["speaker"] == "ritu"

    async def test_without_a_ready_spare_cancel_still_reconnects(self, monkeypatch):
        async with MockSarvamTTSServer(behavior="silent") as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.synthesize_stream("First.", generation_id="g1")
            spare_task = provider._spare_task
            if spare_task is not None:            # spare not ready yet
                spare_task.cancel()
            provider._spare_ws = None
            await provider.cancel("g1")
            await provider.synthesize_stream("Second.", generation_id="g2")
            reasons = [e.info["reason"] for e in _drain(provider) if e.kind == "cold_connect"]
            await provider.close()
        assert reasons[-1] == "after_cancel"

    async def test_close_closes_the_spare(self, monkeypatch):
        async with MockSarvamTTSServer() as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings())
            await provider.connect()
            await _wait_for(lambda: provider._spare_ws is not None)
            spare = provider._spare_ws
            await provider.close()
            assert provider._spare_ws is None
            assert spare.state.name in ("CLOSING", "CLOSED")


# ── router ─────────────────────────────────────────────────────────────────

class TestRouter:
    async def test_cold_connect_is_recorded_and_counted(self):
        recorder = FakeRecorder()
        router = make_router(recorder=recorder)
        counted = []
        router._latency = type("T", (), {"count": lambda self, name: counted.append(name)})()
        await router._dispatch_event(KEY, TTSStreamEvent(
            kind="cold_connect", generation_id="ctx1",
            info={"waited_ms": 182.5, "reason": "after_cancel"},
        ))
        event = recorder.events[-1]
        assert event["kind"] == "tts_cold_connect"
        assert (event["waited_ms"], event["reason"], event["provider"]) == (182.5, "after_cancel", "sarvam")
        assert counted == ["tts_cold_connects"]

    async def test_start_opens_the_socket_in_the_background(self, monkeypatch):
        from pipecat.services.tts_service import TTSService

        async def parent_start(self, frame):
            return None

        monkeypatch.setattr(TTSService, "start", parent_start)
        router = make_router()
        provider = FakeProvider()

        async def get_provider(engine, locale):
            return provider

        router._get_provider = get_provider
        await router.start(object())
        await asyncio.gather(*router.tasks)
        assert ("connect",) in provider.calls
