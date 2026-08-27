"""End-to-end scenario tests for the Honasa bot via POST /bots/{id}/testing/chat.

Each scenario = list of (utterance, expectations) turns. Expectation kinds:
  reply  - substring of the (lowercased) bot reply
  route  - substring of the router route
  status - substring of the workflow status (e.g. handoff)
  trace  - substring of the comma-joined node trace
  slots  - substring of the JSON-dumped workflow slots (lowercased)
  done   - "true"/"false"
  state  - substring of the mock service's runtime state (checked AFTER the
           scenario, against honasa mock /api/v1/state)

Run:  env/bin/python honasa/tests/run_chat_scenarios.py [name-filter]
Requires: backend API on 9001, honasa mock on 9022.
"""

import json
import os
import pathlib
import sys
from datetime import date, timedelta

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
MOCK = "http://127.0.0.1:9022/api/v1"
STATE_FILE = (pathlib.Path(__file__).resolve().parent.parent
              / "setup" / "honasa_config_state.json")
BOT = json.load(open(STATE_FILE))["BOT"]
MOCK_STATE_JSON = (pathlib.Path(__file__).resolve().parent.parent
                   / "data" / "runtime_state.json")

c = httpx.Client(base_url=BASE, timeout=60)
r = c.post("/auth/login", json={"email": "honasa.config@honasa.com",
                                "password": "Demo@2026!"})
r.raise_for_status()
c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"
mock = httpx.Client(base_url=MOCK, timeout=30)

PASS, FAIL = 0, 0
FAILURES = []


def month_in(days_ahead: int) -> str:
    """Lowercase month name of today+N — ETA assertions never rot."""
    return (date.today() + timedelta(days=days_ahead)).strftime("%B").lower()


def reset_mock_state():
    if MOCK_STATE_JSON.exists():
        os.remove(MOCK_STATE_JSON)


def turn(session, message, history):
    r = c.post(f"/bots/{BOT}/testing/chat",
               json={"message": message, "sessionId": session,
                     "messages": history})
    r.raise_for_status()
    return r.json()["data"]


def run(name, turns, verbose=False):
    global PASS, FAIL
    session = f"hn_{abs(hash(name)) % 10**10}"
    history = []
    ok_all = True
    log = []
    state_checks = []
    for message, expect in turns:
        d = turn(session, message, history)
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": d["reply"] or ""})
        wf = d.get("workflow") or {}
        log.append(f"    > {message}\n      route={d['route']} "
                   f"status={wf.get('status')} done={d['done']}\n"
                   f"      reply: {(d['reply'] or '')[:220]}")
        for kind, want in expect:
            if kind == "state":
                state_checks.append(want)
                continue
            got = {"reply": (d["reply"] or "").lower(),
                   "route": d["route"] or "",
                   "status": str(wf.get("status")),
                   "trace": ",".join(wf.get("nodeTrace") or []),
                   "slots": json.dumps(wf.get("slots") or {}).lower(),
                   "done": str(d["done"]).lower()}[kind]
            if want.lower() not in got.lower():
                ok_all = False
                log.append(f"      EXPECT {kind} ~ '{want}' — NOT FOUND")
    if state_checks:
        blob = json.dumps(mock.get("/state").json()).lower()
        for want in state_checks:
            if want.lower() not in blob:
                ok_all = False
                log.append(f"      EXPECT mock state ~ '{want}' — NOT FOUND")
    if ok_all:
        PASS += 1
        print(f"PASS {name}")
        if verbose:
            print("\n".join(log))
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"FAIL {name}")
        print("\n".join(log))


R, RT, ST, TR, SL, DN, MS = "reply", "route", "status", "trace", "slots", "done", "state"

only = sys.argv[1] if len(sys.argv) > 1 else None

