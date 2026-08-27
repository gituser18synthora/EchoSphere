"""Interactive chat tester for the Honasa Customer Care bot.

Usage:  env/bin/python honasa/tests/chat.py [--trace]
Type your message; blank line or Ctrl-C exits. Requires API on 9001 and the
honasa mock on 9022.
"""

import json
import pathlib
import sys
import uuid

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE_FILE = (pathlib.Path(__file__).resolve().parent.parent
              / "setup" / "honasa_config_state.json")
BOT = json.load(open(STATE_FILE))["BOT"]
TRACE = "--trace" in sys.argv

c = httpx.Client(base_url=BASE, timeout=60)
r = c.post("/auth/login", json={"email": "honasa.config@honasa.com",
                                "password": "Demo@2026!"})
r.raise_for_status()
c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"

session = f"cli_{uuid.uuid4().hex[:10]}"
history: list[dict] = []
print(f"Honasa Customer Care ({BOT}) — session {session}. Blank line to exit.")

while True:
    try:
        message = input("you> ").strip()
    except (EOFError, KeyboardInterrupt):
        break
    if not message:
        break
    d = c.post(f"/bots/{BOT}/testing/chat",
               json={"message": message, "sessionId": session,
                     "messages": history}).json()["data"]
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": d["reply"] or ""})
    wf = d.get("workflow") or {}
    if TRACE:
        print(f"     [route={d['route']} status={wf.get('status')} "
              f"done={d['done']} trace={wf.get('nodeTrace')}]")
        if wf.get("slots"):
            print(f"     [slots={json.dumps(wf['slots'], ensure_ascii=False)[:300]}]")
    print(f"bot> {d['reply']}")
    if d.get("done") and (wf.get("status") in ("done", "handoff")):
        print("     (workflow finished — next message starts fresh routing)")
