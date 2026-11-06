"""Shared bits for the Wordline setup scripts: CLI flags, login, ids."""

from __future__ import annotations

import argparse
import json
import os
import sys

TENANT_ID = "tn_ccc488c2c47d"
BOT_ID = "bot_a9d4caca70dc"
WORKFLOW_ID = "wf_0a6550d061e6"
DEFAULT_BASE = "http://127.0.0.1:9001/api/v1"
MERCHANT_API_BASE = os.environ.get(
    "WORDLINE_MERCHANT_API_BASE",
    f"http://127.0.0.1:{os.environ.get('WORDLINE_MERCHANT_API_PORT', '9023')}/api/v1")


def parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--base", default=DEFAULT_BASE, help="platform API base URL")
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    return p


def client(base: str):
    """Logged-in httpx client: WORDLINE_SETUP_EMAIL/PASSWORD, else the
    platform super admin (ECHOSPHERE_SUPER_ADMIN_EMAIL/PASSWORD, local defaults)."""
    import httpx

    email = os.environ.get("WORDLINE_SETUP_EMAIL")
    password = os.environ.get("WORDLINE_SETUP_PASSWORD")
    if not email or not password:
        email = os.environ.get("ECHOSPHERE_SUPER_ADMIN_EMAIL", "admin@aurexion.com")
        password = os.environ.get("ECHOSPHERE_SUPER_ADMIN_PASSWORD", "Admin@2026!")
    c = httpx.Client(base_url=base, timeout=60)
    r = c.post("/auth/login", json={"email": email, "password": password})
    if r.status_code >= 300:
        sys.exit(f"login failed as {email}: {r.status_code} {r.text[:300]}")
    c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"
    return c


def check(r, what: str):
    if r.status_code >= 300:
        sys.exit(f"FAIL {what}: {r.status_code} {r.text[:800]}")
    print(f"ok   {what}")
    return r.json().get("data")


def show(title: str, payload) -> None:
    print(f"--- {title} ---")
    print(json.dumps(payload, indent=2, ensure_ascii=False)[:4000])
