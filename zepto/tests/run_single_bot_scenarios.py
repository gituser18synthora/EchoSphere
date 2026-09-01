"""End-to-end scenario tests for the FOUR dedicated single-concern Zepto
bots via /testing/simulate (the combined bot has its own suite,
run_chat_scenarios.py).

Same mechanics as the combined suite: sample ticket payloads are read back
from each "Zepto Register … Concern" connection's responseSchema.example and
replayed through ``mockToolResults`` — the success path runs with zero
external services; turns without mocks exercise the (equally valid) live
failure edge of the reserved .example ticketing host.

Every bot is exercised in BOTH Hinglish and English, covering: flow start,
every scripted question in order, the RTO conditional branch (yes AND no),
data capture, ticket registration (mocked success + live fallback), the
anything-else hub, and the scripted closing.

Run:  env/bin/python zepto/tests/run_single_bot_scenarios.py [bot-filter] [-v]
      bot-filter: MDND | UNIFORM | ONBOARDING | RTO
Requires: backend API on 9001, stages 06 + 07(knowledge) applied.
"""

import json
import pathlib
import sys
import uuid

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE = json.load(open(pathlib.Path(__file__).resolve().parent.parent
                       / "setup" / "zepto_config_state.json"))
TENANT = "tn_04250683f1b3"

c = httpx.Client(base_url=BASE, timeout=120)
r = c.post("/auth/login", json={"email": "zepto.config@zepto.com",
                                "password": "Demo@2026!"})
r.raise_for_status()
c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"

conns = c.get("/api-connections", params={"tenantId": TENANT}).json()["data"]


def mocks_for(connection_name: str) -> dict:
    conn = next(a for a in conns if a["name"] == connection_name)
    example = (conn.get("responseSchema") or {}).get("example")
    assert isinstance(example, dict), f"sample missing on {connection_name}"
    return {connection_name: example}


PASS, FAIL = 0, 0
FAILURES = []


def turn(bot_id, session, message, history, mocks=None):
    body = {"message": message, "sessionId": session, "messages": history}
    if mocks:
        body["mockToolResults"] = mocks
    r = c.post(f"/bots/{bot_id}/testing/simulate", json=body)
    r.raise_for_status()
    return r.json()["data"]


def run(bot_id, name, turns, verbose=False):
    global PASS, FAIL
    session = f"zs_{uuid.uuid4().hex[:10]}"
    history = []
    ok_all = True
    log = []
    for item in turns:
        message, mocks, expect = (item if len(item) == 3 else
                                  (item[0], None, item[1]))
        d = turn(bot_id, session, message, history, mocks)
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": d.get("response") or ""})
        wf = d.get("workflow") or {}
        log.append(f"    > {message}\n      route={d.get('route')} "
                   f"status={wf.get('status')} done={wf.get('done')}\n"
                   f"      reply: {(d.get('response') or '')[:220]}")
        got = {"reply": (d.get("response") or "").lower(),
               "route": str(d.get("route") or ""),
               "status": str(wf.get("status")),
               "trace": ",".join(wf.get("nodeTrace") or []),
               "slots": json.dumps(wf.get("slots") or {},
                                   ensure_ascii=False).lower(),
               "done": str(wf.get("done")).lower()}
        for kind, want in expect:
            if kind == "reply_not":
                if want.lower() in got["reply"]:
                    ok_all = False
                    log.append(f"      EXPECT reply NOT ~ '{want}' — FOUND")
            elif kind == "trace_not":
                if want.lower() in got["trace"].lower():
                    ok_all = False
                    log.append(f"      EXPECT trace NOT ~ '{want}' — FOUND")
            elif want.lower() not in got[kind].lower():
                ok_all = False
                log.append(f"      EXPECT {kind} ~ '{want}' — NOT FOUND")
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


R, RN, RT, ST, TR, TN, SL, DN = ("reply", "reply_not", "route", "status",
                                 "trace", "trace_not", "slots", "done")

