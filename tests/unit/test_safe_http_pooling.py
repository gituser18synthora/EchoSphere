"""fetch_json keeps ONE pooled keep-alive httpx client per process.

The old per-request `with httpx.Client(...)` paid a fresh TCP+TLS handshake
on every tool call (retries included). The pool changes connection reuse
only: redirects stay disabled, the per-call timeout travels with each
request, and the SSRF policy still runs before any request is made.
"""

import contextlib

import pytest

import shared.safe_http as safe_http


class _FakeResponse:
    status_code = 200
    is_redirect = False
    headers: dict = {}

    def iter_bytes(self, chunk_size=8192):
        yield b'{"ok": true}'


class _FakeClient:
    constructions = 0

    def __init__(self, **kwargs):
        type(self).constructions += 1
        self.kwargs = kwargs
        self.stream_calls: list[dict] = []

    @contextlib.contextmanager
    def stream(self, method, url, **kwargs):
        self.stream_calls.append({"method": method, "url": url, **kwargs})
        yield _FakeResponse()


@pytest.fixture()
def pooled(monkeypatch):
    _FakeClient.constructions = 0
    safe_http._pooled_client = None
    monkeypatch.setattr(safe_http.httpx, "Client", _FakeClient)
    monkeypatch.setattr(
        safe_http, "validate_outbound_url", lambda url: "api.example"
    )
    yield
    safe_http._pooled_client = None  # never leak the fake into other tests


def test_client_constructed_once_across_calls(pooled):
    first = safe_http.fetch_json(
        method="GET", url="https://api.example/a", timeout_ms=1000
    )
    second = safe_http.fetch_json(
        method="POST", url="https://api.example/b", json_body={"x": 1},
        timeout_ms=2500,
    )
    assert first.ok and first.body == {"ok": True}
    assert second.ok
    assert _FakeClient.constructions == 1


def test_timeout_travels_per_request_not_per_client(pooled):
    safe_http.fetch_json(
        method="GET", url="https://api.example/a", timeout_ms=1500
    )
    client = safe_http._pooled_client
    assert client.stream_calls[-1]["timeout"] == 1.5
    assert "timeout" not in client.kwargs  # no stale per-client timeout
    assert client.kwargs["follow_redirects"] is False
    assert client.kwargs["limits"].max_keepalive_connections == 10
    assert client.kwargs["limits"].max_connections == 20


def test_ssrf_check_still_runs_before_any_request(pooled, monkeypatch):
    def deny(url):
        raise safe_http.UnsafeUrlError("blocked by policy")

    monkeypatch.setattr(safe_http, "validate_outbound_url", deny)
    result = safe_http.fetch_json(
        method="GET", url="http://169.254.169.254/latest", timeout_ms=1000
    )
    assert result.status_code == 0 and result.ok is False
    assert result.error == "blocked by policy"
    assert _FakeClient.constructions == 0  # the client was never touched
