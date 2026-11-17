"""Isolated checks for measured monitoring health and honest failure states."""

import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from backend.core import service_health as sh
from shared.db import health as db_health
from shared.providers.base import ProviderError
from voice_runtime.provider_health import ProviderHealth


def _probe(**overrides) -> sh.ServiceProbe:
    base = {"name": "Test Service", "group": "platform", "host": "127.0.0.1", "port": 9999, "path": "/health"}
    return sh.ServiceProbe(**{**base, **overrides})


def _mock_http(monkeypatch, handler):
    monkeypatch.setattr(sh, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class TestRegistryComesFromEnv:
    def test_every_service_uses_its_configured_port(self, monkeypatch):
        settings = sh.get_settings()
        names = ("api", "voice_worker", "mcp", "freeswitch", "telephony_gateway", "mysql", "postgres")
        for index, name in enumerate(names):
            monkeypatch.setattr(settings, f"{name}_port", 19001 + index)
        ports = {probe.name: probe.port for probe in sh.build_registry()}
        assert {name: ports[name] for name in ("Platform API", "Voice Worker", "MCP Server", "FreeSWITCH ESL", "Telephony gateway", "MySQL", "PostgreSQL")} == {
            "Platform API": 19001, "Voice Worker": 19002, "MCP Server": 19003,
            "FreeSWITCH ESL": 19004, "Telephony gateway": 19005, "MySQL": 19006, "PostgreSQL": 19007,
        }

    def test_credential_bearing_connection_urls_never_become_targets(self, monkeypatch):
        settings = sh.get_settings()
        monkeypatch.setattr(settings, "redis_url", "redis://operator:private@redis.internal:6380/1?password=hidden")
        monkeypatch.setattr(settings, "mongodb_uri", "mongodb://operator:private@mongo.internal:27018/private")
        registry = {probe.name: probe for probe in sh.build_registry()}
        assert (registry["Redis"].host, registry["Redis"].port) == ("redis.internal", 6380)
        assert (registry["MongoDB"].host, registry["MongoDB"].port) == ("mongo.internal", 27018)
        assert "private" not in repr(sh.build_registry())

    def test_platform_api_health_path_is_the_documented_one(self):
        assert next(probe for probe in sh.build_registry() if probe.name == "Platform API").path == "/api/health"

    def test_freeswitch_uses_authenticated_esl(self):
        esl = next(probe for probe in sh.build_registry() if probe.name == "FreeSWITCH ESL")
        assert esl.path is None and esl.kind == "esl"

    @pytest.mark.parametrize("bind", ["0.0.0.0", "::", ""])
    def test_wildcard_bind_addresses_are_probed_on_loopback(self, bind):
        assert sh._reachable_host(bind) == "127.0.0.1"

    def test_a_real_host_is_left_alone(self):
        assert sh._reachable_host("10.1.2.3") == "10.1.2.3"

    def test_every_service_declares_a_monitoring_group(self):
        assert all(probe.group in ("platform", "ai", "telephony") for probe in sh.build_registry())


class TestHttpProbe:
    async def test_reports_healthy_only_on_a_successful_probe(self, monkeypatch):
        _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"status": "up"}))
        result = await sh.probe_service(_probe())
        assert result["status"] == "good"
        assert result["value"].endswith("ms")
        assert result["target"] == "127.0.0.1:9999"
        assert result["key"] == "test-service" and result["checkedAt"].endswith("+00:00")
        assert isinstance(result["latencyMs"], float)

    async def test_envelope_wrapped_status_is_understood(self, monkeypatch):
        _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"success": True, "data": {"status": "up", "env": "development"}}))
        assert (await sh.probe_service(_probe()))["status"] == "good"

    @pytest.mark.parametrize("dependency", ["redis", "postgres", "mongodb", "mysql"])
    async def test_nested_dependency_failures_override_top_level_up(self, monkeypatch, dependency):
        _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"status": "up", dependency: {"ok": False}}))
        result = await sh.probe_service(_probe())
        assert result["status"] == "critical"
        assert f"{dependency} unavailable" in result["detail"]

    async def test_checks_envelope_failures_are_not_ignored(self, monkeypatch):
        _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"status": "up", "checks": {"redis": {"ok": False}}}))
        assert (await sh.probe_service(_probe()))["status"] == "critical"

    async def test_worker_observations_are_exposed_and_outage_is_critical(self, monkeypatch):
        payload = {"status": "up", "active_sessions": 4, "capacity": 12,
                   "providers": {"status": "down", "observed_calls": 3, "outage": {"provider": "openai-llm"}}}
        _mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
        result = await sh.probe_service(_probe())
        assert result["status"] == "critical"
        assert result["observations"]["activeSessions"] == 4
        assert result["observations"]["capacity"] == 12
        assert "openai-llm" in result["detail"]

    async def test_unobserved_provider_health_is_explicit(self, monkeypatch):
        _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"status": "up", "providers": {"status": "unknown", "observed_calls": 0}}))
        result = await sh.probe_service(_probe())
        assert result["observations"]["providerStatus"] == "unknown"
        assert "no call outcomes yet" in result["detail"]

    async def test_recent_provider_failures_are_degraded_before_outage_threshold(self, monkeypatch):
        _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"status": "up", "providers": {"status": "degraded"}}))
        assert (await sh.probe_service(_probe()))["status"] == "warning"

    async def test_reachable_but_not_up_is_degraded(self, monkeypatch):
        _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"status": "starting"}))
        result = await sh.probe_service(_probe())
        assert result["status"] == "warning" and "status=starting" in result["detail"]

    async def test_non_json_body_is_degraded(self, monkeypatch):
        _mock_http(monkeypatch, lambda request: httpx.Response(200, text="OK"))
        assert (await sh.probe_service(_probe()))["status"] == "warning"

    @pytest.mark.parametrize("code", [404, 401, 500, 502])
    async def test_error_responses_are_critical(self, monkeypatch, code):
        _mock_http(monkeypatch, lambda request: httpx.Response(code))
        result = await sh.probe_service(_probe())
        assert result["status"] == "critical" and result["value"] == "Unreachable"

    async def test_connection_failure_is_critical_and_never_raises(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)
        _mock_http(monkeypatch, handler)
        result = await sh.probe_service(_probe())
        assert result["status"] == "critical" and "ConnectError" in result["detail"]

    async def test_the_probed_url_is_reported(self, monkeypatch):
        _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"status": "up"}))
        assert "http://127.0.0.1:9002/health" in (await sh.probe_service(_probe(port=9002)))["detail"]