MDND_MOCKS = mocks_for("Zepto Register MDND Concern")
UNIF_MOCKS = mocks_for("Zepto Register Uniform Deduction Concern")
ONBF_MOCKS = mocks_for("Zepto Register Onboarding Fee Concern")
RTO_MOCKS = mocks_for("Zepto Register RTO Concern")

SUITES = {
    "MDND": (STATE["BOT_MDND"], [
        # Mirrors the reference recording (tenant/zepto/zepto-call.mp4):
        # readout -> narrative -> only-missing enquiries -> verification ->
        # other-deduction check -> register -> refund boundary -> closing.
        ("MDND 01 reference-call replay (Hinglish), mocked ticket",
         [("haan bol raha hoon",
           # The grounded readout must name BOTH ticket deductions from the
           # call context (digits may be spoken as words, so no digit
           # literal is asserted).
           [(RT, "workflow"), (TR, "n_ask_issue_desc"), (R, "mdnd"),
            (R, "onboarding")]),
          ("maine deliver kiya tha product, maine call kiya tha, customer "
           "ne bola ghar ke aage rakh do, maine wahan rakh diya, uske baad "
           "deduction hua jo nahi hona chahiye tha",
           [(SL, '"m_called_customer": "yes (called the customer)"'),
            (TR, "n_msg_empathy"), (TR, "n_ask_handover"),
            (R, "परेशानी"), (R, "सौंपा"), (RN, "delivery से पहले")]),
          ("ye order maine customer ke guard ko handover kiya tha",
           [(SL, '"m_handover_recipient": "guard / security"'),
            (TR, "n_hub_verify"), (R, "सही है")]),
          ("ji sahi hai", [(TR, "n_ask_other")]),
          ("nahi, wo onboarding fee to sahi hi deduct hui thi, uske baare "
           "mein kuch nahi", MDND_MOCKS,
           [(TR, "n_api"), (TR, "n_confirmed"), (TR, "n_hub_more"),
            (SL, '"ticket_id": "zpt-mdnd-73412"'), (R, "payout")]),
          ("nahi bas, ye refund kab tak aa jayega?", [(R, "refund")]),
          ("theek hai thank you",
           [(TR, "n_msg_close"), (ST, "done"), (DN, "true"), (R, "शुक्रिया")]),
          ]),
        # One utterance answers BOTH enquiries -> neither question is asked.
        ("MDND 02 multi-answer: one utterance answers both enquiries",
         [("mdnd wala issue hai", [(TR, "n_ask_issue_desc")]),
          ("maine call kiya tha aur order guard ko de diya tha, phir bhi "
           "deduction hua",
           [(SL, '"m_called_customer": "yes (called the customer)"'),
            (SL, '"m_handover_recipient": "guard / security"'),
            (TR, "n_hub_verify"), (R, "सही है"),
            (RN, "सौंपा"), (RN, "delivery से पहले")]),
          ("sahi hai", [(TR, "n_ask_other")]),
          ("nahi kuch nahi",
           [(TR, "n_api"), (TR, "n_pending"), (TR, "n_hub_more")]),
          ("nahi bas", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
          ]),
        # Partial answer -> ONLY the missing question is asked.
        ("MDND 03 partial answer: only the missing question is asked",
         [("mdnd ka issue hai", [(TR, "n_ask_issue_desc")]),
          ("order guard ko de diya tha maine, phir bhi deduction aa gaya",
           [(SL, '"m_handover_recipient": "guard / security"'),
            (TR, "n_ask_called"), (R, "call"), (RN, "सौंपा")]),
          ("haan call kiya tha",
           [(SL, '"m_called_customer": "yes (called the customer)"'),
            (TR, "n_hub_verify"), (R, "सही है")]),
          ]),
        # The partner rejects the summary -> correction captured -> re-verify.
        ("MDND 04 correction at the verification step",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("bas deduction hua hai galat", [(TR, "n_ask_called")]),
          ("haan kiya tha", [(TR, "n_ask_handover")]),
          ("customer ko de diya tha",
           [(SL, '"m_handover_recipient": "customer (direct)"'),
            (TR, "n_hub_verify")]),
          ("nahi galat hai",
           [(TR, "n_ask_correction"), (R, "ठीक करके")]),
          ("actually order maine guard ko diya tha customer ko nahi",
           [(SL, "guard ko diya"), (TR, "n_hub_verify"), (R, "सही है")]),
          ("haan ab sahi hai", [(TR, "n_ask_other")]),
          ]),
        ("MDND 05 English caller end to end, live API fallback",
         [("I have an MDND issue",
           [(RT, "workflow"), (TR, "n_ask_issue_desc")]),
          ("I delivered the order but still got a deduction of 400 rupees",
           [(SL, '"m_deduction_amount": "400"'), (TR, "n_ask_called")]),
          ("yes I called the customer",
           [(SL, '"m_called_customer": "yes (called the customer)"'),
            (TR, "n_ask_handover")]),
          ("I handed it to the security guard",
           [(SL, '"m_handover_recipient": "guard / security"'),
            (TR, "n_hub_verify")]),
          ("yes all correct", [(TR, "n_ask_other")]),
          ("no nothing else",
           [(TR, "n_api"), (TR, "n_pending"), (TR, "n_hub_more")]),
          ("no thanks", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
          ]),
        ("MDND 06 explicit human request -> support handover",
         [("mujhe kisi support executive se baat karni hai",
           [(RT, "handoff")]),
          ]),
        ("MDND 07 policy question routes to the FAQ KB",
         [("MDND kya hota hai?", [(RT, "knowledge")]),
          ]),
    ]),
    "UNIFORM": (STATE["BOT_UNIFORM"], [
        ("UNIFORM 01 Hinglish caller, kit received, mocked ticket",
         [("haan boliye",
           [(RT, "workflow"), (TR, "n_ask_u_deduction_amount"),
            (R, "deduction amount")]),
          ("250 rupees", [(TR, "n_ask_u_deduction_count"), (R, "कितनी बार")]),
          ("do baar kata hai",
           [(TR, "n_ask_u_items_received"), (R, "raincoat")]),
          ("haan sab mila tha",
           [(TR, "n_ask_u_deduction_date"), (R, "week"),
            (SL, '"u_items_received"')]),
          ("pichhle hafte", UNIF_MOCKS,
           [(TR, "n_api"), (TR, "n_confirmed"),
            (SL, '"ticket_id": "zpt-unif-51208"')]),
          ("bas itna hi", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
          ]),
        ("UNIFORM 02 English caller, kit NOT received, live API fallback",
         [("money was deducted from my payout for the raincoat and bag",
           [(RT, "workflow"), (TR, "n_ask_u_deduction_amount")]),
          ("250 rupees", [(TR, "n_ask_u_deduction_count")]),
          ("two times", [(TR, "n_ask_u_items_received")]),
          ("no, I never received the kit",
           [(TR, "n_ask_u_deduction_date"), (SL, "never received")]),
          ("last week", [(TR, "n_api"), (TR, "n_pending"),
                         (R, "rest assured")]),
          ("no thank you", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
          ]),
        ("UNIFORM 03 explicit human request -> support handover",
         [("i want to talk to a human", [(RT, "handoff")]),
          ]),
        ("UNIFORM 04 policy question routes to the FAQ KB",
         [("yeh deduction kya hota hai?", [(RT, "knowledge")]),
          ]),
    ]),
    "ONBOARDING": (STATE["BOT_ONBOARDING"], [
        ("ONBOARDING 01 Hinglish caller, paid at joining, mocked ticket",
         [("onboarding fee ka deduction hua hai",
           [(RT, "workflow"), (TR, "n_ask_o_date_of_joining"),
            (R, "date of joining")]),
          ("15 June ko join kiya tha",
           [(TR, "n_ask_o_deduction_amount"), (R, "deduction amount")]),
          ("1000 rupees", [(TR, "n_ask_o_deduction_date"), (R, "week")]),
          ("is month ki 5 tarikh ko",
           [(TR, "n_ask_o_paid_on_joining"), (R, "pay")]),
          ("haan 500 rupees diye the", ONBF_MOCKS,
           [(TR, "n_api"), (TR, "n_confirmed"),
            (SL, '"ticket_id": "zpt-onbf-66931"')]),
          ("bas", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
          ]),
        ("ONBOARDING 02 English caller, paid nothing, live API fallback",
         [("an onboarding fee was deducted from my payout",
           [(RT, "workflow"), (TR, "n_ask_o_date_of_joining")]),
          ("I joined on 15 June", [(TR, "n_ask_o_deduction_amount")]),
          ("1000 rupees", [(TR, "n_ask_o_deduction_date")]),
          ("on the 5th of this month", [(TR, "n_ask_o_paid_on_joining")]),
          ("no, I paid nothing when I joined",
           [(TR, "n_api"), (TR, "n_pending"), (R, "rest assured"),
            (SL, "paid nothing")]),
          ("no, that's all", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
          ]),
        ("ONBOARDING 03 explicit human request -> support handover",
         [("manager se baat karao", [(RT, "handoff")]),
          ]),
        ("ONBOARDING 04 policy question routes to the FAQ KB",
         [("onboarding fee kya hoti hai?", [(RT, "knowledge")]),
          ]),
    ]),
    "RTO": (STATE["BOT_RTO"], [
        ("RTO 01 Hinglish, handed to store -> date follow-up, mocked ticket",
         [("rto ka issue hai mera",
           [(RT, "workflow"), (TR, "n_ask_r_deduction_amount"),
            (R, "deduction amount")]),
          ("300 rupees", [(TR, "n_ask_r_order_last4"), (R, "last 4")]),
          ("5566", [(TR, "n_ask_r_deduction_date"), (R, "week")]),
          ("kal hi hua deduction",
           [(TR, "n_ask_r_store_handover"), (R, "store team")]),
          ("haan de diya tha store pe",
           [(TR, "n_cond_store_handover"),
            (TR, "n_ask_r_store_handover_date"),
            (SL, '"r_store_handover": "yes"'), (R, "कब")]),
          ("usi din shaam ko", RTO_MOCKS,
           [(TR, "n_api"), (TR, "n_confirmed"),
            (SL, '"ticket_id": "zpt-rto-48057"')]),
          ("nahi bas", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
          ]),
        ("RTO 02 English, NOT handed to store -> follow-up skipped",
         [("I have an RTO issue",
           [(RT, "workflow"), (TR, "n_ask_r_deduction_amount")]),
          ("150 rupees", [(TR, "n_ask_r_order_last4")]),
          ("9012", [(TR, "n_ask_r_deduction_date")]),
          ("day before yesterday", [(TR, "n_ask_r_store_handover")]),
          ("no, I still have the product",
           [(TR, "n_cond_store_handover"), (TN, "n_ask_r_store_handover_date"),
            (TR, "n_api"), (TR, "n_pending"),
            (SL, '"r_store_handover": "no"'), (R, "rest assured")]),
          ("no thank you", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
          ]),
        ("RTO 03 explicit human request -> support handover",
         [("kisi insaan se baat karao", [(RT, "handoff")]),
          ]),
        ("RTO 04 policy question routes to the FAQ KB",
         [("RTO ka matlab kya hai?", [(RT, "knowledge")]),
          ]),
    ]),
}

only = next((a for a in sys.argv[1:] if a != "-v"), None)

for key, (bot_id, scenarios) in SUITES.items():
    if only and only.upper() not in key:
        continue
    for name, turns in scenarios:
        run(bot_id, name, turns, verbose="-v" in sys.argv)

print(f"\n{PASS} passed, {FAIL} failed")
if FAILURES:
    print("failures:", FAILURES)
    sys.exit(1)
