"""End-to-end scenario tests for the OYO bots via POST /bots/{id}/testing/chat.

Each scenario = list of (utterance, expectation) turns. Expectations check
substrings of the reply, the route, workflow status, or slots.
"""

import json
import sys

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
BOT1 = "bot_e8cf0b05bb79"
STATE_FILE = __file__.rsplit("/", 1)[0].replace("/tests", "/setup") + "/oyo_config_state.json"
state = json.load(open(STATE_FILE))
BOT2, BOT3 = state["BOT2"], state["BOT3"]

c = httpx.Client(base_url=BASE, timeout=60)
r = c.post("/auth/login", json={"email": "oyo.config@oyo.com", "password": "Demo@2026!"})
c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"

PASS, FAIL = 0, 0
FAILURES = []


def turn(bot, session, message, history):
    r = c.post(f"/bots/{bot}/testing/chat",
               json={"message": message, "sessionId": session, "messages": history})
    r.raise_for_status()
    return r.json()["data"]


def run(name, bot, turns, verbose=False):
    global PASS, FAIL
    session = f"sc_{abs(hash(name)) % 10**10}"
    history = []
    ok_all = True
    log = []
    for message, expect in turns:
        d = turn(bot, session, message, history)
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": d["reply"] or ""})
        wf = d.get("workflow") or {}
        line = (f"    > {message}\n      route={d['route']} "
                f"status={wf.get('status')} done={d['done']}\n"
                f"      reply: {(d['reply'] or '')[:220]}")
        log.append(line)
        for kind, want in expect:
            got = {"reply": (d["reply"] or "").lower(),
                   "route": d["route"],
                   "status": str(wf.get("status")),
                   "trace": ",".join(wf.get("nodeTrace") or []),
                   "slots": json.dumps(wf.get("slots") or {}).lower(),
                   "done": str(d["done"]).lower()}[kind]
            if want.lower() not in got:
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


R, RT, ST, TR, SL = "reply", "route", "status", "trace", "slots"

only = sys.argv[1] if len(sys.argv) > 1 else None

