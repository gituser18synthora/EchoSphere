"""Measured service, dependency, host-resource and telephony health.

Targets come from settings. Read-only probes are bounded and run concurrently;
there are no provider test requests, calls, database writes or seeded successes.
"""

import asyncio
import os
import re
import shutil
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from shared.config import get_settings
from shared.db.health import BACKING_SERVICES, check_backing_service
from backend.core.ai_health import probe_ai_latency

PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class ServiceProbe:
    name: str
    group: str
    host: str
    port: int
    path: str | None = None
    is_self: bool = False
    kind: str = "http"
    target: str | None = None


def _reachable_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def _connection_target(url: str, default_port: int) -> tuple[str, int]:
    """Only display host/port, never credentials, query strings or database names."""
    try:
        parsed = urlsplit(url)
        return parsed.hostname or "configured host", parsed.port or default_port
    except ValueError:
        # Mongo replica-set URLs can contain several hosts; avoid displaying
        # the credential-bearing URL when urllib cannot parse that authority.
        return "configured hosts", default_port


def build_registry() -> list[ServiceProbe]:
    settings = get_settings()
    redis_host, redis_port = _connection_target(settings.redis_url, 6379)
    mongo_host, mongo_port = _connection_target(settings.mongodb_uri, 27017)
    return [
        ServiceProbe("Platform API", "platform", _reachable_host(settings.api_host),
                     settings.api_port, "/api/health", is_self=True),
        ServiceProbe("Voice Worker", "ai", _reachable_host(settings.voice_worker_host),
                     settings.voice_worker_port, "/health"),
        ServiceProbe("MCP Server", "platform", _reachable_host(settings.mcp_host),
                     settings.mcp_port, "/health"),
        ServiceProbe("FreeSWITCH ESL", "telephony", _reachable_host(settings.freeswitch_host),
                     settings.freeswitch_port, kind="esl"),
        ServiceProbe("Telephony gateway", "telephony", _reachable_host(settings.telephony_gateway_host),
                     settings.telephony_gateway_port, "/health"),
        ServiceProbe("MySQL", "platform", settings.mysql_host, settings.mysql_port, kind="mysql"),
        ServiceProbe("PostgreSQL", "platform", settings.postgres_host, settings.postgres_port, kind="postgres"),
        ServiceProbe("Redis", "platform", redis_host, redis_port, kind="redis"),
        ServiceProbe("MongoDB", "platform", mongo_host, mongo_port, kind="mongodb"),
        ServiceProbe("Host memory", "platform", "API host", 0, kind="memory", target="API host"),
        ServiceProbe("Host disk", "platform", "API host", 0, kind="disk", target="API host /"),
        ServiceProbe("Host load average", "platform", "API host", 0, kind="load", target="API host"),
    ]


def _payload_data(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    return payload["data"] if isinstance(payload.get("data"), dict) else payload


def _payload_status(payload: object) -> str | None:
    status = _payload_data(payload).get("status")
    return status if isinstance(status, str) else None


def _metric(probe: ServiceProbe, status: str, value: str, detail: str,
            *, latency_ms: float | None = None, observations: dict | None = None) -> dict:
    return {
        "key": re.sub(r"[^a-z0-9]+", "-", probe.name.lower()).strip("-")[:96],
        "name": probe.name,
        "status": status,
        "value": value,
        "target": probe.target or f"{probe.host}:{probe.port}",
        "spark": [],
        "group": probe.group,
        "detail": detail,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "latencyMs": round(latency_ms, 2) if latency_ms is not None else None,
        "observations": observations or {},
    }


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS)


