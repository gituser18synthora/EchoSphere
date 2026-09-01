"""End-to-end scenario tests for the Frankfinn bot via POST /testing/simulate.

The simulate endpoint is used (not /testing/chat) because it accepts
``mockToolResults`` — the platform's own mechanism for exercising tool calls
against sample data with zero external services. The booking sample payload
is NOT duplicated here: it is read back from the "Frankfinn Book Seminar
Seat" connection's responseSchema.example, so the tool configuration stays
the single source of truth.

Each scenario = list of (utterance, mocks?, expectations) turns.
Expectation kinds:
  reply  - substring of the (lowercased) bot reply
  route  - substring of the route
  status - substring of the workflow status (e.g. done)
  trace  - substring of the comma-joined node trace
  slots  - substring of the JSON-dumped workflow slots (lowercased)
  done   - "true"/"false" (workflow done flag)

Run:  env/bin/python frankfinn/tests/run_chat_scenarios.py [name-filter]
Requires: backend API on 9001.
"""

import json
import pathlib
import sys
import uuid

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE_FILE = (pathlib.Path(__file__).resolve().parent.parent
              / "setup" / "frankfinn_config_state.json")
BOT = json.load(open(STATE_FILE))["BOT"]
TOOL = "Frankfinn Book Seminar Seat"

c = httpx.Client(base_url=BASE, timeout=120)
r = c.post("/auth/login", json={"email": "frankfinn.config@frankfinn.com",
                                "password": "Demo@2026!"})
r.raise_for_status()
c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"

# The sample booking response lives inside the tool configuration.
conns = c.get("/api-connections",
              params={"tenantId": "tn_6553beac240d"}).json()["data"]
book = next(a for a in conns if a["name"] == TOOL)
BOOKING_SAMPLE = (book.get("responseSchema") or {}).get("example")
assert isinstance(BOOKING_SAMPLE, dict), "sample response missing from tool config"
MOCKS = {TOOL: BOOKING_SAMPLE}

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
    session = f"ff_{uuid.uuid4().hex[:10]}"
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
        for kind, want in expect:
            got = {"reply": (d.get("response") or "").lower(),
                   "route": str(d.get("route") or ""),
                   "status": str(wf.get("status")),
                   "trace": ",".join(wf.get("nodeTrace") or []),
                   "slots": json.dumps(wf.get("slots") or {},
                                       ensure_ascii=False).lower(),
                   "done": str(wf.get("done")).lower()}[kind]
            if want.lower() not in got.lower():
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


R, RT, ST, TR, SL, DN = "reply", "route", "status", "trace", "slots", "done"

only = sys.argv[1] if len(sys.argv) > 1 else None

OPEN_YES = ("haan bol raha hoon",
            [(RT, "workflow"), (TR, "n_hub_opening"), (R, "seminar")])
ELIG = [
    ("haan bata do", [(TR, "n_ask_age"), (R, "age")]),
    ("22 saal", [(TR, "n_ask_area")]),
    ("Maninagar se", [(TR, "n_hub_qual"), (R, "qualification")]),
]