@asynccontextmanager
async def _esl_server(*, accept=True, status="UP 1 day", calls="3 total.", sofia="external profile sip:masked RUNNING (0)\nexternal::trunk gateway sip:masked REGED"):
    """A loopback fixture only; no connection to configured FreeSWITCH."""
    commands, writers = [], set()

    async def serve(reader, writer):
        writers.add(writer)
        try:
            writer.write(b"Content-Type: auth/request\n\n")
            await writer.drain()
            auth = await reader.readuntil(b"\n\n")
            assert auth == b"auth test-esl-password\n\n"
            writer.write(b"Content-Type: command/reply\nReply-Text: +OK accepted\n\n" if accept else b"Content-Type: command/reply\nReply-Text: -ERR rejected\n\n")
            await writer.drain()
            if not accept:
                return
            while True:
                command = (await reader.readuntil(b"\n\n")).decode().strip().removeprefix("api ")
                commands.append(command)
                response = {"status": status, "show calls count": calls, "sofia status": sofia}[command].encode()
                writer.write(f"Content-Type: api/response\nContent-Length: {len(response)}\n\n".encode() + response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            writers.discard(writer)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], commands
    finally:
        server.close()
        for writer in tuple(writers):
            writer.close()
            await writer.wait_closed()
        await server.wait_closed()


