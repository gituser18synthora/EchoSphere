"""Stage 05 — go-live: knowledge base, voice channel, scenarios, readiness,
publish.

Readiness items are DERIVED by the platform (shared/readiness.py); this stage
only creates the missing real configuration:

  r1 knowledge  - bot-scoped KB from honasa/docs/Honasa_Order_Returns_FAQ.md
                  (bot-scoped on purpose: KB presence flips question routing
                  to retrieval, so the bot only gets content that is correct
                  to speak; personal order-fact questions stay with the LLM
                  via the no-route order_fact_question intent)
  r6 channels   - a voice channel bound to the order-support workflow
  r7 scenarios  - the regression suite mirrored as platform test scenarios and
                  executed. Run honasa/tests/run_chat_scenarios.py FIRST —
                  record the platform suite only when the real suite passes.
  recompute     - re-derives r1..r7 from live state
  publish       - bot status -> published

Stages: knowledge, channel, scenarios, recompute, publish, all.
Run: env/bin/python honasa/setup/05_go_live.py [stage]
"""

import json
import pathlib
import sys
import time

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE_FILE = __file__.rsplit("/", 1)[0] + "/honasa_config_state.json"
BOT = json.load(open(STATE_FILE))["BOT"]

DOC_DIR = pathlib.Path(__file__).resolve().parent.parent / "docs"
KB_NAME = "Honasa Orders & Returns FAQ"
PHONE = "+918047133640"

SCENARIOS = [
    ("Order information", 3, "01 order status with ETA (7001002)"),
    ("Order information", 3, "02 broad where-is-my-order (7001003)"),
    ("Order information", 4, "03 order amount + discount (7001001)"),
    ("Order information", 4, "04 refund status in process (7001006)"),
    ("Order information", 3, "05 no refund on order (7001007)"),
    ("Order information", 4, "06 tracking link over WhatsApp (7001002)"),
    ("Order information", 4, "07 tracking not live yet (7001004)"),
    ("Order information", 3, "08 ETA unavailable — honest answer (7001004)"),
    ("Order information", 3, "09 lookup by registered phone (9876501003)"),
    ("Order information", 4, "10 phone with multiple orders (9876509999)"),
    ("Return / Replacement", 4, "11 change-of-mind return raised (7001001)"),
    ("Return / Replacement", 4, "12 return window closed (7001005)"),
    ("Return / Replacement", 4, "13 non-returnable category (7001008)"),
    ("Return / Replacement", 4, "14 eligibility question (7001007)"),
    ("Return / Replacement", 4, "15 damaged -> replacement (7001011)"),
    ("Return / Replacement", 4, "16 damaged -> refund (7001001)"),
    ("Return / Replacement", 4, "17 wrong item -> correct product (7001011)"),
    ("Return / Replacement", 4, "18 missing item -> send item (7001012)"),
    ("Return / Replacement", 4, "19 defective/expired -> replacement (7001007)"),
    ("Return / Replacement", 5, "20 quality window closed -> agent (7001005)"),
    ("Return / Replacement", 4, "21 vague reason defaults to return (7001001)"),
    ("Return / Replacement", 4, "22 return declined at confirm (7001001)"),
    ("Robustness", 3, "23 wrong then correct order id (retry lookup)"),
    ("Robustness", 3, "24 unknown order id -> escalation offer"),
    ("Robustness", 4, "25 request changes mid-call (status -> return)"),
    ("Robustness", 4, "26 off-script question mid-flow"),
    ("Robustness", 3, "27 repeated question re-answered"),
    ("Guard rails", 1, "28 cancellation out of scope -> handoff"),
    ("Guard rails", 1, "29 complaint escalation -> handoff"),
    ("Guard rails", 1, "30 explicit agent request -> handoff"),
    ("Guard rails", 1, "31 stray refund word stays in flow"),
    ("Guard rails", 2, "32 unsupported request -> polite decline"),
    ("Guard rails", 1, "33 return policy from knowledge base"),
]


def client() -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/auth/login", json={"email": "honasa.config@honasa.com",
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
            "detail": ("Customer-facing FAQ for the two supported topics: "
                       "order information and returns/replacements."),
        }), f"create bot KB '{KB_NAME}'")
    fname = "Honasa_Order_Returns_FAQ.md"
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
        "workflowName": "Honasa order support journey",
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


STAGES = {
    "knowledge": stage_knowledge,
    "channel": stage_channel,
    "scenarios": stage_scenarios,
    "recompute": stage_recompute,
    "publish": stage_publish,
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
