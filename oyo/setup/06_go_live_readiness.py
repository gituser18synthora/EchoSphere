"""Bring the three OYO bots to a genuinely green readiness checklist via REST.

Every readiness item is DERIVED by the platform (shared/readiness.py) from
real configuration, so this script only creates the missing configuration —
it never touches the checklist flags themselves:

  r1  knowledge   - tenant-scoped KB built from the oyo_doc/ source documents,
                    indexed by the real ingestion worker (run it first)
  r2  voice       - the customer bot gets a voice profile (the other two
                    already have one)
  r6  channels    - voice channels for the PM + stock bots (customer bot
                    already has one)
  r7  scenarios   - the 34-scenario chat regression suite mirrored as platform
                    test scenarios, then executed via the suite runner.
                    Run oyo/tests/run_chat_scenarios.py first — record the
                    platform suite only when the real suite passes.
  recompute       - re-derives r1..r7 for each bot from live state (heals the
                    items that were configured before derivation existed:
                    approved prompts r3, approved workflows r5, the customer
                    bot's existing channel r6)

Stages (run: python 06_go_live_readiness.py <stage>): voice, knowledge,
channels, scenarios, recompute, all. Idempotent — safe to rerun.
"""

import pathlib
import sys
import time

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
TENANT = "tn_de5cc992b1e9"
BOT_CUSTOMER = "bot_e8cf0b05bb79"
BOT_PM = "bot_99177674902a"
BOT_STOCK = "bot_78b6aa83d94a"
BOTS = (BOT_CUSTOMER, BOT_PM, BOT_STOCK)

DOC_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "oyo_doc"

# KBs are BOT-scoped on purpose. A knowledge base authorized for a bot makes
# the TurnRouter send question-shaped free-text turns to retrieval, so each
# bot only gets content that is correct to speak to ITS audience:
#   - customer bot: guest-facing FAQ (policy questions -> KB is desirable;
#     personal booking facts stay with the LLM via the booking_fact_question
#     intent, which outranks KB signals in the router)
#   - PM / stock bots: the internal spec + call-script playbook
LEGACY_TENANT_KB = "OYO Booking Confirmation Docs"  # replaced by bot-scoped KBs
KBS = {
    BOT_CUSTOMER: ("OYO Guest Booking FAQ",
                   "Guest-facing booking-confirmation FAQ (shift, voucher, timings)",
                   ("OYO_Guest_Booking_FAQ.md",)),
    BOT_PM: ("OYO PM Verification Playbook",
             "Internal booking-confirmation spec and PM call script",
             ("Booking_Confirmation.docx", "Booking Confirmation prompt.docx")),
    BOT_STOCK: ("OYO Stock Validation Playbook",
                "Internal booking-confirmation spec (stock validation flows)",
                ("Booking_Confirmation.docx",)),
}

# Post-workflow personal-fact questions must reach the LLM (which holds the
# caller's verified runtime context), not document retrieval. A no-route
# intent wins over the router's generic KB question heuristics.
FACT_INTENT = {
    "name": "booking_fact_question",
    "description": "Guest asks about facts of their own verified booking "
                    "(dates, hotel, occupancy, amounts) — answered by the LLM "
                    "from runtime context, never from document retrieval.",
    "samples": [
        "when is my check-in",
        "when is my check-out",
        "which hotel",
        "what is my hotel name",
        "can you confirm my hotel name",
        "tell me my hotel name",
        "what are my booking dates",
        "how much did i pay",
        "what is my pending amount",
        "what is my payment status",
        "what is my occupancy",
    ],
    "confidenceThreshold": 0.4,
    "category": "booking",
}

CUSTOMER_VOICE = "vp-el-anvi"  # ElevenLabs Anvi; PM/stock bots use Niraj/Viraj

CHANNELS = {
    BOT_PM: {"phoneNumber": "+918047133634",
             "workflowName": "OYO property verification journey"},
    BOT_STOCK: {"phoneNumber": "+918047133635",
                "workflowName": "OYO stock validation journey"},
}

