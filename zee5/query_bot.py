#!/usr/bin/env python3
"""Query the locally configured ZEE5 Subscription Support bot.

Examples:
    python zee5/query_bot.py
    python zee5/query_bot.py --message "ZEE5 ke plans kya hain?" --language hi-IN
    python zee5/query_bot.py --debug
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DEFAULT_API_BASE = "http://127.0.0.1:9001/api/v1"
DEFAULT_BOT_ID = "bot_7ae045e521ef"


def credentials() -> tuple[str, str]:
    email = os.getenv("ECHOSPHERE_SUPER_ADMIN_EMAIL") or os.getenv("SUPERADMIN_EMAIL")
    password = os.getenv("ECHOSPHERE_SUPER_ADMIN_PASSWORD") or os.getenv("SUPERADMIN_PASSWORD")
    if not email:
        email = input("EchoSphere admin email: ").strip()
    if not password:
        password = getpass.getpass("EchoSphere admin password: ")
    return email, password


def login(client: httpx.Client) -> None:
    email, password = credentials()
    response = client.post("/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    token = response.json()["data"]["token"]
    client.headers["Authorization"] = f"Bearer {token}"


def send_message(
    client: httpx.Client,
    bot_id: str,
    message: str,
    language: str,
    session_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message": message,
        "language": language,
        "messages": [],
    }
    if session_id:
        payload["sessionId"] = session_id

    response = client.post(f"/bots/{bot_id}/testing/chat", json=payload)
    response.raise_for_status()
    return response.json()["data"]


def print_reply(data: dict[str, Any], debug: bool) -> None:
    print(f"BOT: {data.get('reply', '<no reply>')}")
    if debug:
        workflow = data.get("workflow") or {}
        print(
            "DEBUG:",
            {
                "sessionId": data.get("sessionId"),
                "route": data.get("route"),
                "activeWorkflow": data.get("activeWorkflow"),
                "done": data.get("done"),
                "slots": workflow.get("slots"),
                "guardrail": data.get("guardrail"),
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Query the LOCAL ZEE5 support bot")
    parser.add_argument("--message", "-m", help="Send one message and exit")
    parser.add_argument("--language", "-l", default="en-IN", help="Locale, e.g. en-IN or hi-IN")
    parser.add_argument("--bot-id", default=DEFAULT_BOT_ID)
    parser.add_argument(
        "--api-base",
        default=os.getenv("ECHOSPHERE_API_BASE", DEFAULT_API_BASE),
        help="Local EchoSphere API base URL",
    )
    parser.add_argument("--debug", action="store_true", help="Show route, workflow, slots and guardrail")
    args = parser.parse_args()

    try:
        with httpx.Client(base_url=args.api_base, timeout=90) as client:
            login(client)

            if args.message:
                print_reply(
                    send_message(client, args.bot_id, args.message, args.language, None),
                    args.debug,
                )
                return 0

            session_id: str | None = None
            print("ZEE5 local bot chat. Commands: /new, /debug, /quit")
            while True:
                try:
                    message = input("YOU: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 0

                if not message:
                    continue
                if message in {"/quit", "/exit"}:
                    return 0
                if message == "/new":
                    session_id = None
                    print("Started a new conversation.")
                    continue
                if message == "/debug":
                    args.debug = not args.debug
                    print(f"Debug {'on' if args.debug else 'off'}.")
                    continue

                data = send_message(client, args.bot_id, message, args.language, session_id)
                session_id = data.get("sessionId") or session_id
                print_reply(data, args.debug)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500]
        print(f"HTTP {exc.response.status_code}: {detail}", file=sys.stderr)
        return 1
    except httpx.RequestError as exc:
        print(f"Cannot reach local EchoSphere API at {args.api_base}: {exc}", file=sys.stderr)
        print("Start the local stack with: ./scripts/dev.sh start", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
