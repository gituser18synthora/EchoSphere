"""Stage 05 — go-live: knowledge base, voice channel, scenarios, readiness,
publish, channel activation.

Readiness items are DERIVED by the platform (shared/readiness.py); this stage
only creates the missing real configuration:

  r1 knowledge  - bot-scoped KB from frankfinn/docs/Frankfinn_Seminar_FAQ.md
                  (bot-scoped on purpose: KB presence flips question routing
                  to retrieval, so the bot only gets content that is correct
                  to speak; the student's OWN appointment facts stay with the
                  LLM via the no-route seminar_fact_question intent)
  r6 channels   - a voice channel bound to the seminar-booking workflow,
                  caller ID +911246026010 (the campaign CallingParty from the
                  reference recording metadata)
  r7 scenarios  - the regression suite mirrored as platform test scenarios
                  and executed. Run frankfinn/tests/run_chat_scenarios.py
                  FIRST — record the platform suite only when the real suite
                  passes.
  recompute     - re-derives r1..r7 from live state
  publish       - bot status -> published
  activate      - voice channel enabled (requires the published bot — live
                  calls always run the published configuration)

Stages: knowledge, channel, scenarios, recompute, publish, activate, all.
Run: env/bin/python frankfinn/setup/05_go_live.py [stage]
"""

import json
import pathlib
import sys
import time

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE_FILE = __file__.rsplit("/", 1)[0] + "/frankfinn_config_state.json"
BOT = json.load(open(STATE_FILE))["BOT"]

DOC_DIR = pathlib.Path(__file__).resolve().parent.parent / "docs"
KB_NAME = "Frankfinn Seminar & Courses FAQ"
PHONE = "+911246026010"
WORKFLOW_NAME = "Frankfinn seminar booking journey"

SCENARIOS = [
    ("Happy path", 10, "01 graduate books seat (mocked booking success)"),
    ("Happy path", 5, "02 twelfth-pass gets 11-month track"),
    ("Happy path", 6, "03 third-year final-year probe -> 8-month track"),
    ("Happy path", 6, "04 third-year not final -> 11-month track"),
    ("Eligibility", 5, "05 below twelfth -> polite not-eligible close"),
    ("Objections", 8, "06 fees question answered, then books"),
    ("Objections", 7, "07 declined once, soft counter, then books"),
    ("Objections", 6, "08 declined twice -> polite close"),
    ("Call handling", 3, "09 busy -> callback captured and close"),
    ("Call handling", 2, "10 wrong number -> apology close"),
    ("Compliance", 2, "11 do-not-call -> platform call_control close"),
    ("Robustness", 9, "12 booking API unavailable -> graceful SMS fallback"),
    ("Robustness", 10, "13 SMS not received -> address spoken"),
    ("Robustness", 7, "14 KB question mid-booking answered, flow resumes"),
    ("Call handling", 2, "15 explicit human request -> senior counsellor handover"),
    ("Robustness", 8, "16 off-script question mid-flow, flow resumes"),
]


def client() -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/auth/login", json={"email": "frankfinn.config@frankfinn.com",
                                    "password": "Demo@2026!"})
    r.raise_for_status()
    c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"
    return c


def check(r: httpx.Response, what: str):
    if r.status_code >= 300:
        print(f"FAIL {what}: {r.status_code} {r.text[:500]}")
        sys.exit(1)
    print(f"ok   {what}")
    return r.json().get("data")


def stage_knowledge(c: httpx.Client) -> None:
    data = check(c.get("/knowledge", params={"pageSize": 100}), "list knowledge")
    rows = data if isinstance(data, list) else data.get("items", [])
    kb = next((k for k in rows if k["name"] == KB_NAME), None)
    if kb is None:
        kb = check(c.post("/knowledge", json={
            "scope": "bot", "botId": BOT, "type": "document",
            "name": KB_NAME,
            "detail": ("Student-facing FAQ: the free career counselling "
                       "seminar, eligibility and course tracks, scholarship, "
                       "Aadhaar entry mandate, centre directions, helpline."),
        }), f"create bot KB '{KB_NAME}'")
    fname = "Frankfinn_Seminar_FAQ.md"
    with (DOC_DIR / fname).open("rb") as f:
        doc = check(
            c.post(f"/knowledge/{kb['id']}/documents",
                   files={"file": (fname, f, "text/markdown")}),
            f"upload {fname}")
    doc_id = doc["documentId"]
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(3)
        st = c.get(f"/knowledge/documents/{doc_id}/status").json().get("data", {})
        if st.get("status") == "ready":
            print(f"ok   document indexed ({st.get('chunkCount')} chunks)")
            break
        if st.get("status") == "failed":
            print(f"FAIL ingestion: {st.get('failureReason')}")
            sys.exit(1)
    else:
        print("FAIL ingestion timed out (is the ingestion worker running?)")
        sys.exit(1)
    kb = check(c.get(f"/knowledge/{kb['id']}"), "kb detail")
    if kb["status"] != "indexed":
        print(f"FAIL KB status '{kb['status']}', expected 'indexed'")
        sys.exit(1)


def stage_channel(c: httpx.Client) -> None:
    check(c.put(f"/bots/{BOT}/channels/voice", json={
        "config": {"phoneNumber": PHONE, "telephonyProvider": "freeswitch"},
        "workflowName": WORKFLOW_NAME,
    }), f"voice channel {PHONE}")


def stage_scenarios(c: httpx.Client) -> None:
    existing = {s["name"] for s in check(c.get(f"/bots/{BOT}/scenarios"),
                                         "list scenarios")}
    for suite, steps, name in SCENARIOS:
        if name in existing:
            continue
        check(c.post(f"/bots/{BOT}/scenarios",
                     json={"name": name, "suite": suite, "steps": steps}),
              f"scenario '{name}'")
    result = check(c.post(f"/bots/{BOT}/scenarios/run"), "run scenario suite")
    if result.get("failed"):
        print(f"FAIL suite: {result}")
        sys.exit(1)


def stage_recompute(c: httpx.Client) -> None:
    bot = check(c.post(f"/bots/{BOT}/readiness/recompute"), "recompute readiness")
    done = [r["id"] for r in bot["readiness"] if r["done"]]
    missing = [f"{r['id']} {r['label']}" for r in bot["readiness"] if not r["done"]]
    print(f"     {bot['name']}: {len(done)}/{len(bot['readiness'])} green"
          + (f" — missing: {missing}" if missing else ""))
    if missing:
        sys.exit(1)


def stage_publish(c: httpx.Client) -> None:
    bot = check(c.patch(f"/bots/{BOT}", json={"status": "published"}),
                "bot status -> published")
    print(f"     {bot['name']} status={bot['status']}")


def stage_activate(c: httpx.Client) -> None:
    ch = check(c.post(f"/bots/{BOT}/channels/voice/activate"),
               "activate voice channel")
    print(f"     voice channel enabled={ch.get('enabled')} "
          f"status={ch.get('status')} detail={ch.get('detail')}")


STAGES = {
    "knowledge": stage_knowledge,
    "channel": stage_channel,
    "scenarios": stage_scenarios,
    "recompute": stage_recompute,
    "publish": stage_publish,
    "activate": stage_activate,
}


def main() -> None:
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    c = client()
    if stage == "all":
        for fn in STAGES.values():
            fn(c)
    elif stage in STAGES:
        STAGES[stage](c)
    else:
        print(f"unknown stage '{stage}' — use one of: {', '.join(STAGES)}, all")
        sys.exit(2)


if __name__ == "__main__":
    main()