def _payload_observations(data: dict) -> tuple[list[str], list[str], dict]:
    failures, notes, observations = [], [], {}
    checks = dict(data.get("checks", {})) if isinstance(data.get("checks"), dict) else {}
    for name in BACKING_SERVICES:
        if isinstance(data.get(name), dict):
            checks[name] = data[name]
    for name, check in checks.items():
        if isinstance(check, dict) and check.get("ok") is False:
            failures.append(f"{name} unavailable")
    if isinstance(data.get("active_sessions"), int):
        observations["activeSessions"] = data["active_sessions"]
        notes.append(f"Active sessions: {data['active_sessions']}")
    if isinstance(data.get("capacity"), int):
        observations["capacity"] = data["capacity"]
    providers = data.get("providers")
    if isinstance(providers, dict):
        state = providers.get("status", "unknown")
        observations["providerStatus"] = state
        observations["providerCoverage"] = providers.get("coverage", "LLM first outcome per call")
        observations["providerObservedCalls"] = providers.get("observed_calls", 0)
        observations["providerLastObservedAt"] = providers.get("last_observed_at")
        outage = providers.get("outage")
        if state == "down":
            provider = outage.get("provider", "runtime provider") if isinstance(outage, dict) else "runtime provider"
            failures.append(f"LLM provider outage: {provider}")
        elif state == "degraded":
            notes.append("Recent LLM provider failures (passive call observations)")
        elif state == "unknown":
            notes.append("LLM provider health unobserved; no call outcomes yet; STT/TTS not observed")
        else:
            notes.append("LLM provider signal: last observed calls, process-local; STT/TTS not observed")
        if isinstance(providers.get("last_observed_at"), str):
            notes.append(f"Last LLM observation: {providers['last_observed_at']}")
    return failures, notes, observations


async def _probe_http(probe: ServiceProbe) -> dict:
    host = f"[{probe.host}]" if ":" in probe.host else probe.host
    url = f"http://{host}:{probe.port}{probe.path}"
    started = time.perf_counter()
    try:
        async with _http_client() as client:
            response = await client.get(url)
    except Exception as exc:  # noqa: BLE001
        return _metric(probe, "critical", "Unreachable", f"{url} — {exc.__class__.__name__}")
    elapsed = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        return _metric(probe, "critical", "Unreachable", f"{url} → HTTP {response.status_code}", latency_ms=elapsed)
    try:
        data = _payload_data(response.json())
    except ValueError:
        data = {}
    reported = _payload_status(data)
    failures, notes, observations = _payload_observations(data)
    status = "good" if reported == "up" else "warning"
    if failures or reported in ("down", "critical", "error", "failed"):
        status = "critical"
    elif observations.get("providerStatus") == "degraded":
        status = "warning"
    summary = f"{url} → 200"
    if reported != "up":
        summary += f", status={reported or 'unknown'}"
    detail = "; ".join([summary, *failures, *notes])
    return _metric(probe, status, f"{elapsed:.0f} ms", detail, latency_ms=elapsed, observations=observations)


def _sofia_summary(output: str) -> tuple[dict, list[str]]:
    """Read the summary table; don't collect addresses, gateway names or credentials."""
    profiles = registered = gateways = 0
    failures = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        if fields[1].lower() == "profile":
            profiles += 1
            if not any(value.upper() == "RUNNING" for value in fields[2:]):
                failures.append("SIP profile is not running")
        elif fields[1].lower() == "gateway":
            gateways += 1
            state = fields[-1].upper()
            if state == "REGED":
                registered += 1
            elif state not in ("NOREG", "UP"):
                failures.append("SIP gateway registration is not ready")
    if "-ERR" in output or not profiles:
        failures.append("SIP profile status unavailable")
    return {"sipProfiles": profiles, "sipGateways": gateways, "registeredGateways": registered}, failures


