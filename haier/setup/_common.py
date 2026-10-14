"""Shared plumbing for the Haier setup stages: API client, dry-run/apply.

Every stage is DRY-RUN by default: it prints each write (method, path,
JSON body) and sends nothing. Pass ``--apply`` to execute the writes.
Reads (GET) always run so the printed payloads reflect current state.
"""
from __future__ import annotations

import json
import os
import sys

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("ECHOSPHERE_API", "http://127.0.0.1:9001/api/v1")
STATE_FILE = os.path.join(HERE, "haier_config_state.json")
APPLY = "--apply" in sys.argv
WRITES: list[dict] = []


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _login(c: httpx.Client, email: str, password: str) -> bool:
    r = c.post("/auth/login", json={"email": email, "password": password})
    if r.status_code >= 300:
        return False
    c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"
    return True


TENANT_ADMIN_LOGIN = (os.environ.get("HAIER_ADMIN_EMAIL", ""), os.environ.get("HAIER_ADMIN_PASSWORD", ""))
SUPER_ADMIN_LOGIN = (os.environ.get("ECHOSPHERE_SUPER_ADMIN_EMAIL", "admin@aurexion.com"),
                     os.environ.get("ECHOSPHERE_SUPER_ADMIN_PASSWORD", "Admin@2026!"))


def client(*, prefer_tenant_admin: bool = True) -> httpx.Client:
    """Haier tenant-admin login from the environment when given, else the
    platform super admin.

    Every stage endpoint accepts the super admin EXCEPT ``/tenant/turn-detection``
    (``require_roles(TENANT_ADMIN)`` — the super admin gets HTTP 403). That one
    write needs an existing Haier tenant-admin login (e.g. admin@haier.com)
    passed as HAIER_ADMIN_EMAIL / HAIER_ADMIN_PASSWORD; no credential is stored
    in this repository and no account is created.
    """
    c = httpx.Client(base_url=BASE, timeout=60)
    if prefer_tenant_admin and all(TENANT_ADMIN_LOGIN) and _login(c, *TENANT_ADMIN_LOGIN):
        c.headers["X-Login"] = "tenant_admin"
        return c
    if _login(c, *SUPER_ADMIN_LOGIN):
        c.headers["X-Login"] = "super_admin"
        return c
    raise SystemExit("FAIL login (tenant admin from env and super admin both failed)")


def check(r: httpx.Response, what: str):
    if r.status_code >= 300:
        raise SystemExit(f"FAIL {what}: {r.status_code} {r.text[:600]}")
    print(f"ok   {what}")
    data = r.json()
    return data.get("data") if isinstance(data, dict) else data


def write(c: httpx.Client, method: str, path: str, what: str, *, json_body=None,
          files=None, params=None, expect_conflict_ok: bool = False):
    """Print the write; execute only with --apply. Returns response data or None."""
    body_preview = json.dumps(json_body, ensure_ascii=False, indent=1) if json_body is not None else "(no body)"
    WRITES.append({"method": method, "path": path, "what": what, "body": json_body})
    print(f"\n### WRITE {method} {path}  — {what}\n{body_preview}")
    if not APPLY:
        print("     (dry-run: not sent)")
        return None
    r = c.request(method, path, json=json_body, files=files, params=params)
    if expect_conflict_ok and r.status_code == 409:
        print(f"ok   {what} (already exists)")
        return None
    return check(r, what)


def banner(title: str) -> None:
    mode = "APPLY" if APPLY else "DRY-RUN"
    print(f"\n==================== {title} [{mode}] ====================")


def summary() -> None:
    print(f"\n{len(WRITES)} write(s) {'applied' if APPLY else 'planned (dry-run; re-run with --apply)'}")
