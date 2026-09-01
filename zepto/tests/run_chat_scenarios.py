"""End-to-end scenario tests for the Zepto Support bot via /testing/simulate.

The simulate endpoint is used (not /testing/chat) because it accepts
``mockToolResults`` — the platform's own mechanism for exercising tool calls
against sample data with zero external services. The four concern-ticket
sample payloads are NOT duplicated here: they are read back from each
"Zepto Register … Concern" connection's responseSchema.example, so the tool
configuration stays the single source of truth.

Each scenario = list of (utterance, mocks?, expectations) turns.
Expectation kinds:
  reply      - substring of the (lowercased) bot reply
  reply_not  - substring that must NOT appear in the reply (leak checks)
  route      - substring of the route
  status     - substring of the workflow status (e.g. done)
  trace      - substring of the comma-joined node trace
  trace_not  - node id that must NOT appear in the trace (leak checks)
  slots      - substring of the JSON-dumped workflow slots (lowercased)
  done       - "true"/"false" (workflow done flag)

Run:  env/bin/python zepto/tests/run_chat_scenarios.py [name-filter] [-v]
Requires: backend API on 9001, stages 00-05 applied.
"""

import json
import pathlib
import sys
import uuid

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE_FILE = (pathlib.Path(__file__).resolve().parent.parent
              / "setup" / "zepto_config_state.json")
BOT = json.load(open(STATE_FILE))["BOT"]
TENANT = "tn_04250683f1b3"

c = httpx.Client(base_url=BASE, timeout=120)
r = c.post("/auth/login", json={"email": "zepto.config@zepto.com",
                                "password": "Demo@2026!"})
r.raise_for_status()
c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"

# The sample ticket responses live inside the tool configurations.
conns = c.get("/api-connections", params={"tenantId": TENANT}).json()["data"]
MOCKS = {}
for name in ("Zepto Register MDND Concern",
             "Zepto Register Uniform Deduction Concern",
             "Zepto Register Onboarding Fee Concern",
             "Zepto Register RTO Concern"):
    conn = next(a for a in conns if a["name"] == name)
    example = (conn.get("responseSchema") or {}).get("example")
    assert isinstance(example, dict), f"sample response missing on {name}"
    MOCKS[name] = example

PASS, FAIL = 0, 0
FAILURES = []


def turn(session, message, history, mocks=None):
    body = {"message": message, "sessionId": session, "messages": history}
    if mocks:
        body["mockToolResults"] = mocks
    r = c.post(f"/bots/{BOT}/testing/simulate", json=body)
    r.raise_for_status()
    return r.json()["data"]


def run(name, turns, verbose=False):
    global PASS, FAIL
    session = f"zp_{uuid.uuid4().hex[:10]}"
    history = []
    ok_all = True
    log = []
    for item in turns:
        message, mocks, expect = (item if len(item) == 3 else
                                  (item[0], None, item[1]))
        d = turn(session, message, history, mocks)
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

only = next((a for a in sys.argv[1:] if a != "-v"), None)

