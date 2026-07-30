"""SSRF-guarded outbound HTTP for tenant-configured API connections.

Protections:
- http/https only, no redirects followed,
- DNS-resolved target must be a public address (loopback/private/link-local/
  reserved blocked) unless the host is allowlisted or private targets are
  explicitly enabled (dev/test),
- response body reads are capped,
- secrets come from references and are never echoed back.
"""

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from shared.config import get_settings


class UnsafeUrlError(Exception):
    """URL failed the SSRF policy — message is user-safe."""


def _allowed_hosts() -> set[str]:
    raw = get_settings().api_connect_allowed_hosts
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def validate_outbound_url(url: str) -> str:
    """Validate scheme + resolve the host and enforce the address policy.
    Returns the hostname. Raises UnsafeUrlError with a user-safe message."""
    try:
        parsed = urlparse(url)
    except ValueError:
        raise UnsafeUrlError("The URL could not be parsed.")
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError("Only http:// and https:// URLs are allowed.")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("The URL has no hostname.")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("Credentials in the URL are not allowed — use a secret reference.")

    settings = get_settings()
    if host.lower() in _allowed_hosts():
        return host
    if settings.api_connect_allow_private:
        return host

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UnsafeUrlError("The hostname could not be resolved.")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved or address.is_unspecified):
            raise UnsafeUrlError(
                "The URL resolves to a private or internal address, which is not allowed."
            )
    return host


_PREVIEWABLE_TYPES = ("application/json", "text/", "application/xml", "application/problem+json")


@dataclass
class SafeResponse:
    status_code: int
    ok: bool
    latency_ms: int
    body_preview: str = ""
    content_type: str = ""
    error: str | None = None
    redirected_to: str | None = None
    truncated: bool = False
    headers_sent: dict = field(default_factory=dict)  # already masked


def safe_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: dict | list | None = None,
    timeout_ms: int = 4000,
    sensitive_headers: set[str] | None = None,
) -> SafeResponse:
    """Perform one guarded request. Never raises for network errors."""
    import time

    settings = get_settings()
    max_bytes = settings.api_connect_max_response_kb * 1024
    sensitive = {h.lower() for h in (sensitive_headers or set())} | {
        "authorization", "x-api-key", "api-key", "cookie", "proxy-authorization",
    }
    masked_headers = {
        k: ("•••" if k.lower() in sensitive else v) for k, v in (headers or {}).items()
    }

    try:
        validate_outbound_url(url)
    except UnsafeUrlError as exc:
        return SafeResponse(status_code=0, ok=False, latency_ms=0,
                            error=str(exc), headers_sent=masked_headers)

    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout_ms / 1000, follow_redirects=False) as client:
            with client.stream(
                method, url, headers=headers or {}, params=params or None,
                json=json_body,
            ) as resp:
                latency_ms = round((time.monotonic() - started) * 1000)
                content_type = resp.headers.get("content-type", "")
                redirected = resp.headers.get("location") if resp.is_redirect else None
                body = b""
                truncated = False
                if any(content_type.startswith(t) for t in _PREVIEWABLE_TYPES):
                    for chunk in resp.iter_bytes(chunk_size=8192):
                        body += chunk
                        if len(body) >= max_bytes:
                            truncated = True
                            break
                preview = body[:max_bytes].decode("utf-8", errors="replace")
                return SafeResponse(
                    status_code=resp.status_code,
                    ok=200 <= resp.status_code < 400 and not resp.is_redirect,
                    latency_ms=latency_ms,
                    body_preview=preview[:2000],
                    content_type=content_type,
                    redirected_to=redirected,
                    truncated=truncated,
                    headers_sent=masked_headers,
                )
    except httpx.TimeoutException:
        return SafeResponse(
            status_code=504, ok=False,
            latency_ms=round((time.monotonic() - started) * 1000),
            error=f"Upstream timeout after {timeout_ms}ms.", headers_sent=masked_headers,
        )
    except httpx.HTTPError as exc:
        return SafeResponse(
            status_code=502, ok=False,
            latency_ms=round((time.monotonic() - started) * 1000),
            error=f"Connection failed ({type(exc).__name__}).", headers_sent=masked_headers,
        )
