"""Sarvam TTS sockets: no cold handshake after a barge-in, at start-up, or unseen.

Sarvam cannot cancel synthesis server-side, so a barge-in closes the socket and
the next reply used to wait for a fresh handshake (measured against the API:
222-371 ms to first audio right after a cancel vs ~170 ms warm). These tests
pin the warm spare socket that is promoted on cancel, the ``cold_connect``
events a dispatch emits whenever it still has to wait (reason first /
after_cancel), the router's start-up connect (it only created the provider
since 2026-08-05, so every greeting paid the handshake; then it connected in
the background and the greeting, dispatched meanwhile, still logged a
"first" cold connect on 38/38 live calls — now it starts in setup() and the
StartFrame waits for it, bounded) and its ``tts_cold_connect`` recording.
"""

import asyncio

import pytest

import shared.providers.tts.sarvam_ws as sarvam_ws
import voice_runtime.tts_router as tts_router
from shared.providers.tts.sarvam_ws import SarvamWebSocketTTSProvider
from shared.providers.tts.streaming import TTSStreamEvent
from tests.mock_tts_servers import API_KEY, MockSarvamTTSServer
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


def _with_cancel(router):
    """make_router routes create_task to plain asyncio; cancel_task likewise."""
    async def cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    router.cancel_task = cancel_task
    return router


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

    @pytest.fixture
    def _bare_parents(self, monkeypatch):
        """TTSService's own setup/start need a live pipeline; the router's
        start-up logic runs on top of them."""
        from pipecat.services.tts_service import TTSService

        async def parent(self, arg):
            return None

        monkeypatch.setattr(TTSService, "setup", parent)
        monkeypatch.setattr(TTSService, "start", parent)

    def _router(self, provider, *, recorder=None):
        router = make_router(recorder=recorder)

        async def get_provider(engine, locale):
            return provider

        router._get_provider = get_provider
        return _with_cancel(router)

    async def test_setup_starts_the_connect_before_the_start_frame(self, _bare_parents):
        provider = FakeProvider()
        router = self._router(provider)
        await router.setup(object())
        assert router._startup_connect is not None
        await _wait_for(lambda: ("connect",) in provider.calls)
        await router.start(object())
        assert provider.calls.count(("connect",)) == 1   # start reuses it

    async def test_start_holds_until_the_socket_is_open(self, _bare_parents):
        class Slow(FakeProvider):
            async def connect(self):
                await asyncio.sleep(0.15)
                await super().connect()

        recorder = FakeRecorder()
        provider = Slow()
        router = self._router(provider, recorder=recorder)
        await router.setup(object())
        await router.start(object())
        # The greeting's frames queue behind this StartFrame: by the time
        # they are processed the socket is open.
        assert ("connect",) in provider.calls
        event = next(e for e in recorder.events if e["kind"] == "tts_startup_connect")
        assert event["timed_out"] is False and event["held_ms"] >= 100

    async def test_a_slow_handshake_stops_holding_the_start(self, _bare_parents, monkeypatch):
        monkeypatch.setattr(tts_router, "_STARTUP_CONNECT_WAIT_S", 0.05)

        class Hung(FakeProvider):
            async def connect(self):
                await asyncio.sleep(1)
                await super().connect()

        recorder = FakeRecorder()
        router = self._router(Hung(), recorder=recorder)
        await router.setup(object())
        await router.start(object())
        event = next(e for e in recorder.events if e["kind"] == "tts_startup_connect")
        assert event["timed_out"] is True and event["held_ms"] < 500
        assert not router._startup_connect.done()        # still connecting, not cancelled
        await router._shutdown_providers()
        assert router._startup_connect is None

    async def test_start_without_setup_still_connects(self, _bare_parents):
        provider = FakeProvider()
        router = self._router(provider)
        await router.start(object())
        assert ("connect",) in provider.calls

    async def test_the_greeting_finds_an_open_socket(self, _bare_parents, monkeypatch):
        """End to end with the real Sarvam adapter: setup() + start(), then the
        greeting's synthesis reports no cold connect."""
        monkeypatch.setattr(sarvam_ws, "_WARM_SPARE", False)
        monkeypatch.setenv("SARVAM_UNIT_TEST_KEY", API_KEY)
        async with MockSarvamTTSServer(behavior="silent") as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            router = _with_cancel(make_router())
            router._default_engine["api_key_reference"] = "env:SARVAM_UNIT_TEST_KEY"
            await router.setup(object())
            await router.start(object())
            engine = router._engine_for_language("hi-IN")
            provider = await router._get_provider(engine, "hi-IN")
            await provider.synthesize_stream("Namaste.", generation_id="greeting")
            cold = [e for e in _drain(provider) if e.kind == "cold_connect"]
            await router._shutdown_providers()
        assert cold == []
        assert server.connections == 1