SCENARIOS = [
    # ── customer bot ─────────────────────────────────────────────────────────
    ("01 system confirmation only (601001)", BOT1, [
        ("hello", [(RT, "workflow"), (R, "booking id")]),
        ("my booking id is 601001", [(R, "guest name")]),
        ("Rahul Sharma", [(R, "confirmed in our system")]),
        ("no, that's all, thank you", [(R, "proceed with your check-in"), ("done", "true")]),
    ]),
    ("02 cancelled + dispute -> transfer (601002)", BOT1, [
        ("is my booking confirmed", [(RT, "workflow"), (R, "booking id")]),
        ("601002", [(R, "guest name")]),
        ("Priya Verma", [(R, "cancelled"), (R, "did you cancel")]),
        ("no, I never cancelled this booking", [(ST, "handoff"), (R, "transfer")]),
    ]),
    ("03 cancelled by customer (601013)", BOT1, [
        ("booking status please", [(R, "booking id")]),
        ("601013", [(R, "guest name")]),
        ("Nisha Reddy", [(R, "cancelled")]),
        ("yes, I cancelled it myself", [(R, "nothing pending"), ("done", "true")]),
    ]),
    ("04 PM confirms booking (601001)", BOT1, [
        ("I want to confirm my booking with the hotel", [(R, "booking id")]),
        ("601001", [(R, "guest name")]),
        ("Rahul Sharma", [(R, "confirmed in our system")]),
        ("please confirm with the property", [
            (R, "stay on the line"),
            (R, "successfully confirmed your booking with the property"),
            ("done", "true")]),
    ]),
    ("05 PM no answer -> stock confirms (601003)", BOT1, [
        ("check-in confirmation", [(R, "booking id")]),
        ("601003", [(R, "guest name")]),
        ("Arjun Mehta", [(R, "confirmed in our system")]),
        ("yes please check with the property", [
            (R, "unable to reach the property manager"),
            (R, "internal team has validated"),
            ("done", "true")]),
    ]),
    ("06 overbooked -> shift accepted (601004)", BOT1, [
        ("confirm my booking", [(R, "booking id")]),
        ("601004", [(R, "guest name")]),
        ("Sneha Iyer", [(R, "confirmed in our system")]),
        ("confirm with the property please", [
            (R, "overbooked"), (R, "alternate oyo property")]),
        ("yes please", [(R, "shall i proceed with shifting")]),
        ("yes, go ahead", [(R, "initiated the shift"), ("done", "true")]),
    ]),
    ("07 overbooked-but-available -> penalty accepted (601005)", BOT1, [
        ("confirm my booking with the hotel", [(R, "booking id")]),
        ("601005", [(R, "guest name")]),
        ("Vikram Singh", [(R, "confirmed in our system")]),
        ("please verify with the property", [
            (R, "successfully confirmed your booking with the property"),
            ("done", "true")]),
    ]),
    ("08 maintenance + alternate room (601006)", BOT1, [
        ("booking confirmation", [(R, "booking id")]),
        ("601006", [(R, "guest name")]),
        ("Ananya Das", [(R, "confirmed in our system")]),
        ("confirm with the property", [
            (R, "alternate room"), ("done", "true")]),
    ]),
    ("09 maintenance no room -> shift declined (601007)", BOT1, [
        ("check my booking", [(R, "booking id")]),
        ("601007", [(R, "guest name")]),
        ("Rohan Kapoor", [(R, "confirmed in our system")]),
        ("please confirm with the property", [
            (R, "maintenance"), (R, "alternate oyo property")]),
        ("no, don't shift", [(R, "contact oyo support"), ("done", "true")]),
    ]),
    ("10 price meets ARR -> honored (601008)", BOT1, [
        ("is my reservation confirmed", [(R, "booking id")]),
        ("601008", [(R, "guest name")]),
        ("Meera Nair", [(R, "confirmed in our system")]),
        ("confirm with the property", [
            (R, "successfully confirmed your booking with the property"),
            ("done", "true")]),
    ]),
    ("11 price below ARR -> compensation added (601009)", BOT1, [
        ("booking confirmation", [(R, "booking id")]),
        ("601009", [(R, "guest name")]),
        ("Aditya Rao", [(R, "confirmed in our system")]),
        ("confirm with the property", [
            (R, "successfully confirmed with the property"), ("done", "true")]),
    ]),
    ("12 price refused -> shift (601010)", BOT1, [
        ("confirm my booking", [(R, "booking id")]),
        ("601010", [(R, "guest name")]),
        ("Kavita Joshi", [(R, "confirmed in our system")]),
        ("please confirm with the property", [
            (R, "unable to accommodate"), (R, "alternate oyo property")]),
        ("yes", [(R, "shall i proceed with shifting")]),
        ("yes, proceed", [(R, "initiated the shift"), ("done", "true")]),
    ]),
    ("13 PM + stock unavailable -> shift (601011)", BOT1, [
        ("check-in confirmation", [(R, "booking id")]),
        ("601011", [(R, "guest name")]),
        ("Sanjay Gupta", [(R, "confirmed in our system")]),
        ("confirm with the property", [
            (R, "unable to reach the property manager"),
            (R, "could not get a confirmation"),
            (R, "alternate oyo property")]),
        ("yes please", [(R, "shall i proceed with shifting")]),
        ("okay", [(R, "initiated the shift"), ("done", "true")]),
    ]),
    ("14 voucher to email on file (601001)", BOT1, [
        ("I need my booking voucher", [(R, "booking id")]),
        ("601001", [(R, "guest name")]),
        ("Rahul Sharma", [(R, "confirmed in our system")]),
        ("send me the voucher", [(R, "email address from your booking on file")]),
        ("yes please", [(R, "emailed your booking voucher"), (R, "anything else")]),
        ("no thanks", [("done", "true")]),
    ]),
    ("15 voucher, no email on file (601012)", BOT1, [
        ("booking voucher please", [(R, "booking id")]),
        ("601012", [(R, "guest name")]),
        ("Farhan Ali", [(R, "confirmed in our system")]),
        ("email the voucher", [(R, "email address where i should send")]),
        ("farhan.ali@example.com", [(R, "emailed your booking voucher")]),
        ("nothing else", [("done", "true")]),
    ]),
    ("16 booking details exit -> LLM facts (601001)", BOT1, [
        ("share my booking details", [(R, "booking id")]),
        ("601001", [(R, "guest name")]),
        ("Rahul Sharma", [(R, "confirmed in our system")]),
        ("booking details please", [(R, "ask me anything"), ("done", "true")]),
        # Answered by the LLM from the bot's runtime-context fact set (Studio →
        # Runtime Context). Assert on the hotel, not the dates: the fact set is
        # tunable demo data and its dates may be edited independently.
        # Routed via the booking_fact_question intent (no route → LLM): the
        # bot now has an indexed FAQ KB, and without the intent the router's
        # question heuristics would send personal-fact turns to retrieval.
        ("when is my check-in and which hotel is it?", [(RT, "intent"), (R, "gurugram")]),
    ]),
    # Policy questions (no personal facts involved) SHOULD come from the
    # indexed guest FAQ — this is what the readiness item "Knowledge sources
    # indexed" buys the customer bot.
    ("31 guest FAQ from knowledge (overbooked policy)", BOT1, [
        ("what happens if the property is overbooked?",
         [(RT, "knowledge"), (R, "overbooked"), (R, "alternate")]),
    ]),
    ("17 verification failure (601001, wrong name)", BOT1, [
        ("confirm my booking", [(R, "booking id")]),
        ("601001", [(R, "guest name")]),
        ("Amit Kumar", [(ST, "handoff"), (R, "could not verify")]),
    ]),
    ("18 unknown booking id", BOT1, [
        ("confirm my booking", [(R, "booking id")]),
        ("999999", [(R, "guest name")]),
        ("Rahul Sharma", [(ST, "handoff"), (R, "could not verify")]),
    ]),
    ("19 out of scope -> handoff", BOT1, [
        ("I want to cancel my booking", [(RT, "handoff")]),
    ]),
    ("20 out of scope refund -> handoff", BOT1, [
        ("where is my refund", [(RT, "handoff")]),
    ]),

    # ── PM bot ───────────────────────────────────────────────────────────────
    ("21 PM confirms (601012)", BOT2, [
        ("hello", [(RT, "workflow"), (R, "booking id")]),
        ("601012", [(R, "honored for check-in")]),
        ("yes, the booking is confirmed", [
            (R, "guest will proceed with check-in"), ("done", "true")]),
    ]),
    ("22 PM overbooked but availability -> penalty -> accepts (601005)", BOT2, [
        ("hello", [(R, "booking id")]),
        ("601005", [(R, "honored for check-in")]),
        ("we cannot honor this booking", [(R, "reason")]),
        ("the property is overbooked", [
            (R, "available inventory"), (R, "penalties")]),
        ("okay, we will honor the booking", [
            (R, "guest will proceed"), ("done", "true")]),
    ]),
    ("23 PM genuinely overbooked (601004)", BOT2, [
        ("hello", [(R, "booking id")]),
        ("601004", [(R, "honored for check-in")]),
        ("no, we are overbooked", [(R, "alternate stay"), ("done", "true")]),
    ]),
    ("24 PM maintenance, no alternate room (601007)", BOT2, [
        ("hello", [(R, "booking id")]),
        ("601007", [(R, "honored for check-in")]),
        ("we cannot honor it", [(R, "reason")]),
        ("the property is under maintenance", [(R, "alternate rooms")]),
        ("no, nothing available", [(R, "alternate stay"), ("done", "true")]),
    ]),
    ("25 PM maintenance with alternate room (601006)", BOT2, [
        ("hello", [(R, "booking id")]),
        ("601006", [(R, "honored for check-in")]),
        ("cannot honor, maintenance work is going on", [(R, "alternate rooms")]),
        ("yes, we can arrange another room", [
            (R, "guest will proceed"), ("done", "true")]),
    ]),
    ("26 PM price low, meets ARR -> accepts (601008)", BOT2, [
        ("hello", [(R, "booking id")]),
        ("601008", [(R, "honored for check-in")]),
        ("no, the booking price is too low", [
            (R, "average realized rate"), (R, "honor")]),
        ("okay, we will accept the booking", [
            (R, "guest will proceed"), ("done", "true")]),
    ]),
    ("27 PM price low, below ARR -> compensation -> accepts (601009)", BOT2, [
        ("hello", [(R, "booking id")]),
        ("601009", [(R, "honored for check-in")]),
        ("we cannot accommodate, price is very low", [(R, "reason")]),
        ("the booking price is too low", [(R, "complimentary compensation")]),
        ("yes, that works for us", [
            (R, "added the complimentary amount"),
            (R, "guest will proceed"), ("done", "true")]),
    ]),
    ("28 PM price refused even with compensation (601010)", BOT2, [
        ("hello", [(R, "booking id")]),
        ("601010", [(R, "honored for check-in")]),
        ("no, we cannot take this booking", [(R, "reason")]),
        ("the price is too low", [(R, "complimentary compensation")]),
        ("no, we cannot accept this rate", [
            (R, "alternate stay"), ("done", "true")]),
    ]),

    # ── stock bot ────────────────────────────────────────────────────────────
    ("29 stock team confirms (601011)", BOT3, [
        ("hello", [(RT, "workflow"), (R, "booking id")]),
        ("601011", [(R, "honored at check-in")]),
        ("yes, the booking will be honoured", [
            (R, "booking stands confirmed"), ("done", "true")]),
    ]),
    ("30 stock team cannot confirm (601004)", BOT3, [
        ("hello", [(R, "booking id")]),
        ("601004", [(R, "honored at check-in")]),
        ("no, we cannot confirm, no inventory", [
            (R, "alternate property"), ("done", "true")]),
    ]),
]

