"""Simulate-endpoint regression suite for the Haier bot — runs AFTER the setup
stages are applied to the local DB (POST /bots/{id}/testing/simulate goes
through the real router, intents, published prompt, runtime context and the
saved workflow, with the CRM hooks mocked from the connection samples).

Same customer scripts as the offline replay (tests/scenarios.py). The first
turn enters the workflow through the opening intent exactly as on a live
call; off-script turns show the LLM's actual wording.

Run:  env/bin/python haier/tests/run_chat_scenarios.py [name-filter] [--md out.md]
Requires: backend API on 9001, stages 00–04 applied.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import haier_config as H  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402

BASE = os.environ.get("ECHOSPHERE_API", "http://127.0.0.1:9001/api/v1")


def login() -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=180)
    logins = [(os.environ.get("HAIER_ADMIN_EMAIL", ""), os.environ.get("HAIER_ADMIN_PASSWORD", "")),
              (os.environ.get("ECHOSPHERE_SUPER_ADMIN_EMAIL", "admin@aurexion.com"),
               os.environ.get("ECHOSPHERE_SUPER_ADMIN_PASSWORD", "Admin@2026!"))]
    for email, password in logins:
        if not email:
            continue
        r = c.post("/auth/login", json={"email": email, "password": password})
        if r.status_code < 300:
            c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"
            return c
    raise SystemExit("login failed")


def turn(c, session, message, history, context):
    body = {"message": message, "sessionId": session, "messages": history,
            "contextSource": "manual", "contextPayload": context, "mockToolResults": H.MOCKS}
    r = c.post(f"/bots/{H.BOT}/testing/simulate", json=body)
    r.raise_for_status()
    return r.json()["data"]


def run(c, scenario):
    session = f"haier_{uuid.uuid4().hex[:10]}"
    history, rows, failures = [], [], []
    ctx = scenario["context"]
    for index, item in enumerate(scenario["turns"]):
        text, expect = (item, {}) if isinstance(item, str) else item
        d = turn(c, session, text, history, ctx)
        reply = d.get("response") or ""
        wf = d.get("workflow") or {}
        history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        trace = wf.get("nodeTrace") or []
        rows.append({"text": text, "reply": reply, "route": d.get("route"), "status": wf.get("status"),
                     "done": bool(wf.get("done")), "trace": trace, "slots": wf.get("slots") or {},
                     "offScript": bool(wf.get("offScript"))})
        if "node" in expect and expect["node"] not in trace and (trace[-1:] or [""])[0] != expect["node"]:
            failures.append(f"turn {index + 1} {text[:40]!r}: expected node {expect['node']} (trace {trace[-4:]}, route {d.get('route')})")
        if "reply" in expect and not expect.get("offscript") and expect["reply"].lower() not in reply.lower():
            failures.append(f"turn {index + 1} {text[:40]!r}: reply lacks {expect['reply']!r} → {reply[:160]!r}")
        if "done" in expect and bool(expect["done"]) != bool(wf.get("done")):
            failures.append(f"turn {index + 1} {text[:40]!r}: done={wf.get('done')} expected {expect['done']}")
        if wf.get("done") and index < len(scenario["turns"]) - 1:
            failures.append(f"turn {index + 1}: workflow finished early")
            break
    final = rows[-1]["slots"] if rows else {}
    for key, want in (scenario.get("final") or {}).items():
        got = final.get(key)
        if str(got) != str(want) and not (isinstance(want, str) and want.lower() in str(got).lower()):
            failures.append(f"final slot {key}={got!r}, expected {want!r}")
    if rows and not rows[-1]["done"]:
        failures.append("workflow did not finish")
    return rows, failures


def main(argv):
    md_out = None
    if "--md" in argv:
        md_out = argv[argv.index("--md") + 1]
        argv = [a for a in argv if a not in ("--md", md_out)]
    name_filter = argv[0].lower() if argv else ""
    c = login()
    passed = failed = 0
    sections = []
    for scenario in SCENARIOS:
        if name_filter and name_filter not in scenario["name"].lower():
            continue
        rows, failures = run(c, scenario)
        passed += not failures; failed += bool(failures)
        print(f"\n[{'PASS' if not failures else 'FAIL'}] {scenario['name']}")
        for i, row in enumerate(rows, 1):
            node = "END" if row["done"] else (row["trace"][-1] if row["trace"] else row["route"])
            print(f"  {i}. C: {row['text']}\n     B: {row['reply'][:300]}\n        node={node} route={row['route']}")
        for f in failures:
            print(f"  !! {f}")
        sec = [f"### {scenario['name']} — {'PASS' if not failures else 'FAIL'}", "",
               "| # | Customer | Bot (simulate endpoint, real router + LLM) | Node |", "|---|---|---|---|"]
        for i, row in enumerate(rows, 1):
            node = "end ✔" if row["done"] else (row["trace"][-1] if row["trace"] else row["route"])
            sec.append(f"| {i} | {row['text']} | {row['reply']} | `{node}` |")
        if failures:
            sec += ["", *[f"- {f}" for f in failures]]
        sec += ["", "```json", json.dumps(rows[-1]["slots"] if rows else {}, ensure_ascii=False, indent=1), "```", ""]
        sections.append("\n".join(sec))
    print(f"\n{passed} passed, {failed} failed")
    if md_out:
        open(md_out, "w").write("\n".join(sections))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