async def _probe_esl(probe: ServiceProbe) -> dict:
    from voice_runtime.freeswitch import ESLClient

    started = time.perf_counter()
    client = None
    try:
        client = ESLClient(host=probe.host, port=probe.port, timeout=PROBE_TIMEOUT_SECONDS)
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            health = await client.health_check()
            if not health.get("ok"):
                # The existing client authenticates before `status`; an open
                # TCP port or rejected credential is never reported healthy.
                return _metric(probe, "critical", "Unavailable", "ESL authentication/status check failed")
            calls = await client.api("show calls count")
            sofia = await client.api("sofia status")
        observations, warnings = _sofia_summary(sofia)
        match = re.search(r"\b(\d+)\s+total\b", calls)
        if match:
            observations["activeCalls"] = int(match.group(1))
        else:
            warnings.append("Active call count unavailable")
        elapsed = (time.perf_counter() - started) * 1000
        detail = "Authenticated ESL; FreeSWITCH status UP"
        if match:
            detail += f"; active calls: {observations['activeCalls']}"
        detail += f"; SIP profiles: {observations['sipProfiles']}; registered gateways: {observations['registeredGateways']}/{observations['sipGateways']}"
        if warnings:
            detail += "; " + "; ".join(dict.fromkeys(warnings))
        return _metric(probe, "warning" if warnings else "good", f"{elapsed:.0f} ms", detail,
                       latency_ms=elapsed, observations=observations)
    except Exception as exc:  # noqa: BLE001 — no provider exception message/secret in health payload
        category = getattr(exc, "category", exc.__class__.__name__)
        return _metric(probe, "critical", "Unreachable", f"Authenticated ESL probe failed: {category}")
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 — teardown must not hide probe results
                pass


async def _probe_dependency(probe: ServiceProbe) -> dict:
    started = time.perf_counter()
    result = await check_backing_service(probe.kind)
    elapsed = (time.perf_counter() - started) * 1000
    healthy = result.get("ok") is True
    # Return the safe error category; never a raw DSN or exception message.
    detail = "Readiness check passed" if healthy else f"Readiness check failed: {result.get('error', 'unavailable')}"
    return _metric(probe, "good" if healthy else "critical", f"{elapsed:.0f} ms" if healthy else "Unavailable",
                   detail, latency_ms=elapsed)


def _read_memory() -> tuple[int, int]:
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            values[key] = int(rest.split()[0]) * 1024
    return values["MemTotal"], values["MemAvailable"]


def _resource_data(kind: str) -> tuple[float, str, str, dict]:
    if kind == "memory":
        total, available = _read_memory()
        value = (total - available) / total * 100
        return value, f"{value:.1f}%", "System RAM used (MemTotal − MemAvailable); warning ≥80%, critical ≥95%", {"usedPercent": value}
    if kind == "disk":
        usage = shutil.disk_usage("/")
        value = usage.used / usage.total * 100
        return value, f"{value:.1f}%", "Root filesystem used; warning ≥80%, critical ≥95%", {"usedPercent": value, "freeBytes": usage.free}
    load = os.getloadavg()[0]
    cpus = os.cpu_count() or 1
    return load / cpus * 100, f"{load:.2f}", f"1-minute host load average; {cpus} logical CPUs; warning ≥1 load/CPU, critical ≥2 load/CPU", {"load1m": load, "logicalCpus": cpus}


async def _probe_resource(probe: ServiceProbe) -> dict:
    try:
        number, value, detail, observations = await asyncio.to_thread(_resource_data, probe.kind)
        warning, critical = (100, 200) if probe.kind == "load" else (80, 95)
        status = "critical" if number >= critical else ("warning" if number >= warning else "good")
        return _metric(probe, status, value, detail, observations=observations)
    except Exception as exc:  # noqa: BLE001 — unavailable on unsupported operating systems
        return _metric(probe, "warning", "Unavailable", f"Host metric unavailable: {exc.__class__.__name__}")


async def probe_service(probe: ServiceProbe) -> dict:
    if probe.is_self:
        return _metric(probe, "good", "Up", f"Serving this request on {probe.path}")
    if probe.kind in BACKING_SERVICES:
        return await _probe_dependency(probe)
    if probe.kind in ("memory", "disk", "load"):
        return await _probe_resource(probe)
    if probe.kind == "esl" or probe.path is None:
        return await _probe_esl(probe)
    return await _probe_http(probe)


async def probe_services(*, in_process_api: bool = True) -> list[dict]:
    registry = build_registry()
    if not in_process_api:
        # A dedicated collector is not evidence that the HTTP API is alive.
        registry = [replace(probe, is_self=False) for probe in registry]
    services, ai = await asyncio.gather(
        asyncio.gather(*(probe_service(probe) for probe in registry)),
        probe_ai_latency(),
    )
    return [*services, *ai]