for name, bot, turns in SCENARIOS:
    if only and only not in name:
        continue
    try:
        run(name, bot, turns)
    except Exception as exc:  # noqa: BLE001
        FAIL += 1
        FAILURES.append(name)
        print(f"ERROR {name}: {exc}")


# ── cross-bot end-to-end flows ───────────────────────────────────────────────
# These prove the meta-bot handoff: an outbound bot's CONVERSATION changes what
# the customer bot says, via the verification report in the shared backend.

import os
import pathlib

MOCK = "http://127.0.0.1:9021/api/v1"
STATE_JSON = pathlib.Path(__file__).resolve().parent.parent / "data" / "runtime_state.json"
mock = httpx.Client(base_url=MOCK, timeout=30)


def reset_mock_state():
    """Clear reports/dispositions so scripted outcomes apply again."""
    if STATE_JSON.exists():
        os.remove(STATE_JSON)


def e2e(name, steps, checks):
    """steps: [(bot, [messages...])]; checks: [(kind, want)] on the LAST reply."""
    global PASS, FAIL
    reset_mock_state()
    last = None
    transcript = []
    for bot, messages in steps:
        session = f"e2e_{abs(hash(name + bot)) % 10**10}"
        history = []
        for message in messages:
            last = turn(bot, session, message, history)
            history += [{"role": "user", "content": message},
                        {"role": "assistant", "content": last["reply"] or ""}]
            transcript.append(f"    [{bot[-6:]}] > {message}\n"
                              f"      {(last['reply'] or '')[:200]}")
    ok_all = True
    for kind, want in checks:
        if kind == "report":
            reports = mock.get("/verification-reports").json()["reports"]
            got = json.dumps(reports).lower()
        else:
            got = {"reply": (last["reply"] or "").lower(),
                   "slots": json.dumps((last.get("workflow") or {}).get("slots") or {}).lower(),
                   "done": str(last["done"]).lower()}[kind]
        if want.lower() not in got:
            ok_all = False
            transcript.append(f"      EXPECT {kind} ~ '{want}' — NOT FOUND")
    if ok_all:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"FAIL {name}")
        print("\n".join(transcript))


