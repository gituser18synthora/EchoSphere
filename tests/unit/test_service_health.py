"""Live platform-service probes behind the Platform Health card.

The invariant under test is the one the card promises an operator: a service
is reported healthy only when a probe actually succeeded, and every probe
target comes from the environment (``.env``) rather than a hardcoded port.
"""

import asyncio

import httpx
import pytest

from backend.core import service_health as sh


def _probe(**overrides) -> sh.ServiceProbe:
    base = {"name": "Test Service", "group": "platform",
            "host": "127.0.0.1", "port": 9999, "path": "/health"}
    return sh.ServiceProbe(**{**base, **overrides})


def _mock_http(monkeypatch, handler):
    monkeypatch.setattr(
        sh, "_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class TestRegistryComesFromEnv:
    def test_every_service_uses_its_configured_port(self, monkeypatch):
        settings = sh.get_settings()
        monkeypatch.setattr(settings, "api_port", 19001)
        monkeypatch.setattr(settings, "voice_worker_port", 19002)
        monkeypatch.setattr(settings, "mcp_port", 19003)
        monkeypatch.setattr(settings, "freeswitch_port", 19004)
        monkeypatch.setattr(settings, "telephony_gateway_port", 19011)

        ports = {p.name: p.port for p in sh.build_registry()}
        assert ports == {
            "Platform API": 19001, "Voice Worker": 19002, "MCP Server": 19003,
            "FreeSWITCH ESL": 19004, "Telephony gateway": 19011,
        }

    def test_platform_api_health_path_is_the_documented_one(self):
        api = next(p for p in sh.build_registry() if p.name == "Platform API")
        assert api.path == "/api/health"

    def test_freeswitch_is_probed_as_a_socket_not_http(self):
        esl = next(p for p in sh.build_registry() if p.name == "FreeSWITCH ESL")
        assert esl.path is None  # ESL is a raw TCP event socket

    @pytest.mark.parametrize("bind", ["0.0.0.0", "::", ""])
    def test_wildcard_bind_addresses_are_probed_on_loopback(self, bind):
        assert sh._reachable_host(bind) == "127.0.0.1"

    def test_a_real_host_is_left_alone(self):
        assert sh._reachable_host("10.1.2.3") == "10.1.2.3"

    def test_every_service_declares_a_monitoring_group(self):
        assert all(p.group in ("platform", "ai", "telephony")
                   for p in sh.build_registry())


class TestHttpProbe:
    async def test_reports_healthy_only_on_a_successful_probe(self, monkeypatch):
        _mock_http(monkeypatch, lambda r: httpx.Response(200, json={"status": "up"}))
        result = await sh.probe_service(_probe())
        assert result["status"] == "good"
        assert result["value"].endswith("ms")
        assert result["target"] == "127.0.0.1:9999"

    async def test_envelope_wrapped_status_is_understood(self, monkeypatch):
        # The platform API wraps its body in the standard {success, data} envelope.
        _mock_http(monkeypatch, lambda r: httpx.Response(
            200, json={"success": True, "data": {"status": "up", "env": "development"}}))
        assert (await sh.probe_service(_probe()))["status"] == "good"

    async def test_reachable_but_not_up_is_degraded_not_healthy(self, monkeypatch):
        _mock_http(monkeypatch, lambda r: httpx.Response(200, json={"status": "starting"}))
        result = await sh.probe_service(_probe())
        assert result["status"] == "warning"
        assert "status=starting" in result["detail"]

    async def test_non_json_body_is_degraded(self, monkeypatch):
        _mock_http(monkeypatch, lambda r: httpx.Response(200, text="OK"))
        assert (await sh.probe_service(_probe()))["status"] == "warning"

    @pytest.mark.parametrize("code", [404, 401, 500, 502])
    async def test_error_responses_are_critical(self, monkeypatch, code):
        _mock_http(monkeypatch, lambda r: httpx.Response(code))
        result = await sh.probe_service(_probe())
        assert result["status"] == "critical"
        assert result["value"] == "Unreachable"

    async def test_connection_failure_is_critical_and_never_raises(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        _mock_http(monkeypatch, handler)
        result = await sh.probe_service(_probe())
        assert result["status"] == "critical"
        assert "ConnectError" in result["detail"]

    async def test_the_probed_url_is_reported_for_debugging(self, monkeypatch):
        _mock_http(monkeypatch, lambda r: httpx.Response(200, json={"status": "up"}))
        result = await sh.probe_service(_probe(port=9002, path="/health"))
        assert "http://127.0.0.1:9002/health" in result["detail"]


class TestTcpProbe:
    async def test_a_listening_socket_is_healthy(self):
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            result = await sh.probe_service(
                _probe(name="FreeSWITCH ESL", port=port, path=None))
        finally:
            server.close()
            await server.wait_closed()
        assert result["status"] == "good"
        assert "event socket accepted" in result["detail"]

    async def test_a_closed_port_is_critical(self):
        # Bind then release, so the port is known-free rather than guessed.
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()

        result = await sh.probe_service(
            _probe(name="FreeSWITCH ESL", port=port, path=None))
        assert result["status"] == "critical"
        assert result["value"] == "Unreachable"


class TestSelfProbe:
    async def test_the_serving_process_reports_itself_up(self):
        result = await sh.probe_service(_probe(is_self=True))
        assert result["status"] == "good" and result["value"] == "Up"


class TestProbeServices:
    async def test_returns_one_row_per_registered_service_in_order(self, monkeypatch):
        _mock_http(monkeypatch, lambda r: httpx.Response(200, json={"status": "up"}))
        rows = await sh.probe_services()
        assert [r["name"] for r in rows] == [p.name for p in sh.build_registry()]
        assert all({"name", "status", "value", "target", "spark", "group", "detail"}
                   <= set(r) for r in rows)

    async def test_one_dead_service_does_not_hide_the_healthy_ones(self, monkeypatch):
        def handler(request):
            if ":9002" in str(request.url):
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"status": "up"})

        _mock_http(monkeypatch, handler)
        rows = {r["name"]: r["status"] for r in await sh.probe_services()}
        assert rows["Voice Worker"] == "critical"
        assert rows["MCP Server"] == "good"
        assert rows["Platform API"] == "good"

    async def test_no_row_reports_a_stale_placeholder_value(self, monkeypatch):
        # The old seeded rows claimed things like "100% uptime" with no probe.
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        _mock_http(monkeypatch, handler)
        rows = await sh.probe_services()
        for row in rows:
            if row["name"] == "Platform API":
                continue  # in-process, verified by serving the request
            assert row["status"] == "critical"
            assert row["value"] == "Unreachable"
