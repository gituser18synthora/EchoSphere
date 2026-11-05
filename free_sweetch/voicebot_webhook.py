#!/usr/bin/env python3
"""Create an EchoSphere FreeSWITCH media session.

This helper is called by ``voicebot.lua``. The inbound DID, caller number, and
FreeSWITCH UUID remain dynamic for routing and traceability. The webhook secret
is read from a protected file and is never accepted on the command line.
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
DEFAULT_SECRET_FILE = "/usr/local/freeswitch/conf/voicebot_webhook_secret"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-number", default="")
    parser.add_argument("--to-number", required=True)
    parser.add_argument("--to-raw", action="store_true",
                        help="Pass the CRM bot identifier unchanged as To")
    parser.add_argument("--call-id", required=True)
    parser.add_argument("--field", nargs=2, action="append", default=[],
                        metavar=("NAME", "VALUE"),
                        help="Additional webhook field with the vb_ prefix removed")
    return parser.parse_args()


def normalize_indian_did(value: str) -> str:
    """Return an Indian DID in compact E.164 form."""
    compact = re.sub(r"[\s()\-]", "", value)
    if compact.startswith("+91"):
        national_number = compact[3:]
    elif compact.startswith("91") and len(compact) == 12:
        national_number = compact[2:]
    else:
        national_number = compact
    if not re.fullmatch(r"[0-9]{10}", national_number):
        raise ValueError("DID must be a 10-digit Indian number")
    return "+91" + national_number


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
    payload = {
        "To": args.to_number if args.to_raw else normalize_indian_did(args.to_number),
        "From": args.from_number[:30],
        "callId": args.call_id[:64],
        "variables": {},
    }
    for name, value in args.field:
        # Keep custom fields separate from the webhook's routing identity.
        if name:
            payload["variables"][name] = value
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
    print(
        f"voicebot_webhook: request POST {WEBHOOK_URL} payload={body.decode('utf-8')}",
        file=sys.stderr,
        flush=True,
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace")
        detail = " ".join(detail.split())
        raise RuntimeError(
            f"EchoSphere webhook request failed: HTTP {exc.code} {exc.reason}; "
            f"response={detail or '(empty body)'}"
        ) from exc
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