SCENARIOS = [
    ("01 graduate books seat (mocked booking success)",
     [OPEN_YES, *ELIG,
      ("graduation complete ho gayi",
       [(TR, "n_msg_grad_track"), (TR, "n_hub_book"), (R, "8 months")]),
      ("haan kar do book", [(TR, "n_hub_sure"), (R, "10:15")]),
      ("haan pakka aaunga", [(TR, "n_ask_parents"), (R, "parents")]),
      ("haan papa aayenge", MOCKS,
       [(TR, "n_api_book"), (TR, "n_msg_confirmed"), (TR, "n_hub_sms"),
        (SL, '"appointment_number": "frk-ahd-104217"'), (R, "sms")]),
      ("haan mil gaya", [(TR, "n_msg_id"), (R, "aadhaar")]),
      ("nahi bas, thank you", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
      ]),
    ("02 twelfth-pass gets 11-month track",
     [OPEN_YES, *ELIG,
      ("main 12th pass hoon",
       [(TR, "n_msg_ug_track"), (TR, "n_hub_book"), (R, "11 months")]),
      ]),
    ("03 third-year final-year probe -> 8-month track",
     [OPEN_YES, *ELIG,
      ("abhi third year chal raha hai",
       [(TR, "n_hub_finalyear"), (R, "final year")]),
      ("haan final year hai",
       [(TR, "n_msg_grad_track"), (R, "8 months")]),
      ]),
    ("04 third-year not final -> 11-month track",
     [OPEN_YES, *ELIG,
      ("teesra saal hai", [(TR, "n_hub_finalyear")]),
      ("nahi abhi aur saal baaki hai",
       [(TR, "n_msg_ug_track"), (R, "11 months")]),
      ]),
    ("05 below twelfth -> polite not-eligible close",
     [OPEN_YES, *ELIG,
      ("main abhi 12th mein hoon",
       [(TR, "n_msg_not_eligible"), (R, "12th"), (ST, "done"), (DN, "true")]),
      ]),
    ("06 fees question answered, then books",
     [OPEN_YES, *ELIG,
      ("graduation complete ho gayi", [(TR, "n_hub_book")]),
      ("fees kitni hai?",
       [(TR, "n_msg_fees"), (R, "free"), (TR, "n_hub_book")]),
      ("theek hai haan kar do", [(TR, "n_hub_sure"), (R, "10:15")]),
      ]),
    ("07 declined once, soft counter, then books",
     [OPEN_YES, *ELIG,
      ("main 12th pass hoon", [(TR, "n_hub_book")]),
      ("nahi mujhe nahi karna",
       [(TR, "n_msg_objection"), (TR, "n_hub_book2"), (R, "free")]),
      ("chalo theek hai kar do", [(TR, "n_hub_sure"), (R, "10:15")]),
      ]),
    ("08 declined twice -> polite close",
     [OPEN_YES, *ELIG,
      ("main 12th pass hoon", [(TR, "n_hub_book")]),
      ("nahi karna mujhe", [(TR, "n_hub_book2")]),
      ("nahi bhai rehne do",
       [(TR, "n_msg_polite_close"), (ST, "done"), (DN, "true")]),
      ]),
    ("09 busy -> callback captured and close",
     [("main abhi busy hoon baad mein call karna",
       [(RT, "workflow"), (TR, "n_ask_callback"), (R, "time")]),
      ("shaam ko 6 baje",
       [(TR, "n_msg_callback_close"), (ST, "done"), (DN, "true"),
        (SL, "callback_time")]),
      ]),
    ("10 wrong number -> apology close",
     [("aapne galat number lagaya hai, yahan is naam ka koi nahi",
       [(RT, "workflow"), (TR, "n_msg_wrong"), (ST, "done"), (DN, "true")]),
      ]),
    # The PLATFORM intercepts do-not-call before any routing (detect_do_not_call
    # -> call_control): the number is marked DNC and the call ends. The
    # workflow's own DNC edges remain as fallback for phrasings the detector
    # misses.
    ("11 do-not-call -> platform call_control close",
     [("mujhe dobara call mat karna, mera number hata do",
       [(RT, "call_control"), (R, "do-not-call")]),
      ]),
    ("12 booking API unavailable -> graceful SMS fallback",
     [OPEN_YES, *ELIG,
      ("graduation complete ho gayi", [(TR, "n_hub_book")]),
      ("haan kar do", [(TR, "n_hub_sure")]),
      ("pakka aaunga", [(TR, "n_ask_parents")]),
      # NO mocks on this turn: the placeholder CRM host cannot resolve, so
      # the api node deterministically takes the failure edge.
      ("haan mummy papa aayenge",
       [(TR, "n_api_book"), (TR, "n_msg_pending"), (TR, "n_msg_id"),
        (TR, "n_hub_anything"), (R, "sms"), (R, "aadhaar")]),
      ("nahi bas", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
      ]),
    ("13 SMS not received -> address spoken",
     [OPEN_YES, *ELIG,
      ("graduation complete ho gayi", [(TR, "n_hub_book")]),
      ("haan kar do", [(TR, "n_hub_sure")]),
      ("pakka", [(TR, "n_ask_parents")]),
      ("akela aaunga", MOCKS,
       [(TR, "n_msg_confirmed"), (TR, "n_hub_sms")]),
      ("nahi aaya SMS",
       [(TR, "n_msg_sms_later"), (R, "c g road"), (TR, "n_msg_id"),
        (R, "aadhaar")]),
      ]),
    ("14 KB question mid-booking answered, flow resumes",
     [OPEN_YES, *ELIG,
      ("main 12th pass hoon", [(TR, "n_hub_book")]),
      ("seminar mein kya hoga?",
       [(TR, "n_kb_answer"), (TR, "n_hub_book"), (R, "seminar")]),
      ("haan kar do", [(TR, "n_hub_sure"), (R, "10:15")]),
      ]),
    ("15 explicit human request -> senior counsellor handover",
     [OPEN_YES,
      ("mujhe kisi senior counsellor se baat karao",
       [(TR, "n_handover"), (ST, "handoff"), (DN, "true"), (R, "senior")]),
      ]),
    ("16 off-script question mid-flow, flow resumes",
     [OPEN_YES, *ELIG,
      ("graduation complete ho gayi", [(TR, "n_hub_book")]),
      # Off-script: not an answer to the booking question — the LLM replies,
      # the workflow stays paused at the booking hub.
      ("aap log Delhi mein bhi ho kya?", []),
      ("haan seat book kar do", [(TR, "n_hub_sure"), (R, "10:15")]),
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