SCENARIOS = [
    # The opener names the concern -> the issue ask consumes the entry text
    # (entry_slot_filled) and the MDND branch starts with zero re-asking.
    ("01 direct MDND opener, all enquiries, mocked ticket",
     [("mujhe MDND ka issue report karna hai",
       [(RT, "workflow"), (TR, "n_ask_issue"), (TR, "n_m_greet"),
        (SL, '"issue_type": "mdnd"'),
        (R, "mark delivered but not delivered"), (R, "deduction amount")]),
      ("450 rupees ka deduction hua hai",
       [(TR, "n_m_ask_order_last4"), (R, "last 4")]),
      ("7842", [(TR, "n_m_ask_deduction_date"),
                (SL, '"m_order_last4": "7842"'), (R, "date or week")]),
      ("pichhle hafte tuesday ko", [(TR, "n_m_ask_called_customer"),
                                    (R, "call the customer")]),
      ("haan maine call kiya tha", [(TR, "n_m_ask_reached_location"),
                                    (R, "location")]),
      ("haan location pe pahuncha tha", [(TR, "n_m_ask_handover_recipient"),
                                         (R, "hand over")]),
      ("security guard ko de diya tha", [(TR, "n_m_ask_cx_support_call"),
                                         (R, "cx support")]),
      ("nahi cx support ka koi call nahi aaya", MOCKS,
       [(TR, "n_m_api"), (TR, "n_m_confirmed"), (TR, "n_hub_more"),
        (SL, '"ticket_id": "zpt-mdnd-73412"'), (R, "anything else")]),
      ("nahi bas itna hi tha",
       [(TR, "n_msg_close"), (ST, "done"), (DN, "true"),
        (R, "thank you for contacting zepto support")]),
      ]),
    # A vague opener -> the selector asks the scripted identification
    # question; no mocks on the api turn, so the reserved .example host takes
    # the failure edge and the approved script's own closing plays.
    ("02 vague opener -> selector -> uniform branch, API fallback",
     [("mere payout se paisa kata hai",
       [(RT, "workflow"), (TR, "n_ask_issue"), (R, "which concern"),
        (R, "mdnd"), (R, "rto")]),
      ("raincoat t-shirt bag wala deduction hai",
       [(TR, "n_u_greet"), (SL, '"issue_type": "uniform_deduction"'),
        (R, "bag, t-shirt, and raincoat"), (R, "deduction amount")]),
      ("250 rupees", [(TR, "n_u_ask_deduction_count"), (R, "how many times")]),
      ("do baar kata hai", [(TR, "n_u_ask_items_received"),
                            (R, "receive the bag")]),
      ("haan sab mila tha", [(TR, "n_u_ask_deduction_date"),
                             (R, "date or week")]),
      ("pichhle hafte",
       [(TR, "n_u_api"), (TR, "n_u_pending"), (TR, "n_hub_more"),
        (R, "rest assured"), (R, "connect with you shortly")]),
      ("nahi thank you",
       [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
      ]),
    ("03 onboarding fee branch, mocked ticket",
     [("onboarding fee ka deduction hua hai mere saath",
       [(RT, "workflow"), (TR, "n_o_greet"),
        (SL, '"issue_type": "onboarding_fee"'),
        (R, "onboarding fee"), (R, "date of joining")]),
      ("15 June ko join kiya tha", [(TR, "n_o_ask_deduction_amount"),
                                    (R, "deduction amount")]),
      ("1000 rupees", [(TR, "n_o_ask_deduction_date"), (R, "date or week")]),
      ("is month ki 5 tarikh ko", [(TR, "n_o_ask_paid_on_joining"),
                                   (R, "pay any amount")]),
      ("nahi maine joining pe kuch nahi diya tha", MOCKS,
       [(TR, "n_o_api"), (TR, "n_o_confirmed"),
        (SL, '"ticket_id": "zpt-onbf-66931"')]),
      ("bas itna hi", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
      ]),
    # The scripts' one real conditional: the handover-date follow-up is asked
    # only when the product WAS handed to the store team.
    ("04 RTO handed to store -> handover-date follow-up",
     [("RTO ka issue hai mera",
       [(RT, "workflow"), (TR, "n_r_greet"), (SL, '"issue_type": "rto"'),
        (R, "rto"), (R, "deduction amount")]),
      ("300 rupees", [(TR, "n_r_ask_order_last4"), (R, "last 4")]),
      ("5566", [(TR, "n_r_ask_deduction_date"), (R, "date or week")]),
      ("kal hi hua deduction", [(TR, "n_r_ask_store_handover"),
                                (R, "store team")]),
      ("haan de diya tha store pe",
       [(TR, "n_r_cond_handover"), (TR, "n_r_ask_store_handover_date"),
        (SL, '"r_store_handover": "yes"'), (R, "when did you hand over")]),
      ("usi din shaam ko",
       [(TR, "n_r_api"), (TR, "n_r_pending"), (R, "rest assured")]),
      ("nahi bas", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
      ]),
    ("05 RTO not handed to store -> follow-up skipped",
     [("rto deduction hua hai",
       [(RT, "workflow"), (TR, "n_r_greet"), (R, "deduction amount")]),
      ("150 rupees", [(TR, "n_r_ask_order_last4")]),
      ("9012", [(TR, "n_r_ask_deduction_date")]),
      ("parson", [(TR, "n_r_ask_store_handover")]),
      ("nahi abhi product mere paas hai",
       [(TR, "n_r_cond_handover"), (TN, "n_r_ask_store_handover_date"),
        (TR, "n_r_api"), (TR, "n_r_pending"),
        (SL, '"r_store_handover": "no"'), (R, "rest assured")]),
      ("nahi bas", [(TR, "n_msg_close"), (ST, "done")]),
      ]),
    # Concern isolation: while the MDND branch runs, no other concern's
    # scripted question ever appears, and no other branch node is traced.
    ("06 MDND branch never asks another concern's questions",
     [("mdnd ka deduction hua hai",
       [(TR, "n_m_greet"), (RN, "raincoat"), (RN, "onboarding"),
        (RN, "store team"), (TN, "n_u_"), (TN, "n_o_"), (TN, "n_r_")]),
      ("500 rupees", [(RN, "how many times"), (RN, "date of joining"),
                      (TN, "n_u_"), (TN, "n_o_"), (TN, "n_r_")]),
      ("1122", [(RN, "joining"), (TN, "n_u_"), (TN, "n_o_"), (TN, "n_r_")]),
      ("kal", [(R, "call the customer"), (RN, "store team"),
               (TN, "n_u_"), (TN, "n_o_"), (TN, "n_r_")]),
      ("haan", [(R, "location"), (TN, "n_u_"), (TN, "n_o_"), (TN, "n_r_")]),
      ("haan pahuncha tha", [(R, "hand over"), (RN, "store team"),
                             (TN, "n_u_"), (TN, "n_o_"), (TN, "n_r_")]),
      ("customer ko hi diya tha", [(R, "cx support"),
                                   (TN, "n_u_"), (TN, "n_o_"), (TN, "n_r_")]),
      ("nahi", [(TR, "n_m_api"), (TR, "n_m_pending"),
                (TN, "n_u_"), (TN, "n_o_"), (TN, "n_r_")]),
      ("nahi bas", [(TR, "n_msg_close"), (ST, "done")]),
      ]),
    # A second concern in the same call jumps straight to that branch's
    # greeting from the anything-else hub — never back through the issue ask.
    ("07 second concern in the same call jumps to its branch",
     [("raincoat aur bag ka paisa kata hai",
       [(TR, "n_u_greet"), (R, "deduction amount")]),
      ("100 rupees", [(TR, "n_u_ask_deduction_count")]),
      ("ek hi baar", [(TR, "n_u_ask_items_received")]),
      ("haan mila tha", [(TR, "n_u_ask_deduction_date")]),
      ("somvaar ko", [(TR, "n_u_pending"), (R, "anything else")]),
      ("haan ek RTO ka issue bhi hai",
       [(TR, "n_hub_more"), (TR, "n_r_greet"), (TN, "n_ask_issue"),
        (R, "rto"), (R, "deduction amount")]),
      ]),
    # Off-script: not an answer to the pending question — the LLM replies in
    # context, the workflow stays paused, and the next answer resumes it.
    ("08 off-script question mid-branch, flow resumes",
     [("mdnd issue hai mera", [(TR, "n_m_greet"), (R, "deduction amount")]),
      ("yeh deduction hota kyun hai?", []),
      ("450 rupees", [(TR, "n_m_ask_order_last4"), (R, "last 4")]),
      ]),
    ("09 policy/definition question routes to the KB",
     [("MDND kya hota hai?", [(RT, "knowledge")]),
      ]),
    ("10 explicit human request -> support handover",
     [("mujhe kisi support executive se baat karni hai",
       [(RT, "handoff")]),
      ]),
    # Spoken digits: the shared spoken-number pipeline turns digit words into
    # the identifier.
    ("11 spoken digits fill the order-ID last-4",
     [("mdnd ka issue hai", [(TR, "n_m_greet")]),
      ("teen sau rupees", [(TR, "n_m_ask_order_last4")]),
      ("seven eight four two",
       [(SL, '"m_order_last4": "7842"'), (TR, "n_m_ask_deduction_date")]),
      ]),
    # Retry exhaustion at the selector falls back to the human handover edge
    # instead of looping.
    ("12 selector retry exhaustion -> handover fallback",
     [("deduction hua hai", [(TR, "n_ask_issue"), (R, "which concern")]),
      ("gaadi ki service karani hai", []),
      ("mausam bhi kharab hai aaj", []),
      ("kuch samajh nahi aa raha mujhe",
       [(TR, "n_handover"), (ST, "handoff")]),
      ]),
    ("13 Hindi opener routes the uniform branch directly",
     [("रेनकोट का पैसा कट गया है मेरा",
       [(RT, "workflow"), (TR, "n_u_greet"),
        (SL, '"issue_type": "uniform_deduction"'), (R, "deduction amount")]),
      ("दो सौ रुपये", [(TR, "n_u_ask_deduction_count")]),
      ]),
]

for name, turns in SCENARIOS:
    if only and only not in name:
        continue
    run(name, turns, verbose="-v" in sys.argv)

print(f"\n{PASS} passed, {FAIL} failed")
if FAILURES:
    print("failures:", FAILURES)
    sys.exit(1)
