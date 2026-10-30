"""Shared constants + API client for the AU Small Finance Bank bot setup.

The bot ALREADY exists (imported tenant); these scripts only update its
configuration through the platform API — nothing is written to the database
directly and no data/config file carries the demo values (they live in the
bot's system prompt and its workflow definition, both stored on the bot).

Login: a super-admin (or AU tenant admin) account. Defaults to the platform
super admin from .env; override with AU_SETUP_EMAIL / AU_SETUP_PASSWORD.
"""

import os
import pathlib

import httpx

BASE = os.environ.get("AU_API_BASE", "http://127.0.0.1:9001/api/v1")
TENANT = "tn_b8897f32d4aa"
BOT = "bot_ac634648c152"
WORKFLOW_ID = "wf_bb319e0f6fb5"          # the bot's single existing workflow row

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _env_password() -> str:
    explicit = os.environ.get("AU_SETUP_PASSWORD")
    if explicit:
        return explicit
    env_file = _ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("SUPERADMIN_PASSWORD="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "Admin@2026!"


def check(r, what):
    if r.status_code >= 300:
        raise SystemExit(f"FAIL {what}: {r.status_code} {r.text[:800]}")
    print(f"ok   {what}")
    return r.json().get("data")


def client(timeout: float = 60) -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=timeout)
    email = os.environ.get("AU_SETUP_EMAIL", "admin@aurexion.com")
    token = check(c.post("/auth/login", json={"email": email,
                                              "password": _env_password()}),
                  f"login {email}")["token"]
    c.headers["Authorization"] = f"Bearer {token}"
    return c
