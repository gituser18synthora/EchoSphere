"""End-to-end scenarios for the OUTBOUND Zepto OB-fee deduction verification
bot (zepto/setup/09_ob_deduction_outbound.py) via /testing/simulate — the
same route/brain/engine path a live call takes for text turns.

Every scenario starts with the partner's reply to the outbound greeting (the
greeting itself is the bot's opening line and is not a turn). Ticket
registration is replayed through ``mockToolResults`` from the connection's
responseSchema.example; turns without mocks exercise the live failure edge of
the reserved .example ticketing host (details-noted closing).

Covers the requested matrix: Hindi / Hinglish / Indian English; all yes;
first answer no (fee explained from the document); amount not communicated;
deducted-amount mismatch; several answers in one utterance; corrections;
incomplete answers (nulls kept); interruption; language switches; no repeated
questions; final structured fields.

Run:  env/bin/python zepto/tests/run_ob_deduction_scenarios.py ["OB-03" ...] [-v]
Requires: backend API on 9001, stage 09 (config) applied.
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
BOT = STATE["BOT_OB_OUTBOUND"]
CONNECTION = "Zepto Register OB Fee Verification"

c = httpx.Client(base_url=BASE, timeout=120)
r = c.post("/auth/login", json={"email": "zepto.config@zepto.com",
                                "password": "Demo@2026!"})
r.raise_for_status()
c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"

conns = c.get("/api-connections", params={"tenantId": TENANT}).json()["data"]
_conn = next(a for a in conns if a["name"] == CONNECTION)
MOCKS = {CONNECTION: (_conn.get("responseSchema") or {})["example"]}

PASS, FAIL = 0, 0
FAILURES = []


def turn(session, message, history, mocks=None, options=None):
    body = {"message": message, "sessionId": session, "messages": history}
    body.update(options or {})
    if mocks:
        body["mockToolResults"] = mocks
    r = c.post(f"/bots/{BOT}/testing/simulate", json=body)
    r.raise_for_status()
    return r.json()["data"]


R, RN, RT, ST, TR, TN, SL, SLN, DN, SM, LANG = (
    "reply", "reply_not", "route", "status", "trace", "trace_not", "slots",
    "slots_not", "done", "summary", "language")
RA = "reply_any"


def run(name, turns, verbose=False):
    global PASS, FAIL
    session = f"ob_{uuid.uuid4().hex[:10]}"
    history = []
    ok_all = True
    log = []
    for item in turns:
        options = None
        mocks = None
        if len(item) == 4:
            message, mocks, expect, options = item
        elif len(item) == 3:
            message, mocks, expect = item
            if isinstance(mocks, dict) and "interrupted" in mocks or (
                    isinstance(mocks, dict) and "language" in mocks):
                options, mocks = mocks, None
        else:
            message, expect = item
        # The bot's languageVoiceMap default is a tenant setting (admin@zepto.com
        # switched it to en-IN on 2026-09-11 for English testing); every scenario
        # pins its own conversation language so results do not depend on it.
        options = {"language": "hi-IN", **(options or {})}
        d = turn(session, message, history, mocks, options)
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": d.get("response") or ""})
        wf = d.get("workflow") or {}
        log.append(f"    > {message}\n      route={d.get('route')} lang={d.get('language')} "
                   f"status={wf.get('status')} done={wf.get('done')}\n"
                   f"      trace={','.join((wf.get('nodeTrace') or [])[-6:])}\n"
                   f"      slots={json.dumps(wf.get('slots') or {}, ensure_ascii=False)}\n"
                   f"      reply: {(d.get('response') or '')[:260]}")
        got = {"reply": (d.get("response") or "").lower(),
               "route": str(d.get("route") or ""),
               "status": str(wf.get("status")),
               "trace": ",".join(wf.get("nodeTrace") or []),
               "slots": json.dumps(wf.get("slots") or {}, ensure_ascii=False).lower(),
               "summary": json.dumps(wf.get("structuredSummary") or {},
                                     ensure_ascii=False).lower(),
               "done": str(wf.get("done")).lower(),
               "language": str(d.get("language") or "").lower()}
        for kind, want in expect:
            if kind == RA:
                if not any(str(w).lower() in got["reply"] for w in want):
                    ok_all = False
                    log.append(f"      EXPECT reply ~ any of {list(want)} — NOT FOUND")
            elif kind == RN:
                if want.lower() in got["reply"]:
                    ok_all = False
                    log.append(f"      EXPECT reply NOT ~ '{want}' — FOUND")
            elif kind == SLN:
                if want.lower() in got["slots"]:
                    ok_all = False
                    log.append(f"      EXPECT slots NOT ~ '{want}' — FOUND")
            elif kind == TN:
                if want.lower() in got["trace"].lower().split(","):
                    ok_all = False
                    log.append(f"      EXPECT trace NOT ~ '{want}' — FOUND")
            elif kind == TR:
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


Q1 = "सबसे पहले"          # Q_EXPLAINED core (unique to Q1)
Q2 = "क्या आपको बताया गया था कि कितना"   # Q_AMOUNT_INFORMED core (the readback also says "कितना amount deduct होगा")
Q2B = "कितना amount बताया गया था"  # Q_INFORMED_AMOUNT core
Q3 = "उतना ही है जितना"         # Q_AMOUNT_MATCHES core
Q_DED = "कितना amount deduct हुआ"
Q_WEEK = "किस date या week"
VERIFY_HI = "सही है"
MORE = "कोई और बात"
EN = {"language": "en-IN"}

SCENARIOS = [
    # ── A. Hindi ──────────────────────────────────────────────────────────
    ("OB-01 Hindi, all yes -> consistent, mocked ticket update, final fields", [
        ("हाँ जी, बोलिए",
         [(RT, "workflow"), (TR, "n_ask_explained"), (R, Q1),
          (SLN, "deduction_explained")]),                  # opener never swallowed
        ("हाँ, बताया गया था",
         [(SL, '"deduction_explained": "yes"'), (R, Q2), (TN, "n_msg_explain")]),
        ("हाँ, पाँच सौ रुपये बताया था",
         [(SL, '"amount_informed": "yes"'), (SL, '"informed_amount": "500"'),
          (RN, Q2B), (R, Q3)]),                            # amount known → not re-asked
        ("जी, उतना ही कटा है",
         [(SL, '"amount_matches": "yes"'), (TR, "n_hub_verify"), (R, VERIFY_HI),
          (SM, '"deduction_explained": "yes"'), (SM, '"amount_matches": "yes"')]),
        ("हाँ सही है",
         [(TR, "n_msg_consistent"), (R, "consistent"), (R, MORE),
          (SL, '"verification_status": "consistent"'), (RN, "refund")]),
        ("नहीं, बस इतना ही", MOCKS,
         [(TR, "n_api"), (TR, "n_confirmed"), (TR, "n_msg_close"), (DN, "true"),
          (SL, '"ticket_reference": "zpt-obf-70412"'),
          (SM, '"verification_status": "consistent"'),
          (SM, '"informed_amount": "500"'),
          (SM, '"deducted_amount": null'), (SM, '"payment_mode": null'),
          (SM, '"additional_concern": null'), (R, "धन्यवाद")]),
    ]),
    ("OB-02 Hindi, first answer NO ('kisi ne kuch nahi bataya') -> explained, only the amount asked, not-communicated outcome", [
        ("हाँ बोलिए", [(TR, "n_ask_explained")]),
        ("नहीं, मुझे किसी ने कुछ नहीं बताया था",
         [(SL, '"deduction_explained": "no"'), (SL, '"amount_informed": "no"'),   # explicit "nothing"
          (TR, "n_msg_explain"), (SL, '"explanation_given_on_call": "yes"'),
          (R, "onboarding fee"), (RA, ["store", "स्टोर"]),
          (RA, ["installment", "किश्त", "किस्त", "हफ्ते", "weekly"]),
          (R, Q_DED), (RN, Q2), (RN, "refund"), (RN, "वापस")]),
        ("छह सौ रुपये कटे",
         [(SL, '"deducted_amount": "600"'), (TR, "n_hub_verify"), (R, "600"), (RN, Q2), (RN, Q3)]),
        ("जी सही है",
         [(TR, "n_msg_not_communicated"), (TN, "n_msg_explain"),   # explanation never repeated
          (SL, '"verification_status": "amount_not_communicated"'), (RN, "consistent")]),
        ("नहीं", MOCKS, [(TR, "n_api"), (DN, "true"),
                 (SM, '"explanation_given_on_call": "yes"'), (SM, '"deducted_amount": "600"'),
                 (SM, '"informed_amount": null'), (SM, '"amount_matches": null')]),
    ]),
    # ── B. Hinglish ───────────────────────────────────────────────────────
    ("OB-03 Hinglish, amount NOT communicated -> recorded, deduction never called correct", [
        ("haan ji bol raha hoon", [(TR, "n_ask_explained")]),
        ("haan bataya tha, par kitna katega ye nahi bataya tha",
         [(SL, '"deduction_explained": "yes"'), (SL, '"amount_informed": "no"'),
          (RN, Q3), (R, Q_DED)]),                          # nothing to match → actual amount
        ("aath sau rupaye kate hain",
         [(SL, '"deducted_amount": "800"'), (TR, "n_hub_verify"), (R, VERIFY_HI)]),
        ("haan sahi hai",
         [(TR, "n_msg_not_communicated"), (SL, '"verification_status": "amount_not_communicated"'),
          (RN, "consistent"), (RN, "सही deduction"), (RN, "refund"), (R, MORE)]),
        ("nahi bas", MOCKS,
         [(DN, "true"), (SM, '"verification_status": "amount_not_communicated"'),
          (SM, '"amount_matches": null'), (SM, '"informed_amount": null'),
          (SM, '"deducted_amount": "800"')]),
    ]),
    ("OB-04 Hinglish, deducted amount MISMATCH -> discrepancy captured, no resolution invented", [
        ("haan boliye", [(TR, "n_ask_explained")]),
        ("haan bataya tha", [(SL, '"deduction_explained": "yes"'), (R, Q2)]),
        ("haan, 500 rupees bola tha",
         [(SL, '"amount_informed": "yes"'), (SL, '"informed_amount": "500"'), (R, Q3)]),
        ("nahi, zyada kata hai",
         [(SL, '"amount_matches": "no"'), (R, Q_DED)]),
        ("700 kata hai", [(SL, '"deducted_amount": "700"'), (R, Q_WEEK)]),
        ("pichle hafte ke payout mein",
         [(SL, '"deduction_date_or_week": "pichle hafte ke payout mein"'),
          (TR, "n_hub_verify"), (R, VERIFY_HI)]),
        ("haan sab sahi hai",
         [(TR, "n_msg_mismatch"), (SL, '"verification_status": "amount_mismatch"'),
          (RN, "consistent"), (RN, "refund"), (RN, "वापस"), (RN, "reverse"), (R, MORE)]),
        ("nahi", MOCKS,
         [(DN, "true"), (SM, '"verification_status": "amount_mismatch"'),
          (SM, '"informed_amount": "500"'), (SM, '"deducted_amount": "700"'),
          (SM, '"deduction_date_or_week": "pichle hafte ke payout mein"')]),
    ]),
    # ── multi-answer / corrections / incomplete ───────────────────────────
    ("OB-05 one utterance answers all three -> straight to readback, nothing re-asked", [
        ("haan ji", [(TR, "n_ask_explained")]),
        ("haan, deduction ke baare mein bataya tha aur bola tha 500 rupees katega, aur utna hi kata hai",
         [(SL, '"deduction_explained": "yes"'), (SL, '"amount_informed": "yes"'),
          (SL, '"informed_amount": "500"'), (SL, '"amount_matches": "yes"'),
          (TR, "n_hub_verify"), (RN, Q2), (RN, Q2B), (RN, Q3), (R, VERIFY_HI)]),
        ("haan sahi hai", [(TR, "n_msg_consistent")]),
    ]),
    ("OB-06 corrected amount -> confirm the dependent match again", [
        ("haan", [(TR, "n_ask_explained")]),
        ("haan bataya tha, 500 katega bola tha, utna hi kata",
         [(TR, "n_hub_verify"), (SL, '"informed_amount": "500"')]),
        ("nahi, 500 nahi — 600 bataya tha",
         [(SL, '"informed_amount": "600"'), (SLN, "amount_matches"),
          (R, Q3), (RN, "कौन सी बात"), (RN, Q1)]),
        ("haan utna hi kata", [(SL, '"amount_matches": "yes"'), (TR, "n_hub_verify")]),
        ("haan ab sahi hai", [(TR, "n_msg_consistent")]),
        ("nahi", MOCKS, [(DN, "true"), (SM, '"informed_amount": "600"')]),
    ]),
    ("OB-06b field named wrong without a value -> only that question re-asked", [
        ("haan", [(TR, "n_ask_explained")]),
        ("haan bataya tha, 500 katega bola tha, utna hi kata", [(TR, "n_hub_verify")]),
        ("nahi, match wala galat hai",
         [(SLN, "amount_matches"), (SL, '"informed_amount": "500"'),
          (R, Q3), (RN, Q1), (RN, Q2)]),
        ("nahi, 700 kata", [(SL, '"amount_matches": "no"'), (SL, '"deducted_amount": "700"'),
                            (R, Q_WEEK)]),
    ]),
    ("OB-09 incomplete: amount not remembered stays empty in the final fields", [
        ("haan", [(TR, "n_ask_explained")]),
        ("haan", [(SL, '"deduction_explained": "yes"'), (R, Q2)]),
        ("haan", [(SL, '"amount_informed": "yes"'), (R, Q2B)]),
        ("yaad nahi hai exact", [(SL, '"informed_amount": "not remembered"'), (R, Q3)]),
        ("haan utna hi", [(SL, '"amount_matches": "yes"'), (TR, "n_hub_verify")]),
        ("sahi hai", [(TR, "n_msg_consistent")]),
        ("nahi", MOCKS, [(DN, "true"), (SM, '"informed_amount": null'),
                         (SM, '"deducted_amount": null'), (SM, '"amount_matches": "yes"')]),
    ]),
    ("OB-10 interrupted readback still confirms; additional concern noted in the partner's words", [
        ("haan", [(TR, "n_ask_explained")]),
        ("haan bataya tha, 500 katega bola tha, utna hi kata", [(TR, "n_hub_verify")]),
        ("haan sahi hai", {"interrupted": True},
         [(TR, "n_msg_consistent"), (R, MORE)]),
        ("haan, ek aur baat — is hafte bhi ek deduction dikh raha hai", MOCKS,
         [(SL, '"additional_concern": "haan, ek aur baat'), (TR, "n_msg_additional_noted"),
          (TN, "n_ask_correction"), (RN, "क्या check करवाना है"),
          (SL, '"informed_amount": "500"'), (SLN, "deduction_date_or_week"),
          (DN, "true"), (SM, '"additional_concern": "haan, ek aur baat')]),
    ]),
    ("OB-11 off-script question mid-flow answered from the document, flow resumes", [
        ("haan", [(TR, "n_ask_explained")]),
        ("ye onboarding fee kitni hoti hai?",
         [(RA, ["store", "स्टोर", "अलग", "different", "change"]), (RN, "refund")]),
        ("haan bataya tha", [(SL, '"deduction_explained": "yes"'), (R, Q2)]),
    ]),
    ("OB-12 human request at the readback -> support executive handover", [
        ("haan", [(TR, "n_ask_explained")]),
        ("haan bataya tha, 500 katega bola tha, utna hi kata", [(TR, "n_hub_verify")]),
        ("nahi, mujhe kisi agent se baat karao",
         [(TR, "n_handover"), (RA, ["support executive", "executive"])]),
    ]),
    ("OB-12b human request in reply to the greeting -> handoff route", [
        ("mujhe kisi agent se baat karao",
         [(RT, "handoff")]),
    ]),
    ("OB-12c human request while a question is pending -> not refused, flow continues", [
        ("haan", [(TR, "n_ask_explained")]),
        ("mujhe kisi agent se baat karao",
         [(TR, "n_ask_explained"), (RA, ["support executive", "executive", "connect"]),
          (RN, "सिर्फ"), (RN, "नहीं कर सकत")]),
        ("haan bataya tha", [(SL, '"deduction_explained": "yes"'), (R, Q2)]),
    ]),
    # ── review 2026-09-11 (cv_e4df054b5651): explanation branch + extract-first ──
    ("OB-13 first answer NO -> explanation spoken (fixed text), deducted amount asked, then Q2", [
        ("haan boliye", [(TR, "n_ask_explained")]),
        ("nahi",
         [(SL, '"deduction_explained": "no"'), (TR, "n_msg_explain"),
          (SL, '"explanation_given_on_call": "yes"'),
          (R, "onboarding fee"), (R, "store"), (RA, ["installment", "installments"]),
          (RA, ["OB Fee", "ob fee"]), (R, Q_DED), (RN, Q2)]),
        ("400 rupaye", [(SL, '"deducted_amount": "400"'), (R, Q2)]),
        ("nahi", [(SL, '"amount_informed": "no"'), (TR, "n_hub_verify"), (R, "400")]),
        ("haan sahi hai", [(TR, "n_msg_not_communicated"),
                           (SL, '"verification_status": "amount_not_communicated"')]),
        ("nahi", MOCKS, [(DN, "true"), (SM, '"explanation_given_on_call": "yes"'),
                         (SM, '"deducted_amount": "400"'), (SM, '"amount_matches": null'),
                         (SM, '"informed_amount": null')]),
    ]),
    ("OB-14 example 1: informed 300 / deducted 400 in one utterance -> nothing re-asked, derived mismatch", [
        ("haan", [(TR, "n_ask_explained")]),
        ("Mujhe onboarding fee ke baare mein bataya gaya tha. Bola tha 300 katega, lekin 400 cut gaya.",
         [(SL, '"deduction_explained": "yes"'), (SL, '"amount_informed": "yes"'),
          (SL, '"informed_amount": "300"'), (SL, '"deducted_amount": "400"'),
          (SL, '"amount_matches": "no"'), (RN, Q2), (RN, Q2B), (RN, Q3), (RN, Q_DED),
          (R, Q_WEEK)]),
        ("pichle hafte", [(TR, "n_hub_verify"), (R, "300"), (R, "400")]),
        ("haan sahi hai", [(SL, '"verification_status": "amount_mismatch"'), (RN, "consistent")]),
        ("nahi", MOCKS, [(DN, "true"), (SM, '"informed_amount": "300"'),
                         (SM, '"deducted_amount": "400"'), (SM, '"amount_matches": "no"'),
                         (SM, '"verification_status": "amount_mismatch"')]),
    ]),
    ("OB-15 example 2/3: fee not explained + 400 cut -> explain, skip the amount question, ask Q2", [
        ("haan", [(TR, "n_ask_explained")]),
        ("Mujhe fee ke baare mein nahi bataya tha aur 400 rupaye cut gaye.",
         [(SL, '"deduction_explained": "no"'), (SL, '"deducted_amount": "400"'),
          (TR, "n_msg_explain"), (R, "onboarding fee"), (RN, Q_DED), (R, Q2)]),
        ("nahi, kuch nahi bataya tha", [(SL, '"amount_informed": "no"'), (TR, "n_hub_verify")]),
        ("haan sahi hai", [(SL, '"verification_status": "amount_not_communicated"')]),
        ("nahi", MOCKS, [(DN, "true"), (SM, '"deduction_explained": "no"'),
                         (SM, '"deducted_amount": "400"'), (SM, '"informed_amount": null')]),
    ]),
    ("OB-16 English: told three hundred, four hundred deducted -> same structured facts", [
        ("Yes, speaking", EN, [(TR, "n_ask_explained")]),
        ("They told me three hundred but four hundred was deducted.", EN,
         [(SL, '"deduction_explained": "yes"'), (SL, '"amount_informed": "yes"'),
          (SL, '"informed_amount": "300"'), (SL, '"deducted_amount": "400"'),
          (SL, '"amount_matches": "no"'), (R, "week")]),
        ("Last week", EN, [(TR, "n_hub_verify"), (R, "300"), (R, "400"), (R, "correct")]),
        ("Yes, all correct", EN, [(SL, '"verification_status": "amount_mismatch"')]),
        ("No, that's all", MOCKS, [(DN, "true"), (SM, '"amount_matches": "no"')], EN),
    ]),
    ("OB-17 mixed script: 300 बताया था लेकिन 400 कट गया", [
        ("हाँ", [(TR, "n_ask_explained")]),
        ("Mujhe 300 बताया था लेकिन 400 कट गया.",
         [(SL, '"informed_amount": "300"'), (SL, '"deducted_amount": "400"'),
          (SL, '"amount_matches": "no"'), (SL, '"deduction_explained": "yes"'), (R, Q_WEEK)]),
    ]),
    ("OB-18 same amount without figures; then a Yes -> No change at the readback", [
        ("haan", [(TR, "n_ask_explained")]),
        ("haan bataya tha, amount bhi bataya tha aur same amount kata",
         [(SL, '"amount_matches": "yes"'), (SL, '"amount_informed": "yes"'), (R, Q2B), (RN, Q3)]),
        ("yaad nahi", [(SL, '"informed_amount": "not remembered"'), (TR, "n_hub_verify"), (RN, Q3)]),
        ("nahi nahi, mujhe deduction ke baare mein nahi bataya gaya tha",
         [(SL, '"deduction_explained": "no"'), (TR, "n_msg_explain"), (R, "onboarding fee"),
          (SL, '"amount_matches": "yes"'), (R, Q_DED)]),          # figure still unknown → asked once
        ("paanch sau hi kata", [(SL, '"deducted_amount": "500"'), (TR, "n_hub_verify"),
                                (TN, "n_msg_explain")]),
        ("haan ab sahi hai", MOCKS, [(SL, '"verification_status": "consistent"')]),
    ]),
    # ── KB questions (cv_56df956b0430) ────────────────────────────────────
    ("OB-19 standalone KB queries before the flow (hi / Hinglish / en) answer from the document", [
        ("ऑनबोर्डिंग फीस क्या होती है?",
         [(RT, "knowledge"), (RA, ["store", "स्टोर", "onboarding fee", "ऑनबोर्डिंग फीस", "फीस"]),
          (RN, "सिर्फ"), (RN, "only assist")]),
        ("onboarding fee ek baar me pay kar sakte hain kya?",
         [(RT, "knowledge"), (RA, ["एक बार", "एक साथ", "one go", "at once", "installment", "किस्त", "किश्त"])]),
        ("Is the onboarding fee the same for every store?", EN,
         [(RT, "knowledge"), (RA, ["different", "differ", "store"])]),
        ("upfront fee kya hoti hai?",
         [(RT, "knowledge"), (RA, ["upfront", "अपफ्रंट", "minimum"])]),
        ("remaining amount weekly payout se deduct hota hai kya?",
         [(RT, "knowledge"), (RA, ["weekly", "हफ्ते", "payout", "installment", "किस्त", "किश्त"])]),
        ("OB fee aur standard deduction kya hota hai?",
         [(RT, "knowledge"), (RA, ["ob fee", "standard deduction", "admin panel", "installment", "किस्त", "किश्त"])]),
    ]),
    ("OB-20 KB question while Q1 is pending -> answered from KB, state kept, Q1 re-asked", [
        ("haan", [(TR, "n_ask_explained")]),
        ("waise ye onboarding fee kyu lete hain?",
         [(RT, "workflow_off_script_kb"), (RA, ["join", "rider", "onboarding fee", "ऑनबोर्डिंग"]),
          (RN, "सिर्फ"), (RN, "only assist"), (SLN, "deduction_explained")]),
        ("nahi bataya tha", [(SL, '"deduction_explained": "no"'), (TR, "n_msg_explain")]),
    ]),
    ("OB-21 workflow answer + KB question in one utterance (Q1 = no + 'fee hoti kya hai')", [
        ("haan", [(TR, "n_ask_explained")]),
        ("Nahi bataya tha. Waise onboarding fee kya hoti hai?",
         [(RT, "workflow_kb"), (SL, '"deduction_explained": "no"'), (TR, "n_msg_explain"),
          (RA, ["store", "स्टोर"]), (R, Q_DED)]),
        ("400 rupaye", [(SL, '"deducted_amount": "400"'), (R, Q2)]),
    ]),
    ("OB-22 300/400 facts + store-fee KB question -> all captured, KB answered, nothing repeated", [
        ("haan", [(TR, "n_ask_explained")]),
        ("Mujhe 300 bataya tha but 400 cut hua. Waise har store ki onboarding fee same hoti hai kya?",
         [(RT, "workflow_kb"), (SL, '"deduction_explained": "yes"'), (SL, '"amount_informed": "yes"'),
          (SL, '"informed_amount": "300"'), (SL, '"deducted_amount": "400"'),
          (SL, '"amount_matches": "no"'), (RA, ["अलग", "different", "differ", "store", "स्टोर"]),
          (RN, Q2), (RN, Q2B), (RN, Q3), (RN, Q_DED), (R, Q_WEEK)]),
        ("pichle hafte", [(TR, "n_hub_verify"), (R, "300"), (R, "400")]),
    ]),
    ("OB-23 KB questions never become slots (amount / installment questions)", [
        ("haan", [(TR, "n_ask_explained")]),
        ("Onboarding fee kitni hoti hai?",
         [(RT, "workflow_off_script_kb"), (SLN, "informed_amount"), (SLN, "deducted_amount"),
          (SLN, "amount_informed")]),
        ("Installment me cut hoti hai kya?",
         [(RT, "workflow_off_script_kb"), (SLN, "payment_mode")]),
        ("haan bataya tha", [(SL, '"deduction_explained": "yes"'), (R, Q2)]),
    ]),
    ("OB-24 upfront-fee question + deducted amount in one utterance (C)", [
        ("haan", [(TR, "n_ask_explained")]),
        ("haan bataya tha", [(R, Q2)]),
        ("nahi", [(SL, '"amount_informed": "no"'), (R, Q_DED)]),
        ("Upfront fee kya hai aur mere 500 cut gaye.",
         [(RT, "workflow_kb"), (SL, '"deducted_amount": "500"'), (RA, ["upfront", "अपफ्रंट", "minimum"]),
          (TR, "n_hub_verify")]),
    ]),
    # ── negative / hallucination pass ─────────────────────────────────────
    ("OB-25 unsupported policy questions -> 'not specified', no invented policy", [
        ("haan", [(TR, "n_ask_explained")]),
        ("Can onboarding fee be refunded?", EN,
         [(RA, ["does not specify", "not specify", "no information", "not available", "don't have", "cannot confirm", "not mention", "नहीं बताती", "नहीं बताया गया", "उपलब्ध जानकारी", "जानकारी नहीं", "specify नहीं"]),
          (RN, "yes, it can be refunded"), (RN, "will be refunded"), (RN, "eligible for a refund")]),
        ("Can the fee be waived?", EN,
         [(RA, ["does not specify", "not specify", "no information", "not available", "don't have", "cannot confirm", "not mention", "नहीं बताती", "नहीं बताया गया", "उपलब्ध जानकारी", "जानकारी नहीं", "specify नहीं"]),
          (RN, "can be waived"), (RN, "will be waived")]),
        ("What is the exact onboarding fee?", EN,
         [(RA, ["different", "differ", "store", "अलग", "does not specify", "not specify", "नहीं बताती"]),
          (RN, "rs.", ), (RN, "rupees is"), (RN, "500"), (RN, "1000"), (RN, "2000")]),
        ("Who decides the onboarding fee?", EN,
         [(RA, ["does not specify", "not specify", "no information", "not available", "don't have", "cannot confirm", "not mention", "नहीं बताती", "नहीं बताया गया", "उपलब्ध जानकारी", "जानकारी नहीं", "specify नहीं", "requirements", "store"]),
          (RN, "manager decides"), (RN, "hr decides"), (RN, "government")]),
        ("Can I cancel the deduction?", EN,
         [(RA, ["does not specify", "not specify", "no information", "not available", "don't have", "cannot confirm", "not mention", "नहीं बताती", "नहीं बताया गया", "उपलब्ध जानकारी", "जानकारी नहीं", "specify नहीं", "cancel नहीं", "verify", "team"]),
          (RN, "yes, you can cancel"), (RN, "will be cancelled")]),
    ]),
    ("OB-26 unrelated questions during the flow -> no fake KB answer, flow state kept", [
        ("haan", [(TR, "n_ask_explained")]),
        ("Aaj weather kaisa hai?",
         [(RN, "upfront"), (RN, "installment"), (RN, "किस्त"), (TR, "n_ask_explained"), (SLN, "deduction_explained")]),
        ("Meri salary kab aayegi?",
         [(RN, "upfront"), (RN, "installment"), (TR, "n_ask_explained"), (SLN, "deduction_explained")]),
        ("Store manager ka number kya hai?",
         [(RN, "upfront"), (RN, "installment"), (RN, "किस्त"), (TR, "n_ask_explained"), (SLN, "deduction_explained")]),
        ("haan bataya tha", [(SL, '"deduction_explained": "yes"'), (R, Q2)]),
    ]),
    ("OB-27 questions about values never populate personal slots", [
        ("haan", [(TR, "n_ask_explained")]),
        ("Can I pay in installments?", EN, [(SLN, "payment_mode")]),
        ("500 onboarding fee hoti hai kya?", [(SLN, "informed_amount"), (SLN, "deducted_amount"), (SLN, "amount_informed")]),
        ("Kya 400 rupees cut hona chahiye?", [(SLN, "deducted_amount"), (SLN, "informed_amount")]),
        ("Upfront fee 300 hoti hai kya?", [(SLN, "upfront_amount_paid"), (SLN, "informed_amount"), (SLN, "deducted_amount")]),
        ("haan bataya tha", [(SL, '"deduction_explained": "yes"'), (R, Q2), (SLN, "payment_mode"), (SLN, "upfront_amount_paid")]),
    ]),
    ("OB-28 workflow facts + unsupported KB question (refund) in one utterance", [
        ("haan", [(TR, "n_ask_explained")]),
        ("Mujhe 300 bola tha but 400 cut gaya. Waise onboarding fee refund hoti hai kya?",
         [(RT, "workflow_kb"), (SL, '"informed_amount": "300"'), (SL, '"deducted_amount": "400"'),
          (SL, '"amount_matches": "no"'), (SL, '"deduction_explained": "yes"'),
          (RA, ["नहीं है", "specify", "जानकारी", "information"]),
          (RN, "refund ho jayega"), (RN, "refund मिल"), (RN, "वापस मिल"), (RN, "will be refunded"),
          (RN, Q2), (RN, Q3), (RN, Q_DED), (R, Q_WEEK)]),
        ("pichle hafte", [(TR, "n_hub_verify"), (R, "300"), (R, "400")]),
    ]),
    # ── conversational quality (cv_2c60d51f61fb) ──────────────────────────
    ("OB-29 natural readback: 200 told / 300 deducted / last week Monday -> combined sentence, derived difference", [
        ("haan", [(TR, "n_ask_explained")]),
        ("haan bataya gaya tha", [(R, Q2)]),
        ("haan, mujhe bataya tha ki 200 katega, lekin 300 kata",
         [(SL, '"informed_amount": "200"'), (SL, '"deducted_amount": "300"'), (SL, '"amount_matches": "no"'),
          (R, Q_WEEK)]),
        ("पिछले हफ्ते मंडे को।",
         [(SL, '"deduction_date_or_week": "पिछले हफ्ते मंडे"'), (TR, "n_hub_verify"),
          (R, "200 रुपये बताए गए थे"), (R, "300 रुपये deduct हुए"), (R, "100 रुपये का difference"),
          (R, "पिछले हफ्ते मंडे"), (R, "क्या ये सारी details सही हैं"),
          (RN, "कितना amount deduct होगा, ये आपको बताया गया था"), (RN, "बताए गए और deduct हुए amount में difference है")]),
        ("नहीं, Sunday नहीं, Monday को हुआ था.",
         [(SL, '"deduction_date_or_week": "Monday"'), (TR, "n_hub_verify"),
          (R, "Monday"), (RA, ["update कर लेता", "बाकी सारी details सही"]), (RN, "बस confirm करना है"),
          (RN, "200 रुपये बताए गए थे")]),                             # not the whole readback again
        ("haan sahi hai",
         [(TR, "n_msg_mismatch"), (SL, '"verification_status": "amount_mismatch"'), (RN, "review"), (RN, "team")]),
        ("nahi", MOCKS, [(DN, "true"), (SM, '"deduction_date_or_week": "Monday"'),
                         (SM, '"informed_amount": "200"'), (SM, '"deducted_amount": "300"'),
                         (RN, "update कर रहा"), (RN, "confirm नहीं हुआ")]),
    ]),
    ("OB-30 equal amounts -> no difference spoken; readback stays one confirmation", [
        ("haan", [(TR, "n_ask_explained")]),
        ("haan bataya tha, 500 katega bola tha aur 500 hi kate",
         [(SL, '"amount_matches": "yes"'), (TR, "n_hub_verify"), (R, "500 रुपये"),
          (R, "उतना ही amount"), (RN, "difference"), (R, "क्या ये सारी details सही हैं")]),
    ]),
    ("OB-31 three KB questions after the outcome -> answered without the verbatim closing line every time", [
        ("haan", [(TR, "n_ask_explained")]),
        ("haan bataya tha, 300 katega bola tha, 400 kata", [(R, Q_WEEK)]),
        ("pichle hafte", [(TR, "n_hub_verify")]),
        ("haan sahi hai", [(TR, "n_msg_mismatch"), (R, MORE)]),
        ("upfront fee kya hoti hai?", [(RA, ["upfront", "अपफ्रंट", "minimum"]), (TR, "n_hub_more")]),
        ("installment ka option hai kya?", [(RA, ["installment", "किस्त", "किश्त", "एक बार", "one go"]), (TR, "n_hub_more")]),
        ("OB fee kya hota hai?", [(RA, ["ob fee", "onboarding fee", "ऑनबोर्डिंग"]), (TR, "n_hub_more")]),
        ("to ye refund kab tak aayega mera?",
         [(RA, ["नहीं", "specify", "जानकारी", "information"]), (RN, "review"), (RN, "team"), (RN, "टीम"),
          (TR, "n_hub_more")]),
        ("nahi bas", MOCKS, [(DN, "true"), (RN, "update कर रहा"), (RN, "confirm नहीं हुआ")]),
    ]),
    # ── C. Indian English ─────────────────────────────────────────────────
    ("OB-07 Indian English end to end, live API fallback (no mock)", [
        ("Yes, speaking", EN, [(TR, "n_ask_explained"), (LANG, "en")]),
        ("Yes, it was explained to me during onboarding", EN,
         [(SL, '"deduction_explained": "yes"'), (LANG, "en")]),
        ("They told me five hundred rupees would be deducted", EN,
         [(SL, '"amount_informed": "yes"'), (SL, '"informed_amount": "500"')]),
        ("No, they deducted seven hundred", EN,
         [(SL, '"amount_matches": "no"'), (SL, '"deducted_amount": "700"')]),
        ("I don't remember the week", EN,
         [(SL, '"deduction_date_or_week": "not remembered"'), (TR, "n_hub_verify")]),
        ("Yes, all correct", EN,
         [(TR, "n_msg_mismatch"), (SL, '"verification_status": "amount_mismatch"'),
          (RN, "refund")]),
        ("No, that's all, thank you", EN,
         [(TR, "n_api"), (TR, "n_pending"), (TR, "n_msg_close"), (DN, "true"),
          (SM, '"verification_status": "amount_mismatch"')]),
    ]),
    # /testing/simulate has no per-turn language detection (the live brain's
    # _maybe_switch_language does that from the audio transcript: a single
    # "yes"/"haan" never switches, a real English sentence does). The suite
    # therefore pins the language the brain would have decided for each turn
    # and checks that slots and flow position survive the switches.
    ("OB-08 language switching: Hindi -> Hinglish -> English, slots and flow unaffected", [
        ("हाँ जी बोलिए", [(TR, "n_ask_explained"), (LANG, "hi")]),
        ("haan bataya tha, sab explain kiya tha",
         [(SL, '"deduction_explained": "yes"'), (R, Q2), (LANG, "hi")]),   # Hinglish stays hi-IN
        ("yes", [(SL, '"amount_informed": "yes"'), (R, Q2B), (LANG, "hi")]),  # one word never switches
        ("I think they said around five hundred rupees", EN,
         [(SL, '"informed_amount": "500"'), (LANG, "en")]),               # real English sentence → en-IN
        ("Yes, exactly the same amount was deducted", EN,
         [(SL, '"amount_matches": "yes"'), (TR, "n_hub_verify"), (LANG, "en"),
          (R, "correct")]),                                                 # English readback
        ("Yes that's correct", EN, [(TR, "n_msg_consistent"), (R, "consistent with")]),
        ("No, nothing else", MOCKS, [(DN, "true")], EN),
    ]),
]


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "-v"]
    verbose = "-v" in sys.argv
    for name, turns in SCENARIOS:
        if args and not any(a in name for a in args):
            continue
        run(name, turns, verbose)
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("failures:", FAILURES)
        sys.exit(1)


if __name__ == "__main__":
    main()