class TestEslProbe:
    @pytest.fixture(autouse=True)
    def test_password(self, monkeypatch):
        monkeypatch.setattr(sh.get_settings(), "freeswitch_password_reference", "test-esl-password")

    async def test_authenticates_and_queries_only_read_only_commands(self):
        async with _esl_server() as (port, commands):
            result = await sh.probe_service(_probe(name="FreeSWITCH ESL", path=None, port=port))
        assert result["status"] == "good"
        assert commands == ["status", "show calls count", "sofia status"]
        assert result["observations"] == {"sipProfiles": 1, "sipGateways": 1, "registeredGateways": 1, "activeCalls": 3}
        assert "masked" not in json.dumps(result) and "test-esl-password" not in json.dumps(result)

    async def test_rejected_authentication_never_reports_healthy(self):
        async with _esl_server(accept=False) as (port, commands):
            result = await sh.probe_service(_probe(path=None, port=port))
        assert result["status"] == "critical" and commands == []

    async def test_unregistered_gateway_is_degraded(self):
        async with _esl_server(sofia="external profile sip:masked RUNNING (0)\ntrunk gateway sip:masked FAIL_WAIT") as (port, _):
            result = await sh.probe_service(_probe(path=None, port=port))
        assert result["status"] == "warning" and "registration is not ready" in result["detail"]

    async def test_nonregistering_gateway_is_not_a_false_failure(self):
        async with _esl_server(sofia="external profile sip:masked RUNNING (0)\ntrunk gateway sip:masked NOREG") as (port, _):
            result = await sh.probe_service(_probe(path=None, port=port))
        assert result["status"] == "good" and result["observations"]["registeredGateways"] == 0

    async def test_status_failure_stops_further_queries(self):
        async with _esl_server(status="-ERR not available") as (port, commands):
            result = await sh.probe_service(_probe(path=None, port=port))
        assert result["status"] == "critical" and commands == ["status"]

    async def test_missing_sofia_and_call_count_are_explicit(self):
        async with _esl_server(calls="-ERR invalid", sofia="-ERR no sofia") as (port, _):
            result = await sh.probe_service(_probe(path=None, port=port))
        assert result["status"] == "warning"
        assert "Active call count unavailable" in result["detail"] and "SIP profile status unavailable" in result["detail"]

    async def test_open_tcp_port_alone_is_not_healthy(self, monkeypatch):
        monkeypatch.setattr(sh, "PROBE_TIMEOUT_SECONDS", 0.02)
        writers = []
        def serve(reader, writer):
            writers.append(writer)
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        try:
            result = await sh.probe_service(_probe(path=None, port=server.sockets[0].getsockname()[1]))
        finally:
            server.close()
            for writer in writers:
                writer.close()
                await writer.wait_closed()
            await server.wait_closed()
        assert result["status"] == "critical"

    async def test_closed_port_is_critical(self):
        server = await asyncio.start_server(lambda reader, writer: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        assert (await sh.probe_service(_probe(path=None, port=port)))["status"] == "critical"


class TestBackingServices:
    async def test_probes_use_existing_shared_readiness_result(self, monkeypatch):
        async def check(name):
            assert name == "redis"
            return {"ok": False, "error": "ConnectionError"}
        monkeypatch.setattr(sh, "check_backing_service", check)
        result = await sh.probe_service(_probe(kind="redis"))
        assert result["status"] == "critical" and "ConnectionError" in result["detail"]

    async def test_each_backing_check_has_a_deadline_and_runs_concurrently(self, monkeypatch):
        monkeypatch.setattr(db_health, "CHECK_TIMEOUT_SECONDS", 0.03)
        async def check(name):
            await asyncio.Event().wait()
        monkeypatch.setattr(db_health, "_check", check)
        started = time.perf_counter()
        result = await db_health.check_backing_services()
        assert time.perf_counter() - started < 0.1
        assert all(value == {"ok": False, "error": "TimeoutError"} for value in result.values())

    async def test_failed_dependency_does_not_discard_other_results(self, monkeypatch):
        async def check(name):
            if name == "mysql":
                raise RuntimeError("password=never expose this")
            return {"ok": True}
        monkeypatch.setattr(db_health, "_check", check)
        result = await db_health.check_backing_services()
        assert result["mysql"] == {"ok": False, "error": "RuntimeError"}
        assert result["redis"] == {"ok": True}
        assert "password" not in json.dumps(result)

    async def test_mysql_check_is_a_read_only_select(self, monkeypatch):
        statements = []
        class Connection:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def execute(self, statement):
                statements.append(str(statement))
        class Engine:
            def connect(self):
                return Connection()
        monkeypatch.setattr(db_health, "get_engine", lambda: Engine())
        assert await db_health.check_backing_service("mysql") == {"ok": True}
        assert statements == ["SELECT 1"]


class TestResources:
    @pytest.mark.parametrize("percent,expected", [(79, "good"), (80, "warning"), (95, "critical")])
    async def test_memory_thresholds(self, monkeypatch, percent, expected):
        monkeypatch.setattr(sh, "_read_memory", lambda: (1000, (100 - percent) * 10))
        result = await sh.probe_service(_probe(kind="memory"))
        assert result["status"] == expected and result["value"] == f"{percent:.1f}%"
        assert result["observations"]["usedPercent"] == percent

    async def test_load_is_labelled_load_average_not_cpu_usage(self, monkeypatch):
        monkeypatch.setattr(sh.os, "getloadavg", lambda: (6.0, 4.0, 3.0))
        monkeypatch.setattr(sh.os, "cpu_count", lambda: 2)
        result = await sh.probe_service(_probe(kind="load"))
        assert result["status"] == "critical" and result["value"] == "6.00"
        assert "load average" in result["detail"] and "2 logical CPUs" in result["detail"]
        assert result["observations"] == {"load1m": 6.0, "logicalCpus": 2}

    async def test_unsupported_resource_probe_is_unavailable(self, monkeypatch):
        def unavailable():
            raise OSError("private path")
        monkeypatch.setattr(sh, "_read_memory", unavailable)
        result = await sh.probe_service(_probe(kind="memory"))
        assert result["status"] == "warning" and result["value"] == "Unavailable"
        assert "private path" not in result["detail"]


class TestPassiveProviderSnapshot:
    def test_no_calls_means_unknown_and_does_not_claim_provider_success(self):
        health = ProviderHealth()
        assert health.snapshot()["status"] == "unknown"
        assert health.snapshot()["observed_calls"] == 0

    def test_failures_outage_and_recovery_are_sanitized_and_observed(self):
        health = ProviderHealth()
        error = ProviderError("example-llm", "auth", "credential=private", status=401)
        health.failed("one", error, stage="warmup")
        assert health.snapshot()["status"] == "degraded"
        health.failed("two", error, stage="warmup")
        health.failed("three", error, stage="warmup")
        snapshot = health.snapshot()
        assert snapshot["status"] == "down" and snapshot["outage"]["provider"] == "example-llm"
        assert snapshot["last_observed_at"] and snapshot["outage"]["failed_calls"] == 3
        assert "credential" not in json.dumps(snapshot) and "private" not in json.dumps(snapshot)
        health.succeeded("four")
        assert health.snapshot()["status"] == "up" and health.snapshot()["outage"] is None


class TestSelfProbe:
    async def test_serving_process_reports_liveness_only(self):
        result = await sh.probe_service(_probe(is_self=True))
        assert result["status"] == "good" and result["value"] == "Up"
        assert result["latencyMs"] is None


class TestProbeServices:
    @pytest.fixture(autouse=True)
    def isolate_dependencies(self, monkeypatch):
        async def check(name):
            return {"ok": True}
        async def esl(probe):
            return sh._metric(probe, "critical", "Unavailable", "fixture ESL unavailable")
        async def resource(probe):
            return sh._metric(probe, "good", "50%", "fixture host reading")
        monkeypatch.setattr(sh, "check_backing_service", check)
        monkeypatch.setattr(sh, "_probe_esl", esl)
        monkeypatch.setattr(sh, "_probe_resource", resource)
        async def ai():
            return []
        monkeypatch.setattr(sh, "probe_ai_latency", ai)

    async def test_returns_every_registered_service_with_stable_contract(self, monkeypatch):
        _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"status": "up"}))
        rows = await sh.probe_services()
        assert [row["name"] for row in rows] == [probe.name for probe in sh.build_registry()]
        assert len({row["key"] for row in rows}) == len(rows)
        assert all({"key", "name", "status", "value", "target", "spark", "group", "detail", "checkedAt", "latencyMs"} <= set(row) for row in rows)

    async def test_dead_service_does_not_hide_healthy_ones(self, monkeypatch):
        def handler(request):
            if ":9002" in str(request.url):
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"status": "up"})
        _mock_http(monkeypatch, handler)
        rows = {row["name"]: row["status"] for row in await sh.probe_services()}
        assert rows["Voice Worker"] == "critical" and rows["MCP Server"] == "good" and rows["Platform API"] == "good"

    async def test_dedicated_collector_checks_api_over_http(self, monkeypatch):
        seen = []
        def handler(request):
            seen.append(str(request.url))
            raise httpx.ConnectError("refused", request=request)
        _mock_http(monkeypatch, handler)
        rows = {row["name"]: row for row in await sh.probe_services(in_process_api=False)}
        assert rows["Platform API"]["status"] == "critical"
        assert any(url.endswith("/api/health") for url in seen)

    async def test_embedded_collector_does_not_make_circular_api_http_request(self, monkeypatch):
        seen = []
        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(503)
        _mock_http(monkeypatch, handler)
        rows = {row["name"]: row for row in await sh.probe_services(in_process_api=True)}
        assert rows["Platform API"]["status"] == "good"
        assert not any(url.endswith("/api/health") for url in seen)

    async def test_no_row_reports_seeded_uptime(self, monkeypatch):
        _mock_http(monkeypatch, lambda request: httpx.Response(503))
        assert all("100% uptime" not in row["value"] for row in await sh.probe_services())

    async def test_passive_ai_cards_are_included_with_service_checks(self, monkeypatch):
        _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"status": "up"}))
        async def ai():
            return [{"key": "stt-finalization", "name": "STT finalization", "status": "neutral", "group": "ai"}]
        monkeypatch.setattr(sh, "probe_ai_latency", ai)
        rows = await sh.probe_services()
        assert rows[-1]["key"] == "stt-finalization"
        assert len(rows) == len(sh.build_registry()) + 1
