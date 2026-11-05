#!/usr/bin/env python3
"""Create an EchoSphere FreeSWITCH media session.

This helper is called by ``voicebot.lua``. The QA DID is intentionally static
for the first integration, while caller number and FreeSWITCH UUID remain
dynamic for traceability. The webhook secret is read from a protected file and
is never accepted on the command line.

Outbound calls originated by EchoSphere (Studio "Real call") pass the bot as
``--bot-id`` (the ``voicebotID`` channel variable; sent as ``To`` so the bot
is routed directly, no DID assignment needed) and the ``vb_*`` channel
variables as ``--variables-json`` (sent as the webhook's ``variables`` — the
bot's per-call context). Both are optional; without them the call is routed
by the static DID exactly as before.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEBHOOK_URL = "http://echosphere.edas.tech:9011/telephony/webhook/freeswitch"
EXPECTED_WS_HOST = "echosphere.edas.tech:9011"
STATIC_DID = "+91 80 4522 1010"
DEFAULT_SECRET_FILE = "/usr/local/freeswitch/conf/voicebot_webhook_secret"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-number", default="")
    parser.add_argument("--call-id", required=True)
    parser.add_argument("--bot-id", default="")
    parser.add_argument("--variables-json", default="")
    return parser.parse_args()


BOT_ID_PATTERN = re.compile(r"^bot_[A-Za-z0-9]{4,40}$")
VARIABLE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
MAX_VARIABLES = 50


def parse_variables(raw: str) -> dict[str, str]:
    """``vb_*`` channel variables (already stripped of the prefix by the Lua
    side) as a flat string map; anything malformed is dropped, never fatal."""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("voicebot_webhook: ignoring malformed --variables-json", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not VARIABLE_KEY_PATTERN.match(key):
            continue
        if not isinstance(value, (str, int, float, bool)):
            continue
        if len(out) >= MAX_VARIABLES:
            break
        out[key] = str(value)[:200]
    return out


def load_secret() -> bytes:
    path = Path(os.environ.get("VOICEBOT_WEBHOOK_SECRET_FILE", DEFAULT_SECRET_FILE))
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read webhook secret file: {exc}") from exc
    if not secret:
        raise RuntimeError("webhook secret file is empty")
    return secret.encode()


def validate_ws_url(value: str) -> str:
    parsed = urlparse(value)
    expected_prefix = "/ws/telephony/freeswitch/vs_"
    query = parse_qs(parsed.query, strict_parsing=True)
    if (
        parsed.scheme not in {"ws", "wss"}
        or parsed.netloc != EXPECTED_WS_HOST
        or not parsed.path.startswith(expected_prefix)
        or parsed.params
        or query != {"transport": ["audio_fork"]}
        or parsed.fragment
    ):
        raise RuntimeError("webhook returned an unexpected WebSocket URL")
    # EchoSphere currently advertises wss:// on port 9011, but that port is
    # plain HTTP (verified from FreeSWITCH). Normalize this trusted, validated
    # media endpoint to ws://; its signed webhook uses the same service port.
    return parsed._replace(scheme="ws").geturl()


def main() -> int:
    args = parse_args()
    bot_id = args.bot_id.strip()
    if bot_id and not BOT_ID_PATTERN.match(bot_id):
        raise RuntimeError("malformed --bot-id")
    payload = {
        # A bot id in ``To`` routes the call to that bot directly (its own
        # tenant is the anchor); otherwise the static QA DID picks the bot.
        "To": bot_id or STATIC_DID,
        "From": args.from_number[:30],
        "callId": args.call_id[:64],
    }
    variables = parse_variables(args.variables_json)
    if variables:
        payload["variables"] = variables
        payload["Direction"] = "outbound"
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        load_secret(),
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    request = urllib.request.Request(
        WEBHOOK_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"EchoSphere webhook request failed: {exc}") from exc

    ws_url = result.get("audio_fork_url") or result.get("audio_stream_url") or ""
    print(validate_ws_url(ws_url))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"voicebot_webhook: {exc}", file=sys.stderr)
        raise SystemExit(1)