E2E = [
    # Customer bot → PM bot conversation → report → customer bot speaks the
    # reason the PM actually gave (not the scripted outcome, not a generic line).
    ("E1 PM bot denies overbooked -> customer bot says overbooked (601004)",
     [(BOT2, ["hello", "601004", "no, we are overbooked",
              "the property is fully overbooked, no rooms"]),
      (BOT1, ["confirm my booking", "601004", "Sneha Iyer",
              "please confirm with the property"])],
     [("report", '"deny_reason": "overbooked"'),
      ("report", '"outcome": "not_honored"'),
      ("reply", "overbooked"),
      ("reply", "alternate oyo property"),
      ("slots", '"pm_outcome_source": "live_report"')]),

    # A PM conversation that SAVES the booking flips the customer outcome to
    # confirmed even though the scripted outcome for this booking is a denial.
    ("E2 PM bot honors after penalty advisory -> customer bot confirms (601010)",
     [(BOT2, ["hello", "601010", "we are overbooked",
              "okay, we will honor the booking"]),
      (BOT1, ["confirm my booking", "601010", "Kavita Joshi",
              "please confirm with the property"])],
     [("report", '"outcome": "honored"'),
      ("report", '"resolution": "penalty_warning_accepted"'),
      ("reply", "successfully confirmed your booking with the property"),
      ("slots", '"pm_outcome_source": "live_report"')]),

    # PM unreachable (scripted no_answer) -> customer bot escalates to the stock
    # team -> the Stock bot's own conversation decides the final answer.
    ("E3 PM unreachable -> stock bot confirms -> customer bot confirms (601003)",
     [(BOT3, ["hello", "601003", "yes, the booking will be honoured"]),
      (BOT1, ["check-in confirmation", "601003", "Arjun Mehta",
              "confirm with the property"])],
     [("report", '"channel": "stock"'),
      ("report", '"outcome": "honored"'),
      ("reply", "unable to reach the property manager"),
      ("reply", "internal team has validated"),
      ("slots", '"stock_status": "confirmed"')]),

    # PM unreachable AND the stock team cannot confirm -> shift offer.
    ("E4 PM unreachable -> stock bot cannot confirm -> shift offered (601011)",
     [(BOT3, ["hello", "601011", "no, we cannot confirm, no inventory"]),
      (BOT1, ["check-in confirmation", "601011", "Sanjay Gupta",
              "confirm with the property"])],
     [("report", '"outcome": "not_honored"'),
      ("reply", "could not get a confirmation"),
      ("reply", "alternate oyo property")]),
]

for name, steps, checks in E2E:
    if only and only not in name:
        continue
    try:
        e2e(name, steps, checks)
    except Exception as exc:  # noqa: BLE001
        FAIL += 1
        FAILURES.append(name)
        print(f"ERROR {name}: {exc}")

reset_mock_state()  # leave a clean slate for manual testing

print(f"\n{PASS} passed, {FAIL} failed")
if FAILURES:
    print("failures:", FAILURES)