# Mirrors oyo/tests/run_chat_scenarios.py (30 single-bot + E1-E4 cross-bot;
# the cross-bot runs assert on the customer bot's final behavior).
SCENARIOS = {
    BOT_CUSTOMER: [
        ("Booking status", 4, "01 system confirmation only (601001)"),
        ("Booking status", 4, "02 cancelled + dispute -> transfer (601002)"),
        ("Booking status", 4, "03 cancelled by customer (601013)"),
        ("PM orchestration", 4, "04 PM confirms booking (601001)"),
        ("PM orchestration", 4, "05 PM no answer -> stock confirms (601003)"),
        ("PM orchestration", 6, "06 overbooked -> shift accepted (601004)"),
        ("PM orchestration", 4, "07 overbooked-but-available -> penalty accepted (601005)"),
        ("PM orchestration", 4, "08 maintenance + alternate room (601006)"),
        ("PM orchestration", 5, "09 maintenance no room -> shift declined (601007)"),
        ("PM orchestration", 4, "10 price meets ARR -> honored (601008)"),
        ("PM orchestration", 4, "11 price below ARR -> compensation added (601009)"),
        ("PM orchestration", 6, "12 price refused -> shift (601010)"),
        ("PM orchestration", 6, "13 PM + stock unavailable -> shift (601011)"),
        ("Voucher", 6, "14 voucher to email on file (601001)"),
        ("Voucher", 6, "15 voucher, no email on file (601012)"),
        ("Guard rails", 5, "16 booking details answered immediately (601001)"),
        ("Guard rails", 3, "17 verification failure (601001, wrong name)"),
        ("Guard rails", 3, "18 unknown booking id"),
        ("Guard rails", 1, "19 out of scope -> handoff"),
        ("Guard rails", 1, "20 out of scope refund -> handoff"),
        ("Guard rails", 1, "31 guest FAQ from knowledge (overbooked policy)"),
        ("Cross-bot", 8, "E1 PM bot denies overbooked -> customer bot says overbooked (601004)"),
        ("Cross-bot", 8, "E2 PM bot honors after penalty advisory -> customer bot confirms (601010)"),
        ("Cross-bot", 7, "E3 PM unreachable -> stock bot confirms -> customer bot confirms (601003)"),
        ("Cross-bot", 7, "E4 PM unreachable -> stock bot cannot confirm -> shift offered (601011)"),
    ],
    BOT_PM: [
        ("PM negotiation", 3, "21 PM confirms (601012)"),
        ("PM negotiation", 5, "22 PM overbooked but availability -> penalty -> accepts (601005)"),
        ("PM negotiation", 3, "23 PM genuinely overbooked (601004)"),
        ("PM negotiation", 5, "24 PM maintenance, no alternate room (601007)"),
        ("PM negotiation", 4, "25 PM maintenance with alternate room (601006)"),
        ("PM negotiation", 4, "26 PM price low, meets ARR -> accepts (601008)"),
        ("PM negotiation", 5, "27 PM price low, below ARR -> compensation -> accepts (601009)"),
        ("PM negotiation", 5, "28 PM price refused even with compensation (601010)"),
    ],
    BOT_STOCK: [
        ("Stock validation", 3, "29 stock team confirms (601011)"),
        ("Stock validation", 3, "30 stock team cannot confirm (601004)"),
    ],
}


def client() -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/auth/login", json={"email": "oyo.config@oyo.com",
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


# ── stage: voice ─────────────────────────────────────────────────────────────


def stage_voice(c: httpx.Client) -> None:
    bot = check(c.get(f"/bots/{BOT_CUSTOMER}"), "get customer bot")
    if bot.get("voiceId") != CUSTOMER_VOICE:
        check(c.patch(f"/bots/{BOT_CUSTOMER}", json={"voiceId": CUSTOMER_VOICE}),
              f"customer bot voiceId -> {CUSTOMER_VOICE}")
    settings = check(c.get(f"/bots/{BOT_CUSTOMER}/voice-settings"),
                     "get customer voice settings")
    if settings.get("voiceId") != CUSTOMER_VOICE:
        check(c.put(f"/bots/{BOT_CUSTOMER}/voice-settings",
                    json={"voiceId": CUSTOMER_VOICE}),
              "customer voice settings voiceId")


# ── stage: knowledge ─────────────────────────────────────────────────────────


def _list_kbs(c: httpx.Client) -> list[dict]:
    data = check(c.get("/knowledge", params={"pageSize": 100}), "list knowledge")
    return data if isinstance(data, list) else data.get("items", [])


def _await_ingestion(c: httpx.Client, doc_ids: list[str]) -> None:
    deadline = time.time() + 180
    pending = set(doc_ids)
    while pending and time.time() < deadline:
        time.sleep(3)
        for doc_id in sorted(pending):
            st = c.get(f"/knowledge/documents/{doc_id}/status").json().get("data", {})
            status = st.get("status")
            if status == "ready":
                print(f"ok   document {doc_id} indexed ({st.get('chunkCount')} chunks)")
                pending.discard(doc_id)
            elif status == "failed":
                print(f"FAIL document {doc_id}: {st.get('failureReason')}")
                sys.exit(1)
    if pending:
        print(f"FAIL ingestion timed out for: {sorted(pending)} "
              "(is the ingestion worker running?)")
        sys.exit(1)


