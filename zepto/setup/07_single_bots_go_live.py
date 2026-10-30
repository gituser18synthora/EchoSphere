"""Stage 07 — go-live for the four dedicated single-concern Zepto bots:
per-bot knowledge base, voice channel, platform scenarios, readiness,
publish, channel activation. The combined bot (bot_3213a1508a96) and its
channel (+918047133650) are not touched.

Per-bot KBs are CONCERN-SPECIFIC on purpose: KB presence flips question
routing to retrieval, and a dedicated line must never retrieve (and speak)
another concern's process — the same isolation rule the workflows follow.

Stages: knowledge, channel, scenarios, recompute, publish, activate, all.
Run: env/bin/python zepto/setup/07_single_bots_go_live.py [stage]
"""

import json
import pathlib
import sys
import time

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE_FILE = __file__.rsplit("/", 1)[0] + "/zepto_config_state.json"
STATE = json.load(open(STATE_FILE))
DOC_DIR = pathlib.Path(__file__).resolve().parent.parent / "docs"

BOTS = [
    {
        "state_key": "BOT_MDND",
        "kb_name": "Zepto MDND FAQ",
        "doc": "Zepto_MDND_FAQ.md",
        "phone": "+918047133651",
        "workflow_name": "Zepto MDND concern journey",
        "kb_detail": ("Partner-facing FAQ for the dedicated MDND line: what "
                      "MDND means, what the line collects, the review/"
                      "callback process, the no-payment-details rule."),
    },
    {
        "state_key": "BOT_UNIFORM",
        "kb_name": "Zepto Raincoat T-shirt Bag FAQ",
        "doc": "Zepto_Raincoat_Tshirt_Bag_FAQ.md",
        "phone": "+918047133652",
        "workflow_name": "Zepto raincoat t-shirt bag deduction journey",
        "kb_detail": ("Partner-facing FAQ for the dedicated Raincoat/T-shirt/"
                      "Bag deduction line: what the deduction is, what the "
                      "line collects, the review/callback process, the "
                      "no-payment-details rule."),
    },
    {
        "state_key": "BOT_ONBOARDING",
        "kb_name": "Zepto Onboarding Fee FAQ",
        "doc": "Zepto_Onboarding_Fee_FAQ.md",
        "phone": "+918047133653",
        "workflow_name": "Zepto onboarding fee deduction journey",
        "kb_detail": ("Partner-facing FAQ for the dedicated Onboarding Fee "
                      "deduction line: what the fee deduction is, what the "
                      "line collects, the review/callback process, the "
                      "no-payment-details rule."),
    },
    {
        "state_key": "BOT_RTO",
        "kb_name": "Zepto RTO FAQ",
        "doc": "Zepto_RTO_FAQ.md",
        "phone": "+918047133654",
        "workflow_name": "Zepto RTO issue journey",
        "kb_detail": ("Partner-facing FAQ for the dedicated RTO line: what "
                      "RTO means, what the line collects, the review/"
                      "callback process, the no-payment-details rule."),
    },
]

# Mirrors zepto/tests/run_single_bot_scenarios.py — record the platform
# suite only when the real suite passes.
SCENARIOS = {
    "BOT_MDND": [
        # v2 — rebuilt from the reference recording (zepto-call.mp4).
        ("Reference call", 7, "v2-01 reference-call replay (Hinglish), mocked ticket"),
        ("Multi-answer", 5, "v3-02 one utterance answers all incident enquiries, none re-asked"),
        ("Multi-answer", 3, "v2-03 partial answer -> only the missing question asked"),
        ("Verification", 7, "v2-04 summary rejected -> correction captured -> re-verified"),
        ("Happy path", 7, "v2-05 English caller end to end, live API fallback"),
        ("Call handling", 1, "v2-06 explicit human request -> support handover"),
        ("Knowledge", 1, "v2-07 'MDND kya hota hai' answered from the FAQ KB"),
        ("Missing context", 2, "v3-08 all missing values supplied in one utterance"),
        ("Missing context", 3, "v3-09 partial values -> only missing date asked"),
        ("Multi-answer", 3, "v3-10 confirmation plus other-deduction answer skips repeat"),
        ("Interruption", 2, "v3-11 interrupted combined answer remains captured"),
    ],
    "BOT_UNIFORM": [
        ("Happy path", 7, "01 Hinglish caller, kit received, mocked ticket"),
        ("Happy path", 7, "02 English caller, kit NOT received, live API fallback"),
        ("Call handling", 1, "03 explicit human request -> support handover"),
        ("Knowledge", 1, "04 deduction definition answered from the FAQ KB"),
    ],
    "BOT_ONBOARDING": [
        ("Happy path", 7, "01 Hinglish caller, paid at joining, mocked ticket"),
        ("Happy path", 7, "02 English caller, paid nothing, live API fallback"),
        ("Call handling", 1, "03 explicit human request -> support handover"),
        ("Knowledge", 1, "04 onboarding-fee definition answered from the FAQ KB"),
    ],
    "BOT_RTO": [
        ("Conditions", 8, "01 Hinglish caller, handed to store -> date follow-up, mocked ticket"),
        ("Conditions", 7, "02 English caller, NOT handed to store -> follow-up skipped"),
        ("Call handling", 1, "03 explicit human request -> support handover"),
        ("Knowledge", 1, "04 'RTO ka matlab' answered from the FAQ KB"),
    ],
}


