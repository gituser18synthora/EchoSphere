"""Live health probes for the platform's own runtime services.

Every row the Platform Health card shows is a real check performed when the
request is served — an HTTP ``/health`` round-trip or, for FreeSWITCH's event
socket, a TCP connect. Nothing here reports healthy without a successful
probe, matching the "no fabricated successes" rule the channel connectivity
tests already follow (``backend/routers/channels._run_channel_test``).

Hosts and ports come exclusively from :class:`shared.config.Settings`, i.e.
from ``.env`` — never hardcoded — so moving a service to a different port
moves its probe with it.
"""

import asyncio
import time
from dataclasses import dataclass

import httpx

from shared.config import get_settings

# Per-probe ceiling. Short on purpose: the admin dashboard waits on these, and
# a service that cannot answer a loopback health check in two seconds is not
# healthy from the operator's point of view.
PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class ServiceProbe:
    """One row of the Platform Health card.

    ``group`` matches a Monitoring page tab id, so the frontend never has to
    map service names to tabs by hand.
    """

    name: str
    group: str  # platform | ai | telephony
    host: str
    port: int
    # HTTP health path, or None for a raw TCP connect (FreeSWITCH ESL is not
    # an HTTP server — probing it with a GET would always fail).
    path: str | None = None
    # True for the process serving this request: probing ourselves over HTTP
    # would be circular, so we report what we are bound to instead.
    is_self: bool = False


def _reachable_host(host: str) -> str:
    """A wildcard bind address is not a dialable target — probe loopback."""
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def build_registry() -> list[ServiceProbe]:
    """The services shown on Platform Health, resolved from ``.env``."""
    settings = get_settings()
    return [
        ServiceProbe(
            name="Platform API", group="platform",
            host=_reachable_host(settings.api_host), port=settings.api_port,
            path="/api/health", is_self=True,
        ),
        ServiceProbe(
            name="Voice Worker", group="ai",
            host=_reachable_host(settings.voice_worker_host),
            port=settings.voice_worker_port, path="/health",
        ),
        ServiceProbe(
            name="MCP Server", group="platform",
            host=_reachable_host(settings.mcp_host), port=settings.mcp_port,
            path="/health",
        ),
        ServiceProbe(
            name="FreeSWITCH ESL", group="telephony",
            host=_reachable_host(settings.freeswitch_host),
            port=settings.freeswitch_port, path=None,
        ),
        ServiceProbe(
            name="Telephony gateway", group="telephony",
            host=_reachable_host(settings.telephony_gateway_host),
            port=settings.telephony_gateway_port, path="/health",
        ),
    ]


def _payload_status(payload: object) -> str | None:
    """The reported status from either a bare or envelope-wrapped body.

    The voice worker and MCP server answer ``{"status": "up", ...}``; the
    platform API wraps its own payload in the standard ``{"success", "data"}``
    envelope.
    """
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, str):
        return status
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("status"), str):
        return data["status"]
    return None


def _metric(probe: ServiceProbe, status: str, value: str, detail: str) -> dict:
    """A HealthMetric-shaped row for the admin UI.

    ``target`` carries the host:port actually probed — the fastest way for an
    operator to spot a service listening somewhere other than where ``.env``
    says it should be.
    """
    return {
        "name": probe.name,
        "status": status,
        "value": value,
        "target": f"{probe.host}:{probe.port}",
        "spark": [],
        "group": probe.group,
        "detail": detail,
    }


def _http_client() -> httpx.AsyncClient:
    """Seam for tests; production always gets a plain, short-timeout client."""
    return httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS)


async def _probe_http(probe: ServiceProbe) -> dict:
    url = f"http://{probe.host}:{probe.port}{probe.path}"
    started = time.perf_counter()
    try:
        async with _http_client() as client:
            response = await client.get(url)
    except Exception as exc:  # noqa: BLE001 — every failure is "not healthy"
        return _metric(probe, "critical", "Unreachable",
                       f"{url} — {exc.__class__.__name__}")
    elapsed = f"{(time.perf_counter() - started) * 1000:.0f} ms"

    if response.status_code != 200:
        return _metric(probe, "critical", "Unreachable",
                       f"{url} → HTTP {response.status_code}")
    try:
        reported = _payload_status(response.json())
    except ValueError:
        reported = None
    if reported != "up":
        # Answering but not declaring itself up: reachable yet degraded.
        return _metric(probe, "warning", elapsed,
                       f"{url} → 200, status={reported or 'unknown'}")
    return _metric(probe, "good", elapsed, f"{url} → 200")


async def _probe_tcp(probe: ServiceProbe) -> dict:
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(probe.host, probe.port),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        return _metric(probe, "critical", "Unreachable",
                       f"{probe.host}:{probe.port} — {exc.__class__.__name__}")
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001 — teardown must not fail the probe
        pass
    elapsed = f"{(time.perf_counter() - started) * 1000:.0f} ms"
    return _metric(probe, "good", elapsed,
                   f"{probe.host}:{probe.port} event socket accepted")


async def probe_service(probe: ServiceProbe) -> dict:
    if probe.is_self:
        return _metric(probe, "good", "Up", f"Serving this request on {probe.path}")
    if probe.path is None:
        return await _probe_tcp(probe)
    return await _probe_http(probe)


async def probe_services() -> list[dict]:
    """Probe every registered service concurrently, in registry order."""
    registry = build_registry()
    return list(await asyncio.gather(*(probe_service(p) for p in registry)))