SCENARIOS = [
    # ── Order / Information ──────────────────────────────────────────────────
    ("01 order status with ETA (7001002)", [
        ("where is my order?", [(RT, "workflow"), (R, "order id")]),
        ("7001002", [(TR, "n_hub"), (SL, '"order_status": "shipped"')]),
        ("when will it arrive?", [(TR, "n_msg_order_answer"),
                                  (R, month_in(2))]),
    ]),
    # Post-verification fact questions that name a verified fact are answered
    # by the LLM over the folded verified context (the platform's
    # verified-context-question route) — assert the ANSWER, not the mechanism.
    ("02 broad where-is-my-order (7001003)", [
        ("I want to check my order status", [(R, "order id")]),
        ("7001003", [(TR, "n_hub")]),
        ("where is my order right now?", [(R, "delivery")]),
    ]),
    ("03 order amount + discount (7001001)", [
        ("what is my order amount?", [(R, "order id")]),
        ("7001001", [(TR, "n_hub")]),
        ("what was the order amount?", [(R, "rupees")]),
        ("and did I get any discount?", [(R, "discount")]),
    ]),
    ("04 refund status in process (7001006)", [
        ("where is my refund?", [(RT, "workflow"), (R, "order id")]),
        ("7001006", [(TR, "n_hub"), (SL, '"refund_status": "in_process"')]),
        ("where is my refund?", [(TR, "n_msg_order_answer"), (R, "refund")]),
    ]),
    ("05 no refund on order (7001007)", [
        ("refund status of my order", [(R, "order id")]),
        ("7001007", [(TR, "n_hub"), (SL, '"refund_status": "none"')]),
        ("where is my refund?", [(TR, "n_msg_order_answer"), (R, "refund")]),
    ]),
    ("06 tracking link over WhatsApp (7001002)", [
        ("can you share the tracking link?", [(R, "order id")]),
        ("7001002", [(TR, "n_hub")]),
        ("send me the tracking link", [(TR, "n_api_tracklink"),
                                       (R, "whatsapp"),
                                       (SL, '"tracking_link_sent": true')]),
        ("that's all, thanks", [(DN, "true")]),
    ]),
    ("07 tracking not live yet (7001004)", [
        ("track my order", [(R, "order id")]),
        ("7001004", [(TR, "n_hub")]),
        ("send me the tracking link", [(TR, "n_msg_tracklink_fail"),
                                       (R, "isn't available for this order yet")]),
    ]),
    ("08 ETA unavailable — honest answer (7001004)", [
        ("when will my order arrive?", [(R, "order id")]),
        ("7001004", [(TR, "n_hub"), (SL, '"order_status": "processing"')]),
        ("when will it be delivered?", [(TR, "n_msg_order_answer"),
                                        (R, "available")]),
    ]),
    ("09 lookup by registered phone (9876501003)", [
        ("where is my order?", [(R, "order id")]),
        ("my registered number is 9876501003", [
            (TR, "n_hub"), (SL, '"order_id": "7001003"')]),
    ]),
    ("10 phone with multiple orders (9876509999)", [
        ("order status please", [(R, "order id")]),
        ("9876509999", [(TR, "n_hub"), (SL, '"order_id": "7001009"'),
                        (SL, '"multiple_orders_on_phone": true')]),
        ("what is the status?", [(TR, "n_msg_order_answer"), (R, "shipped")]),
    ]),

    # ── Return / Replacement ─────────────────────────────────────────────────
    ("11 change-of-mind return raised (7001001)", [
        ("I want to return my product", [(RT, "workflow"), (R, "order id")]),
        ("7001001", [(TR, "n_hub")]),
        ("I just don't need it anymore", [(TR, "n_intent_ret_confirm"),
                                          (R, "eligible for return"), (R, "?")]),
        ("yes please", [(TR, "n_msg_return_ok"), (R, "whatsapp"),
                        (SL, '"resolution_request_id"'),
                        (MS, '"issue_type": "no_longer_needed"'),
                        (MS, '"resolution": "return"')]),
        ("no, that's all", [(DN, "true")]),
    ]),
    ("12 return window closed (7001005)", [
        ("I want to return my order", [(R, "order id")]),
        ("7001005", [(TR, "n_hub")]),
        ("I don't need it anymore", [(TR, "n_msg_ret_ineligible"),
                                     (SL, '"return_ineligible_reason": "window_closed"'),
                                     (R, "?")]),
        ("yes, connect me", [(ST, "handoff"), (R, "stay on the line")]),
    ]),
    ("13 non-returnable category (7001008)", [
        ("can I return my product?", [(R, "order id")]),
        ("7001008", [(TR, "n_hub"),
                     (SL, '"return_ineligible_reason": "category_not_returnable"')]),
        # Eligibility QUESTION: answered from the verified eligibility facts
        # (chat reroute) — the answer must be a truthful "not eligible".
        ("can I return this product?", [(R, "not eligible")]),
        ("no, it's okay", [(DN, "true")]),
    ]),
    ("14 eligibility question answered (7001007)", [
        ("can I return my product?", [(RT, "workflow"), (R, "order id")]),
        ("7001007", [(TR, "n_hub"), (SL, '"return_eligible": true')]),
        ("can I return this?", [(R, "return")]),
        ("no, I was just asking", [(DN, "true")]),
    ]),
    ("15 damaged -> replacement (7001011)", [
        ("I received a damaged product", [(RT, "workflow"), (R, "order id")]),
        ("7001011", [(TR, "n_hub")]),
        ("the face wash arrived damaged", [(TR, "n_ask_dmg"),
                                           (R, "sorry"), (R, "damage")]),
        ("the tube is torn and it leaked everywhere", [
            (TR, "n_intent_dmg_choice"), (R, "replacement")]),
        ("replacement please", [(TR, "n_msg_replace_ok"), (R, "whatsapp"),
                                (SL, '"resolution_type": "replacement"'),
                                (MS, '"issue_type": "damaged"'),
                                (MS, '"resolution": "replacement"'),
                                (MS, "torn")]),
    ]),
    ("16 damaged -> refund (7001001)", [
        ("my product came broken", [(RT, "workflow"), (R, "order id")]),
        ("7001001", [(TR, "n_hub")]),
        ("the hair oil bottle is broken", [(TR, "n_ask_dmg")]),
        ("the cap cracked and half the oil spilled", [
            (TR, "n_intent_dmg_choice")]),
        ("I'd like a refund, return it", [(TR, "n_msg_return_ok"),
                                          (R, "whatsapp"),
                                          (MS, '"issue_type": "damaged"'),
                                          (MS, '"resolution": "return"')]),
    ]),
    ("17 wrong item -> correct product (7001011)", [
        ("I received the wrong product", [(RT, "workflow"), (R, "order id")]),
        ("7001011", [(TR, "n_hub")]),
        ("you sent me the wrong item", [(TR, "n_ask_wrong"), (R, "ordered")]),
        ("I got a face serum but I had ordered the radiance face wash", [
            (TR, "n_intent_wrong_choice"), (R, "replacement")]),
        ("send the correct product", [(TR, "n_msg_replace_ok"),
                                      (R, "whatsapp"),
                                      (MS, '"issue_type": "wrong_item"'),
                                      (MS, '"resolution": "replacement"')]),
    ]),
    ("18 missing item -> send item (7001012)", [
        ("an item is missing from my order", [(RT, "workflow"), (R, "order id")]),
        ("7001012", [(TR, "n_hub")]),
        ("one item is missing from the box", [(TR, "n_ask_missing"),
                                              (R, "missing")]),
        ("the aloe vera gel is missing", [(TR, "n_intent_missing_choice"),
                                          (R, "send")]),
        ("please send the missing item", [(TR, "n_msg_replace_ok"),
                                          (R, "whatsapp"),
                                          (MS, '"issue_type": "missing_item"'),
                                          (MS, '"resolution": "replacement"')]),
    ]),
    ("19 defective/expired -> replacement (7001007)", [
        ("I received an expired product", [(RT, "workflow"), (R, "order id")]),
        ("7001007", [(TR, "n_hub")]),
        ("the product is past its expiry date", [(TR, "n_ask_defect"),
                                                 (R, "expiry")]),
        ("the moisturizer expired last month", [(TR, "n_intent_defect_choice"),
                                                (R, "replacement")]),
        ("replacement", [(TR, "n_msg_replace_ok"), (R, "whatsapp"),
                         (MS, '"issue_type": "defective_expired"'),
                         (MS, '"resolution": "replacement"')]),
    ]),
    ("20 quality window closed -> agent (7001005)", [
        ("I received a damaged product", [(R, "order id")]),
        ("7001005", [(TR, "n_hub")]),
        ("it arrived damaged", [(TR, "n_ask_dmg")]),
        ("the colour tube was crushed", [(TR, "n_intent_dmg_choice")]),
        ("replacement please", [(TR, "n_msg_res_fail"),
                                (R, "couldn't raise"), (R, "?")]),
        ("yes please connect me", [(ST, "handoff"), (R, "stay on the line")]),
    ]),
    ("21 vague reason defaults to return (7001001)", [
        ("I want to return my product", [(R, "order id")]),
        ("7001001", [(TR, "n_hub")]),
        ("I want to return it", [(TR, "n_intent_reason"), (R, "?")]),
        ("nothing wrong, I just don't want it", [(TR, "n_intent_ret_confirm"),
                                                 (R, "eligible for return")]),
        ("haan, kar do", [(TR, "n_msg_return_ok"), (R, "whatsapp")]),
    ]),
    ("22 return declined at confirm (7001001)", [
        ("return my order", [(R, "order id")]),
        ("7001001", [(TR, "n_hub")]),
        ("I don't need it anymore", [(TR, "n_intent_ret_confirm")]),
        ("no, not now", [(TR, "n_msg_ret_declined"), (R, "won't raise")]),
        ("nothing else, thanks", [(DN, "true")]),
    ]),

    # ── Robustness ───────────────────────────────────────────────────────────
    ("23 wrong then correct order id (retry lookup)", [
        ("where is my order?", [(R, "order id")]),
        ("1234567", [(TR, "n_msg_notfound"), (R, "couldn't find"),
                     (R, "double-check")]),
        ("sorry, it is 7001003", [(TR, "n_api_lookup2"), (TR, "n_hub"),
                                  (SL, '"order_id": "7001003"')]),
    ]),
    ("24 unknown order id twice -> escalation (handoff)", [
        ("order status please", [(R, "order id")]),
        ("1234567", [(TR, "n_msg_notfound")]),
        ("9999999", [(TR, "n_msg_cant_locate"), (R, "support executive"),
                     (R, "?")]),
        ("yes please", [(ST, "handoff"), (R, "stay on the line"),
                        (MS, '"escalations"')]),
    ]),
    ("25 request changes mid-call (status -> return)", [
        ("where is my order?", [(R, "order id")]),
        ("7001001", [(TR, "n_hub")]),
        ("when was it delivered?", [(R, month_in(-2))]),
        ("actually, I want to return it", [(TR, "n_intent_reason"), (R, "?")]),
        ("I just don't need it anymore", [(TR, "n_intent_ret_confirm")]),
        ("yes", [(TR, "n_msg_return_ok"), (R, "whatsapp")]),
    ]),
    ("26 off-script question mid-flow, then continues", [
        ("I received a damaged product", [(R, "order id")]),
        ("7001011", [(TR, "n_hub")]),
        ("the face wash is damaged", [(TR, "n_ask_dmg")]),
        ("the pump is broken", [(TR, "n_intent_dmg_choice")]),
        ("which one is faster?", [(RT, "workflow")]),
        ("okay, replacement then", [(TR, "n_msg_replace_ok"), (R, "whatsapp")]),
    ]),
    ("27 repeated question re-answered", [
        ("what is my order amount?", [(R, "order id")]),
        ("7001001", [(TR, "n_hub")]),
        ("what is my order amount?", [(R, "rupees")]),
        ("sorry, what was the amount again?", [(R, "rupees")]),
    ]),
    ("34 spoken digit dictation", [
        ("where is my order?", [(R, "order id")]),
        ("seven zero zero one zero zero two", [(TR, "n_hub"),
                                               (SL, '"order_id": "7001002"')]),
    ]),
    ("35 id and question in one utterance", [
        ("where is my order?", [(R, "order id")]),
        ("7001002, when will it arrive?", [(TR, "n_hub")]),
        ("when will it arrive?", [(TR, "n_msg_order_answer"),
                                  (R, month_in(2))]),
    ]),
    ("36 hindi entry stays in flow", [
        ("मेरा ऑर्डर कहाँ है", [(RT, "workflow")]),
        ("7001003", [(TR, "n_hub")]),
    ]),

    # ── Guard rails / out of scope ───────────────────────────────────────────
    ("28 cancellation out of scope -> handoff", [
        ("I want to cancel my order", [(RT, "handoff")]),
    ]),
    ("29 complaint escalation -> handoff", [
        ("my issue is not resolved, I want to complain", [(RT, "handoff")]),
    ]),
    ("30 explicit agent request -> handoff", [
        ("please connect me to an agent", [(RT, "handoff")]),
    ]),
    ("31 stray refund word stays in the order flow", [
        ("I don't need a refund, just tell me where my order is",
         [(RT, "workflow"), (R, "order id")]),
    ]),
    ("32 unsupported request -> polite decline", [
        ("can you recommend a good sunscreen for oily skin?",
         [(R, "order")]),
    ]),
    ("33 return policy from knowledge base", [
        ("what is your return policy?", [(R, "7 days")]),
    ]),
]

reset_mock_state()
for name, turns_list in SCENARIOS:
    if only and only not in name:
        continue
    try:
        run(name, turns_list)
    except Exception as exc:  # noqa: BLE001
        FAIL += 1
        FAILURES.append(name)
        print(f"ERROR {name}: {exc}")
reset_mock_state()  # clean slate for manual testing

print(f"\n{PASS} passed, {FAIL} failed")
if FAILURES:
    print("failures:", FAILURES)
    sys.exit(1)