def client() -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/auth/login", json={"email": "zepto.config@zepto.com",
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
    by_name = {k["name"]: k for k in rows}
    for spec in BOTS:
        bot_id = STATE[spec["state_key"]]
        kb = by_name.get(spec["kb_name"])
        if kb is None:
            kb = check(c.post("/knowledge", json={
                "scope": "bot", "botId": bot_id, "type": "document",
                "name": spec["kb_name"], "detail": spec["kb_detail"],
            }), f"create bot KB '{spec['kb_name']}'")
        with (DOC_DIR / spec["doc"]).open("rb") as f:
            doc = check(
                c.post(f"/knowledge/{kb['id']}/documents",
                       files={"file": (spec["doc"], f, "text/markdown")}),
                f"upload {spec['doc']}")
        doc_id = doc["documentId"]
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(3)
            st = c.get(f"/knowledge/documents/{doc_id}/status").json().get("data", {})
            if st.get("status") == "ready":
                print(f"     indexed ({st.get('chunkCount')} chunks)")
                break
            if st.get("status") == "failed":
                print(f"FAIL ingestion: {st.get('failureReason')}")
                sys.exit(1)
        else:
            print("FAIL ingestion timed out (is the ingestion worker running?)")
            sys.exit(1)


def stage_channel(c: httpx.Client) -> None:
    for spec in BOTS:
        bot_id = STATE[spec["state_key"]]
        check(c.put(f"/bots/{bot_id}/channels/voice", json={
            "config": {"phoneNumber": spec["phone"],
                       "telephonyProvider": "freeswitch"},
            "workflowName": spec["workflow_name"],
        }), f"voice channel {spec['phone']} -> {spec['state_key']}")


def stage_scenarios(c: httpx.Client) -> None:
    for spec in BOTS:
        bot_id = STATE[spec["state_key"]]
        existing = {s["name"] for s in check(c.get(f"/bots/{bot_id}/scenarios"),
                                             f"list scenarios {spec['state_key']}")}
        for suite, steps, name in SCENARIOS[spec["state_key"]]:
            if name in existing:
                continue
            check(c.post(f"/bots/{bot_id}/scenarios",
                         json={"name": name, "suite": suite, "steps": steps}),
                  f"scenario '{name}'")
        result = check(c.post(f"/bots/{bot_id}/scenarios/run"),
                       f"run scenario suite {spec['state_key']}")
        if result.get("failed"):
            print(f"FAIL suite {spec['state_key']}: {result}")
            sys.exit(1)


def stage_recompute(c: httpx.Client) -> None:
    for spec in BOTS:
        bot_id = STATE[spec["state_key"]]
        bot = check(c.post(f"/bots/{bot_id}/readiness/recompute"),
                    f"recompute readiness {spec['state_key']}")
        missing = [f"{r['id']} {r['label']}" for r in bot["readiness"]
                   if not r["done"]]
        print(f"     {bot['name']}: "
              f"{len(bot['readiness']) - len(missing)}/{len(bot['readiness'])} green"
              + (f" — missing: {missing}" if missing else ""))
        if missing:
            sys.exit(1)


def stage_publish(c: httpx.Client) -> None:
    for spec in BOTS:
        bot_id = STATE[spec["state_key"]]
        bot = check(c.patch(f"/bots/{bot_id}", json={"status": "published"}),
                    f"publish {spec['state_key']}")
        print(f"     {bot['name']} status={bot['status']}")


def stage_activate(c: httpx.Client) -> None:
    for spec in BOTS:
        bot_id = STATE[spec["state_key"]]
        ch = check(c.post(f"/bots/{bot_id}/channels/voice/activate"),
                   f"activate voice channel {spec['state_key']}")
        print(f"     enabled={ch.get('enabled')} status={ch.get('status')} "
              f"detail={ch.get('detail')}")


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