def stage_knowledge(c: httpx.Client) -> None:
    existing = {k["name"]: k for k in _list_kbs(c)}
    legacy = existing.get(LEGACY_TENANT_KB)
    if legacy is not None:
        check(c.delete(f"/knowledge/{legacy['id']}"),
              f"archive legacy tenant KB '{LEGACY_TENANT_KB}'")
    doc_ids = []
    kb_ids = []
    for bot_id, (name, detail, files) in KBS.items():
        kb = existing.get(name)
        if kb is None:
            kb = check(c.post("/knowledge", json={
                "scope": "bot", "botId": bot_id, "type": "document",
                "name": name, "detail": detail,
            }), f"create bot KB '{name}'")
        kb_ids.append(kb["id"])
        for fname in files:
            with (DOC_DIR / fname).open("rb") as f:
                data = check(
                    c.post(f"/knowledge/{kb['id']}/documents",
                           files={"file": (fname, f, "application/octet-stream")}),
                    f"upload {fname} -> {name}",
                )
            doc_ids.append(data["documentId"])
    _await_ingestion(c, doc_ids)
    for kb_id in kb_ids:
        kb = check(c.get(f"/knowledge/{kb_id}"), "kb detail")
        if kb["status"] != "indexed":
            print(f"FAIL KB {kb['name']} status is '{kb['status']}', expected 'indexed'")
            sys.exit(1)


# ── stage: intents ───────────────────────────────────────────────────────────


def stage_intents(c: httpx.Client) -> None:
    rows = check(c.get(f"/bots/{BOT_CUSTOMER}/intents"), "list customer intents")
    if any(i["name"] == FACT_INTENT["name"] for i in rows):
        print(f"ok   intent '{FACT_INTENT['name']}' already present")
        return
    check(c.post(f"/bots/{BOT_CUSTOMER}/intents", json=FACT_INTENT),
          f"create intent '{FACT_INTENT['name']}'")


# ── stage: channels ──────────────────────────────────────────────────────────


def stage_channels(c: httpx.Client) -> None:
    for bot_id, cfg in CHANNELS.items():
        check(c.put(f"/bots/{bot_id}/channels/voice", json={
            "config": {"phoneNumber": cfg["phoneNumber"],
                       "telephonyProvider": "freeswitch"},
            "workflowName": cfg["workflowName"],
        }), f"voice channel {cfg['phoneNumber']} -> {bot_id}")


# ── stage: scenarios ─────────────────────────────────────────────────────────


def stage_scenarios(c: httpx.Client) -> None:
    for bot_id, rows in SCENARIOS.items():
        existing = {s["name"] for s in check(
            c.get(f"/bots/{bot_id}/scenarios"), f"list scenarios {bot_id}")}
        for suite, steps, name in rows:
            if name in existing:
                continue
            check(c.post(f"/bots/{bot_id}/scenarios",
                         json={"name": name, "suite": suite, "steps": steps}),
                  f"scenario '{name}'")
        result = check(c.post(f"/bots/{bot_id}/scenarios/run"), f"run suite {bot_id}")
        if result["failed"]:
            print(f"FAIL suite for {bot_id}: {result}")
            sys.exit(1)


# ── stage: recompute ─────────────────────────────────────────────────────────


def stage_recompute(c: httpx.Client) -> None:
    all_green = True
    for bot_id in BOTS:
        bot = check(c.post(f"/bots/{bot_id}/readiness/recompute"),
                    f"recompute readiness {bot_id}")
        done = [r["id"] for r in bot["readiness"] if r["done"]]
        missing = [f"{r['id']} {r['label']}" for r in bot["readiness"] if not r["done"]]
        print(f"     {bot['name']}: {len(done)}/{len(bot['readiness'])} green"
              + (f" — missing: {missing}" if missing else ""))
        all_green = all_green and not missing
    if not all_green:
        sys.exit(1)


STAGES = {
    "voice": stage_voice,
    "knowledge": stage_knowledge,
    "intents": stage_intents,
    "channels": stage_channels,
    "scenarios": stage_scenarios,
    "recompute": stage_recompute,
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
