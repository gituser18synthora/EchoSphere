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


def turn(bot_id, session, message, history, mocks=None, options=None):
    body = {"message": message, "sessionId": session, "messages": history}
    body.update(options or {})
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
        if len(item) == 4:
            message, mocks, expect, options = item
        elif len(item) == 3:
            message, mocks, expect = item
            options = None
        else:
            message, expect = item
            mocks, options = None, None
        d = turn(bot_id, session, message, history, mocks, options)
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
               # Structured post-call summary preview derived from the slots
               # as they stand after this turn (goal_policy.summaryFields).
               "summary": json.dumps(wf.get("structuredSummary") or {},
                                     ensure_ascii=False).lower(),
               "done": str(wf.get("done")).lower()}
        for kind, want in expect:
            if kind == "reply_not":
                if want.lower() in got["reply"]:
                    ok_all = False
                    log.append(f"      EXPECT reply NOT ~ '{want}' — FOUND")
            elif kind == "trace_not":
                if want.lower() in got["trace"].lower().split(","):
                    ok_all = False
                    log.append(f"      EXPECT trace NOT ~ '{want}' — FOUND")
            elif kind == "trace":
                if want.lower() not in got["trace"].lower().split(","):
                    ok_all = False
                    log.append(f"      EXPECT trace ~ '{want}' — NOT FOUND")
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


R, RN, RT, ST, TR, TN, SL, DN, SM = ("reply", "reply_not", "route", "status",
                                     "trace", "trace_not", "slots", "done",
                                     "summary")

MDND_MOCKS = mocks_for("Zepto Register MDND Concern")
UNIF_MOCKS = mocks_for("Zepto Register Uniform Deduction Concern")
ONBF_MOCKS = mocks_for("Zepto Register Onboarding Fee Concern")
RTO_MOCKS = mocks_for("Zepto Register RTO Concern")

