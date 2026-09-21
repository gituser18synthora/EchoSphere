"""Deepgram configuration shared by every Deepgram adapter (STT and TTS).

Deepgram is one vendor with one account, one API key and one set of regional
hosts. Everything in this module is *capability-neutral* — credentials, host
selection, auth headers and HTTP error categorization — so the STT and TTS
adapters can share it without sharing any runtime logic. Nothing here knows
about transcription or synthesis; the per-capability wire protocols live in
``shared/providers/stt/deepgram.py``, ``shared/providers/tts/deepgram.py``
and ``shared/providers/tts/deepgram_ws.py``.

Credentials
-----------
The platform stores credentials as ``env:`` references on ``provider_defs``
(``env:DEEPGRAM_API_KEY`` for both the STT and the TTS provider row), so a
Deepgram adapter never needs its own environment variable. When a caller
constructs a ``ProviderConfig`` by hand and leaves the reference blank,
:data:`DEFAULT_SECRET_REFERENCE` is the last-resort fallback — the same key,
never another vendor's.

Regions
-------
Deepgram serves the identical API from several hosts; only the base URL
changes, and the same API keys work on all of them
(developers.deepgram.com/reference/custom-endpoints, verified 2026-09-21):

===========  ==========================  =============================
region code  REST base                   WebSocket base
===========  ==========================  =============================
``global``   ``api.deepgram.com``        ``wss://api.deepgram.com``
``in``       ``api.in.deepgram.com``     ``wss://api.in.deepgram.com``
``eu``       ``api.eu.deepgram.com``     ``wss://api.eu.deepgram.com``
``au``       ``api.au.deepgram.com``     ``wss://api.au.deepgram.com``
===========  ==========================  =============================

``DEEPGRAM_REGION`` sets the platform default (``global`` when unset); an
engine may override it per configuration with a ``region`` parameter.
``DEEPGRAM_API_BASE`` / ``DEEPGRAM_WS_BASE`` pin an explicit host — for a
self-hosted deployment or for mocked end-to-end verification — and win over
the region, mirroring ``SARVAM_TTS_WS_URL`` and ``ELEVENLABS_WS_BASE``.

A regional host is a data-residency choice, NOT a capability statement: the
India endpoint runs exactly the same models as the global one and adds no
languages. Language support is modelled in ``shared.providers.languages``.
"""

from __future__ import annotations

import os

import httpx

from shared.config import get_settings
from shared.providers.base import ProviderError

#: Where both Deepgram provider rows point their ``secret_ref``.
DEFAULT_SECRET_REFERENCE = "env:DEEPGRAM_API_KEY"

#: Region code → Deepgram host. Official regional endpoints, verified
#: 2026-09-21 (developers.deepgram.com/reference/custom-endpoints).
REGION_HOSTS: dict[str, str] = {
    "global": "api.deepgram.com",
    "in": "api.in.deepgram.com",
    "eu": "api.eu.deepgram.com",
    "au": "api.au.deepgram.com",
}

#: Spellings operators actually type, mapped onto the canonical codes above.
_REGION_ALIASES: dict[str, str] = {
    "": "global",
    "default": "global",
    "us": "global",
    "world": "global",
    "india": "in",
    "in-in": "in",
    "ap-south": "in",
    "europe": "eu",
    "eu-west": "eu",
    "australia": "au",
}


def normalize_region(region: str | None) -> str:
    """Canonical region code for whatever spelling reached us.

    An unknown value falls back to ``global`` rather than building a
    nonexistent hostname out of unvalidated input.
    """
    code = (region or "").strip().lower()
    code = _REGION_ALIASES.get(code, code)
    return code if code in REGION_HOSTS else "global"


def default_region() -> str:
    """Platform default region (``DEEPGRAM_REGION``, else ``global``).

    Read per call, not captured at import, so a process that reloads its
    environment picks the change up without a restart — and so tests can set
    it with monkeypatch.setenv.
    """
    return normalize_region(os.environ.get("DEEPGRAM_REGION"))


def region_host(region: str | None = None) -> str:
    """Deepgram hostname for a region (falsy ``region`` → platform default)."""
    code = normalize_region(region) if region else default_region()
    return REGION_HOSTS[code]


def rest_base_url(region: str | None = None) -> str:
    """``https://<host>`` for REST calls, honouring ``DEEPGRAM_API_BASE``."""
    override = (os.environ.get("DEEPGRAM_API_BASE") or "").strip()
    if override:
        return override.rstrip("/")
    return f"https://{region_host(region)}"


def ws_base_url(region: str | None = None) -> str:
    """``wss://<host>`` for WebSocket calls, honouring ``DEEPGRAM_WS_BASE``."""
    override = (os.environ.get("DEEPGRAM_WS_BASE") or "").strip()
    if override:
        return override.rstrip("/")
    return f"wss://{region_host(region)}"


def auth_headers(api_key: str) -> dict[str, str]:
    """Deepgram's handshake/request auth header (``Token <key>``)."""
    return {"Authorization": f"Token {api_key}"}


def resolve_api_key(*references: str) -> str:
    """First non-empty resolved secret among ``references``, else the
    Deepgram default reference. Returns "" when nothing resolves."""
    settings = get_settings()
    for reference in (*references, DEFAULT_SECRET_REFERENCE):
        if not reference:
            continue
        key = settings.resolve_secret(reference)
        if key:
            return key
    return ""


def categorize_status(status_code: int) -> str:
    """Map a Deepgram HTTP status onto a :class:`ProviderError` category."""
    if status_code in (401, 403):
        return "auth"
    if status_code == 429:
        return "rate_limit"
    if status_code in (400, 404, 422):
        # A bad model/voice/encoding combination is a configuration error the
        # operator can fix — it must surface, never trigger engine fallback.
        return "invalid_input"
    return "upstream"


def raise_for_status(provider: str, response: httpx.Response) -> None:
    """Raise a categorized :class:`ProviderError` for a 4xx/5xx response."""
    if response.status_code < 400:
        return
    detail = response.text[:200]
    raise ProviderError(
        provider,
        categorize_status(response.status_code),
        f"HTTP {response.status_code}: {detail}",
    )
