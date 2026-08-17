"""Interactive chat tester for the OYO bots (talks to POST /bots/{id}/testing/chat).

Usage:
    env/bin/python oyo/tests/chat.py customer     # OYO Booking Support
    env/bin/python oyo/tests/chat.py pm           # OYO Property Verification
    env/bin/python oyo/tests/chat.py stock        # OYO Stock Team Validation
    env/bin/python oyo/tests/chat.py customer --trace   # also print nodes + slots

Type a message and press Enter. Commands: /trace (toggle detail), /new (fresh
session), /slots (dump slots), /quit.
"""

import sys
import uuid

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
LOGIN = {"email": "oyo.config@oyo.com", "password": "Demo@2026!"}

BOTS = {
    "customer": ("bot_e8cf0b05bb79", "OYO Booking Support"),
    "pm": ("bot_99177674902a", "OYO Property Verification"),
    "stock": ("bot_78b6aa83d94a", "OYO Stock Team Validation"),
}

DIM, BOLD, CYAN, YELLOW, GREY, RESET = (
    "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[90m", "\033[0m")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    trace = "--trace" in sys.argv
    key = (args[0] if args else "customer").lower()
    if key not in BOTS:
        print(f"Unknown bot '{key}'. Choose one of: {', '.join(BOTS)}")
        return 2
    bot_id, bot_name = BOTS[key]

    client = httpx.Client(base_url=BASE, timeout=90)
    try:
        r = client.post("/auth/login", json=LOGIN)
        r.raise_for_status()
        client.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"
    except Exception as exc:  # noqa: BLE001
        print(f"Login failed — is the backend API on 9001 running? ({exc})")
        return 1

    session = f"cli_{uuid.uuid4().hex[:8]}"
    history: list[dict] = []
    print(f"{BOLD}{bot_name}{RESET} {GREY}({bot_id}){RESET}")
    print(f"{GREY}session {session} · /trace /new /slots /quit{RESET}\n")
    last_slots: dict = {}

    while True:
        try:
            message = input(f"{CYAN}you ›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not message:
            continue
        low = message.lower()
        if low in ("/quit", "/exit", "/q"):
            return 0
        if low == "/trace":
            trace = not trace
            print(f"{GREY}trace {'on' if trace else 'off'}{RESET}")
            continue
        if low == "/new":
            session = f"cli_{uuid.uuid4().hex[:8]}"
            history, last_slots = [], {}
            print(f"{GREY}new session {session}{RESET}")
            continue
        if low == "/slots":
            for k, v in (last_slots or {}).items():
                print(f"{GREY}  {k} = {v}{RESET}")
            continue

        try:
            resp = client.post(
                f"/bots/{bot_id}/testing/chat",
                json={"message": message, "sessionId": session, "messages": history},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        except Exception as exc:  # noqa: BLE001
            print(f"{YELLOW}request failed: {exc}{RESET}")
            continue

        reply = data.get("reply") or ""
        history += [{"role": "user", "content": message},
                    {"role": "assistant", "content": reply}]
        wf = data.get("workflow") or {}
        last_slots = wf.get("slots") or last_slots
        print(f"{BOLD}bot ›{RESET} {reply}")
        if trace:
            print(f"{GREY}      route={data['route']} intent={data.get('matchedIntent')} "
                  f"status={wf.get('status')} done={data['done']} "
                  f"latency={data.get('latencyMs')}ms{RESET}")
            if wf.get("nodeTrace"):
                print(f"{GREY}      nodes: {' → '.join(wf['nodeTrace'])}{RESET}")
            if wf.get("slots"):
                keys = ", ".join(f"{k}={v}" for k, v in wf["slots"].items())
                print(f"{GREY}      slots: {keys[:400]}{RESET}")
        if data.get("done"):
            print(f"{GREY}      — call flow finished (send another message or /new){RESET}")
        print()


if __name__ == "__main__":
    raise SystemExit(main())