SUITES = {
    "MDND": (STATE["BOT_MDND"], [
        # MDND flow v3 (zepto/setup/08_mdnd_flow_v3.py): readout -> narrative
        # -> reached+called asked TOGETHER when both unknown -> handover
        # recipient (wide vocabulary) -> guard name only for a guard handover
        # -> CX-support call -> verification with field-level corrections ->
        # other-deduction check -> register -> refund boundary -> closing.
        # Slot canonicals: reached/called/cx = "yes (…)"/"no (…)"; recipient
        # = guard / security | customer (direct) | mother | father | brother |
        # relative (other) | left at door | someone else | not handed over.
        ("MDND 01 reference-call replay (Hinglish), mocked ticket + summary",
         [("haan bol raha hoon",
           # The readout node is llm_grounded: under the DB-authored system
           # prompt the generated readout may be rejected and the authored
           # question spoken instead, so only the MDND mention is asserted
           # (grounded wording is never deterministic).
           [(RT, "workflow"), (TR, "n_ask_issue_desc"), (R, "mdnd")]),
          ("maine deliver kiya tha product, maine call kiya tha, customer "
           "ne bola ghar ke aage rakh do, maine wahan rakh diya, uske baad "
           "deduction hua jo nahi hona chahiye tha",
           # Story answered reached + called -> neither is asked again; the
           # handover recipient is the only open enquiry.
           [(SL, '"m_reached_location": "yes (reached the location)"'),
            (SL, '"m_called_customer": "yes (called the customer)"'),
            (TR, "n_msg_empathy"), (TR, "n_ask_handover"),
            (TN, "n_ask_reached_called"), (TN, "n_ask_reached"),
            (R, "परेशानी"),
            (RN, "delivery से पहले"), (RN, "location पर पहुंचे"),
            (SM, '"reach_customer_location": "yes"'),
            (SM, '"call_customer": "yes"'),
            (SM, '"hand_over_to": null')]),
          ("ye order maine customer ke guard ko handover kiya tha",
           # Guard handover -> the guard-name follow-up, nothing else.
           [(SL, '"m_handover_recipient": "guard / security"'),
            (TR, "n_cond_guard"), (TR, "n_ask_guard_name_known"),
            (RN, "cx support"), (R, "नाम"),
            (SM, '"hand_over_product": "yes"'),
            (SM, '"hand_over_to": "security_guard"')]),
          ("nahi, naam nahi pucha tha",
           # "not asked" is recorded, then the NEW CX-support enquiry.
           [(SL, '"m_guard_name": "not known (name not asked)"'),
            (TR, "n_ask_cx"), (R, "cx support"), (RN, "सही है")]),
          ("nahi, cx support se koi call nahi aaya",
           [(SL, '"m_cx_support_call": "no (no cx support call)"'),
            (TR, "n_hub_verify"), (R, "सही है"),
            (SM, '"call_cx": "no"')]),
          # MDND-only line: the confirmation registers at once — no
          # other-deduction question in between.
          ("ji sahi hai", MDND_MOCKS,
           [(TR, "n_api"), (TR, "n_confirmed"), (TR, "n_hub_more"),
            (TN, "n_ask_other"), (RN, "onboarding"),
            (SL, '"ticket_id": "zpt-mdnd-73412"'), (R, "payout"),
            # Final structured summary = the five reporting fields.
            (SM, '"call_customer": "yes"'),
            (SM, '"reach_customer_location": "yes"'),
            (SM, '"hand_over_product": "yes"'),
            (SM, '"hand_over_to": "security_guard"'),
            (SM, '"call_cx": "no"')]),
          ("nahi bas, ye refund kab tak aa jayega?", [(R, "refund")]),
          ("theek hai thank you",
           [(TR, "n_msg_close"), (ST, "done"), (DN, "true"), (R, "शुक्रिया")]),
          ]),
        # One utterance answers reached + called + handover + CX -> straight
        # to the guard-name follow-up, then verification.
        ("MDND 02 multi-answer: one utterance answers every enquiry",
         [("mdnd wala issue hai", [(TR, "n_ask_issue_desc")]),
          ("haan maine call kiya tha aur uske location par bhi gaya tha, "
           "order guard ko de diya tha, cx support se call bhi aaya tha, "
           "phir bhi deduction hua",
           [(SL, '"m_reached_location": "yes (reached the location)"'),
            (SL, '"m_called_customer": "yes (called the customer)"'),
            (SL, '"m_handover_recipient": "guard / security"'),
            (SL, '"m_cx_support_call": "yes (received cx support call)"'),
            (TR, "n_ask_guard_name_known"), (TN, "n_ask_reached_called"),
            (RN, "सौंपा"), (RN, "delivery से पहले"), (RN, "कोई call आया था"),
            (SM, '"call_cx": "yes"')]),
          ("haan pucha tha, guard ka naam Ramesh tha",
           # Name given with the yes -> the name ask is skipped too.
           [(SL, '"m_guard_name": "ramesh"'), (RN, "cx support"),
            (TR, "n_hub_verify"), (R, "सही है"), (RN, "नाम क्या था")]),
          ("sahi hai",
           [(TR, "n_api"), (TR, "n_pending"), (TR, "n_hub_more"),
            (TN, "n_ask_other")]),
          ("nahi bas", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
          ]),
        # Partial answer -> ONLY the missing questions, in order.
        ("MDND 03 partial answer: only the missing questions are asked",
         [("mdnd ka issue hai", [(TR, "n_ask_issue_desc")]),
          ("location par pahunch kar order guard ko de diya tha maine, phir "
           "bhi deduction aa gaya",
           # reached + recipient known -> the SINGLE call question (not the
           # combined one), recipient never re-asked.
           [(SL, '"m_reached_location": "yes (reached the location)"'),
            (SL, '"m_handover_recipient": "guard / security"'),
            (TR, "n_ask_called"), (TN, "n_ask_reached_called"),
            (R, "call"), (RN, "सौंपा"), (RN, "location पर पहुंचे")]),
          ("haan call kiya tha",
           [(SL, '"m_called_customer": "yes (called the customer)"'),
            (TR, "n_ask_guard_name_known"), (RN, "सौंपा"),
            (R, "नाम")]),
          ("yaad nahi",
           [(SL, '"m_guard_name": "not known (name not asked)"'),
            (TR, "n_ask_cx"), (R, "cx support")]),
          ("haan, cx se call aaya tha",
           [(SL, '"m_cx_support_call": "yes (received cx support call)"'),
            (TR, "n_hub_verify"), (R, "सही है")]),
          ]),
        # Nothing volunteered -> the COMBINED reached+called question, then
        # handover, then CX; a rejected summary with no detail asks which
        # part is wrong, applies the correction and re-verifies (no restart).
        ("MDND 04 combined question + correction at the verification step",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("bas deduction hua hai galat",
           [(TR, "n_cond_reached"), (TR, "n_cond_called"),
            (TR, "n_ask_reached_called"), (TN, "n_ask_reached"),
            (TN, "n_ask_called"), (R, "location"), (R, "call")]),
          ("haan dono kiya tha",
           # "both" fills reached AND called independently.
           [(SL, '"m_reached_location": "yes (reached the location)"'),
            (SL, '"m_called_customer": "yes (called the customer)"'),
            (TR, "n_ask_handover"), (RN, "delivery से पहले")]),
          ("customer ko de diya tha",
           # Customer handover -> no guard-name detour, CX question next.
           [(SL, '"m_handover_recipient": "customer (direct)"'),
            (TN, "n_ask_guard_name_known"), (TR, "n_ask_cx"),
            (R, "cx support")]),
          ("nahi",
           [(SL, '"m_cx_support_call": "no (no cx support call)"'),
            (TR, "n_hub_verify"), (R, "सही है")]),
          ("nahi galat hai",
           [(TR, "n_ask_correction"), (R, "ठीक करके")]),
          ("actually order maine guard ko diya tha customer ko nahi",
           # Only the recipient changes; the guard-name follow-up now
           # applies; reached/called/CX are NOT re-asked.
           [(SL, '"m_handover_recipient": "guard / security"'),
            (SL, '"m_called_customer": "yes (called the customer)"'),
            (SL, '"m_cx_support_call": "no (no cx support call)"'),
            (TN, "n_ask_reached_called"), (RN, "cx support"),
            (RN, "location पर पहुंचे"), (RN, "delivery से पहले"),
            (TR, "n_ask_guard_name_known"), (R, "नाम")]),
          ("nahi pucha",
           [(TR, "n_hub_verify"), (R, "सही है"), (RN, "कोई call आया था"),
            (SM, '"hand_over_to": "security_guard"')]),
          ("haan ab sahi hai", [(TR, "n_api"), (TN, "n_ask_other")]),
          ]),
        ("MDND 05 English caller end to end, live API fallback",
         [("I have an MDND issue",
           [(RT, "workflow"), (TR, "n_ask_issue_desc")]),
          ("I reached the customer location and delivered the order but "
           "still got a deduction of 400 rupees",
           [(SL, '"m_deduction_amount": "400"'),
            (SL, '"m_reached_location": "yes (reached the location)"'),
            (TR, "n_ask_called"), (TN, "n_ask_reached_called")]),
          ("yes I called the customer",
           [(SL, '"m_called_customer": "yes (called the customer)"'),
            (TR, "n_ask_handover")]),
          ("I handed it to the security guard",
           [(SL, '"m_handover_recipient": "guard / security"'),
            (TR, "n_ask_guard_name_known")]),
          ("no I did not ask",
           [(SL, '"m_guard_name": "not known (name not asked)"'),
            (TR, "n_ask_cx")]),
          ("no, nobody called me from support",
           [(SL, '"m_cx_support_call": "no (no cx support call)"'),
            (TR, "n_hub_verify")]),
          ("yes all correct",
           [(TR, "n_api"), (TR, "n_pending"), (TR, "n_hub_more"),
            (TN, "n_ask_other"),
            (SM, '"call_customer": "yes"'),
            (SM, '"reach_customer_location": "yes"'),
            (SM, '"hand_over_product": "yes"'),
            (SM, '"hand_over_to": "security_guard"'),
            (SM, '"call_cx": "no"')]),
          ("no thanks", [(TR, "n_msg_close"), (ST, "done"), (DN, "true")]),
          ]),
        ("MDND 06 explicit human request -> support handover",
         [("mujhe kisi support executive se baat karni hai",
           [(RT, "handoff")]),
          ]),
        ("MDND 07 policy question routes to the FAQ KB",
         [("MDND kya hota hai?", [(RT, "knowledge")]),
          ]),
        ("MDND 08 no context: every value captured together -> verify",
         [("mdnd issue hai", None, [(TR, "n_ask_issue_desc")],
           {"contextSource": "none"}),
          ("deduction 400 rupees tha, order 9203 tha, 4 August ko hua; "
           "main location par pahuncha, customer ko call kiya aur guard ko "
           "de diya, guard ka naam Ramesh tha, aur cx support se call aaya "
           "tha",
           None,
           [(SL, '"m_deduction_amount": "400"'),
            (SL, '"m_order_last4": "9203"'),
            (SL, '"m_deduction_date": "4 august"'),
            (SL, '"m_reached_location": "yes (reached the location)"'),
            (SL, '"m_called_customer": "yes (called the customer)"'),
            (SL, '"m_handover_recipient": "guard / security"'),
            (SL, '"m_guard_name": "ramesh"'),
            (SL, '"m_cx_support_call": "yes (received cx support call)"'),
            (TN, "n_ask_guard_name_known"), (RN, "cx support से कोई"),
            (TR, "n_hub_verify")],
           {"contextSource": "none"}),
          ]),
        ("MDND 09 no context partial: asks only missing date, then guard name",
         [("mdnd issue hai", None, [(TR, "n_ask_issue_desc")],
           {"contextSource": "none"}),
          ("400 rupees ka deduction hai, order 9203 hai; location par "
           "pahuncha, call kiya aur guard ko diya",
           None,
           [(SL, '"m_deduction_amount": "400"'),
            (SL, '"m_order_last4": "9203"'),
            (TR, "n_ask_date"), (R, "date"),
            (TN, "n_ask_reached_called"), (TN, "n_ask_reached"),
            (TN, "n_ask_called"), (TN, "n_ask_handover")],
           {"contextSource": "none"}),
          ("4 August ko", None,
           [(SL, '"m_deduction_date": "4 august"'),
            (TN, "n_ask_reached_called"), (RN, "सौंपा"),
            (TR, "n_ask_guard_name_known")],
           {"contextSource": "none"}),
          ]),
        ("MDND 10 confirmation registers at once (MDND-only line)",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("location par pahuncha, call kiya aur customer ko de diya, cx "
           "support se koi call nahi aaya",
           [(SL, '"m_handover_recipient": "customer (direct)"'),
            (SL, '"m_cx_support_call": "no (no cx support call)"'),
            (TN, "n_ask_guard_name_known"), (TR, "n_hub_verify")]),
          ("haan sab sahi hai",
           [(RN, "onboarding"), (RN, "दूसरा"), (TN, "n_ask_other"),
            (TR, "n_api"), (TR, "n_pending"), (TR, "n_hub_more"),
            (RN, "moment")]),
          ]),
        ("MDND 11 interrupted combined answer remains captured",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("location par pahunch gaya tha, customer ko call kiya aur order "
           "customer ke haath me de diya, support se koi call nahi",
           None,
           [(SL, '"m_reached_location": "yes (reached the location)"'),
            (SL, '"m_called_customer": "yes (called the customer)"'),
            (SL, '"m_handover_recipient": "customer (direct)"'),
            (SL, '"m_cx_support_call": "no (no cx support call)"'),
            (TR, "n_hub_verify")],
           {"interrupted": True}),
          ]),
        # ── v3 additions ──
        ("MDND 12 combined question, half answered -> only the call half",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("deduction galat hua hai", [(TR, "n_ask_reached_called")]),
          ("haan pahuncha tha",
           # Only reaching was confirmed -> the short call follow-up.
           [(SL, '"m_reached_location": "yes (reached the location)"'),
            (TR, "n_ask_called"), (RN, "सौंपा"),
            (R, "call"), (RN, "location पर पहुंचे")]),
          ("nahi, call nahi laga tha",
           [(SL, '"m_called_customer": "no (did not call)"'),
            (TR, "n_ask_handover"), (SM, '"call_customer": "no"'),
            (SM, '"reach_customer_location": "yes"')]),
          ]),
        ("MDND 13 combined question, split answer extracted independently",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("deduction galat hua hai", [(TR, "n_ask_reached_called")]),
          ("location par pahuncha tha par call nahi kiya",
           [(SL, '"m_reached_location": "yes (reached the location)"'),
            (SL, '"m_called_customer": "no (did not call)"'),
            (RN, "delivery से पहले"), (TR, "n_ask_handover")]),
          ]),
        ("MDND 13b combined question answered no to both",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("deduction galat hua hai", [(TR, "n_ask_reached_called")]),
          ("nahi, dono nahi kiya",
           [(SL, '"m_reached_location": "no (did not reach the location)"'),
            (SL, '"m_called_customer": "no (did not call)"'),
            (TR, "n_ask_handover"),
            (SM, '"reach_customer_location": "no"'),
            (SM, '"call_customer": "no"')]),
          ]),
        ("MDND 14 family-member handover (mother) -> no guard detour",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("location par pahuncha aur call kiya tha",
           [(TR, "n_ask_handover")]),
          ("customer ki mother ko de diya tha",
           [(SL, '"m_handover_recipient": "mother"'),
            (TN, "n_ask_guard_name_known"), (TR, "n_ask_cx"),
            (SM, '"hand_over_product": "yes"'),
            (SM, '"hand_over_to": "mother"')]),
          ]),
        ("MDND 14b doorstep handover",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("location par pahuncha aur call kiya tha", [(TR, "n_ask_handover")]),
          ("darwaze par rakh diya tha",
           [(SL, '"m_handover_recipient": "left at door"'),
            (TR, "n_ask_cx"), (SM, '"hand_over_to": "doorstep"')]),
          ]),
        ("MDND 14c brother handover in the narrative",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("location pe pahuncha, customer ko call kiya aur order uske bhai "
           "ko de diya",
           [(SL, '"m_handover_recipient": "brother"'),
            (TR, "n_ask_cx"), (SM, '"hand_over_to": "brother"')]),
          ]),
        ("MDND 14d not handed over -> hand_over_product No",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("location par pahuncha aur call kiya tha", [(TR, "n_ask_handover")]),
          ("kisi ko nahi de paya, order wapas le aaya",
           [(SL, '"m_handover_recipient": "not handed over"'),
            (TR, "n_ask_cx"), (SM, '"hand_over_product": "no"'),
            (SM, '"hand_over_to": null')]),
          ]),
        ("MDND 15 inline correction at verify: no 'which part' question",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("location par pahuncha, call kiya aur customer ko de diya, cx se "
           "koi call nahi aaya",
           [(TR, "n_hub_verify")]),
          ("nahi, maine call nahi kiya tha",
           # The rejection carries the fix -> applied, re-verified at once.
           [(SL, '"m_called_customer": "no (did not call)"'),
            (SL, '"m_reached_location": "yes (reached the location)"'),
            (RN, "ठीक करके"), (RN, "delivery से पहले"),
            (TR, "n_hub_verify"), (R, "सही है"),
            (SM, '"call_customer": "no"')]),
          ("haan ab sahi hai", [(TR, "n_api"), (TN, "n_ask_other")]),
          ]),
        ("MDND 16 correction names a field: only that field is re-asked",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("location par pahuncha, call kiya aur customer ko de diya, cx se "
           "koi call nahi aaya",
           [(TR, "n_hub_verify")]),
          ("nahi, cx wala galat hai",
           # CX cleared -> ONLY the CX question again.
           [(RN, "ठीक करके"), (TN, "n_ask_reached_called"),
            (TR, "n_ask_cx"), (R, "cx support"),
            (SM, '"call_cx": null')]),
          ("haan, call aaya tha",
           [(SL, '"m_cx_support_call": "yes (received cx support call)"'),
            (TR, "n_hub_verify"), (R, "सही है"),
            (SM, '"call_cx": "yes"')]),
          ]),
        ("MDND 18 cv_a00399bcc37b narrative: nothing re-asked",
         [("haan bol raha hoon", [(TR, "n_ask_issue_desc"), (RN, "onboarding")]),
          ("हाँ, मैंने कस्टमर को प्रोडक्ट जो था कस्टमर के घर पर जाकर डिलीवर किया "
           "और डिलीवर करने से पहले ना मैं कस्टमर को कॉल भी किया तो कस्टमर बोला कि "
           "मेरी मम्मी है मेरी मम्मी के पास ही प्रोडक्ट दे दो तो मैं उनके मम्मी को "
           "दिया, उनके माँ को प्रोडक्ट दिया और मैं चला आया।",
           [(SL, '"m_reached_location": "yes (reached the location)"'),
            (SL, '"m_called_customer": "yes (called the customer)"'),
            (SL, '"m_handover_recipient": "mother"'),
            (TR, "n_ask_cx"), (RN, "location पर पहुंचे"), (RN, "किसको सौंपा"),
            (SM, '"hand_over_to": "mother"'), (SM, '"call_customer": "yes"'),
            (SM, '"reach_customer_location": "yes"')]),
          ("नहीं, कोई कॉल नहीं आया था।",
           [(SL, '"m_cx_support_call": "no (no cx support call)"'),
            (TR, "n_hub_verify"), (R, "सही है")]),
          # cv_a00399bcc37b: the denial-style correction must re-ask ONLY the
          # recipient, not loop on the canned confirm line.
          ("नहीं नहीं नहीं, मुझे इसमें थोड़ा सा चेंज करना है कि प्रोडक्ट मैंने उनकी "
           "माँ को नहीं दिया था। सिक्योरिटी गार्ड को।",
           [(RN, "बस confirm करना है"), (RN, "कौन सी बात सही नहीं"),
            (TR, "n_ask_handover"), (RN, "location पर पहुंचे")]),
          ("security guard ko diya tha",
           [(SL, '"m_handover_recipient": "guard / security"'),
            (TR, "n_ask_guard_name_known"), (SM, '"hand_over_to": "security_guard"')]),
          ("nahi pucha", [(TR, "n_hub_verify"), (R, "सही है")]),
          ("haan ab sahi hai", [(TR, "n_api"), (TN, "n_ask_other")]),
          ]),
        ("MDND 19 cv_25e68bad6919 narrative: nothing re-asked",
         [("haan bol raha hoon", [(TR, "n_ask_issue_desc")]),
          ("हा. हाँ, मैं लोकेशन पर पहुँचा था और कॉल भी किया था, तो कस्टमर बोला कि "
           "मेरे घर पर मेरी माँ है। माँ के हाथ में दे दो। तो मैंने माँ को दे दिया था।",
           [(SL, '"m_handover_recipient": "mother"'), (TR, "n_ask_cx"),
            (RN, "location पर पहुंचे"), (RN, "किसको सौंपा")]),
          ]),
        ("MDND 20 guard name given with the yes -> name never asked",
         [("haan bol raha hoon", [(TR, "n_ask_issue_desc")]),
          ("मैंने कस्टमर को कॉल किया, लोकेशन पर पहुँच के कॉल किया। कस्टमर ने बोला "
           "गार्ड को दे दो। तो मैं गार्ड के हाथों में ही हैंडओवर कर दिया था।",
           [(SL, '"m_handover_recipient": "guard / security"'),
            (TR, "n_ask_guard_name_known"), (R, "नाम")]),
          ("हाँ, नाम पूछा था तो गार्ड बोला उसका नाम राजू है।",
           [(SL, '"m_guard_name": "राजू"'), (TR, "n_ask_cx"),
            (RN, "नाम क्या था"), (RN, "बस इतना confirm"), (R, "cx support")]),
          ]),
        ("MDND 21 bare yes -> name asked, only the name stored",
         [("haan bol raha hoon", [(TR, "n_ask_issue_desc")]),
          ("location par pahuncha, call kiya aur guard ko de diya",
           [(TR, "n_ask_guard_name_known")]),
          ("haan pucha tha", [(TR, "n_ask_guard_name"), (R, "नाम")]),
          ("राजू मैंने बताया ना अभी घाट का नाम राजू था",
           [(SL, '"m_guard_name": "राजू"'), (TR, "n_ask_cx")]),
          ]),
        ("MDND 17 correction via the which-part answer, then re-verify",
         [("mdnd issue hai", [(TR, "n_ask_issue_desc")]),
          ("location par pahuncha, call kiya aur customer ko de diya, cx se "
           "koi call nahi aaya",
           [(TR, "n_hub_verify")]),
          ("galat hai", [(TR, "n_ask_correction"), (R, "ठीक करके")]),
          ("location wala galat hai, main location par nahi pahuncha tha",
           # Clear + restate in one answer -> new value, no re-ask.
           [(SL, '"m_reached_location": "no (did not reach the location)"'),
            (TN, "n_ask_reached_called"), (TR, "n_hub_verify"),
            (R, "सही है"), (SM, '"reach_customer_location": "no"')]),
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

_args = [a for a in sys.argv[1:] if a != "-v"]
only = _args[0] if _args else None            # bot filter: MDND | UNIFORM | …
ONLY = _args[1:]                              # optional scenario-name filters

for key, (bot_id, scenarios) in SUITES.items():
    if only and only.upper() not in key:
        continue
    for name, turns in scenarios:
        if ONLY and not any(o.lower() in name.lower() for o in ONLY):
            continue
        run(bot_id, name, turns, verbose="-v" in sys.argv)

print(f"\n{PASS} passed, {FAIL} failed")
if FAILURES:
    print("failures:", FAILURES)
    sys.exit(1)
