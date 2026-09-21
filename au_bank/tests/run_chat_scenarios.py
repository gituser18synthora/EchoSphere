"""End-to-end AU Small Finance Bank scenarios via POST /bots/{id}/testing/chat.

This is the FULL runtime stack: real TurnRouter (the bot's saved intents), the
real workflow engine on the saved definition, real guardrails/compliance, and
the real LLM for off-script turns and language adaptation — the same path a
live call takes, minus audio.

Each scenario is a list of turns: (utterance, [(kind, expected)…]).
  reply  / !reply — substring that must / must not appear in the bot reply
  trace  / !trace — node id that must / must not appear in the node trace
  route           — substring of the router route
  slots           — substring of the JSON-dumped workflow slots
  done            — "true" / "false"
  lang            — the conversation language the server reports

Requires the backend API on 9001 (env/bin/uvicorn backend.main:app --port 9001).
Run:  env/bin/python au_bank/tests/run_chat_scenarios.py [name-filter] [-v]
"""

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "setup"))
from _common import BOT, check, client  # noqa: E402

R, NR, TR, NTR, RT, SL, DN, LG = ("reply", "!reply", "trace", "!trace", "route",
                                  "slots", "done", "lang")
MOBILE, OTP = "9876543210", "123456"

AUTH = [
    (MOBILE, [(RT, "workflow"), (TR, "n_ask_otp"), (R, "otp")]),
    (OTP, [(TR, "n_msg_verified"), (SL, '"customer_verified": true'),
           (R, "verified")]),
]

SCENARIOS = [
    ("01 authentication then balance then transactions", [
        *AUTH,
        ("I want to check my account balance",
         [(TR, "n_hub_balance"), (R, "forty-five thousand two hundred and eighty"),
          (R, "recent transactions")]),
        ("yes", [(TR, "n_msg_txns"), (R, "two thousand five hundred"),
                 (R, "fifteen thousand")]),
        ("no that's all", [(TR, "n_msg_close"), (R, "thank you for banking"),
                           (DN, "true")]),
    ]),
    ("02 authentication then mini statement", [
        *AUTH,
        ("I need my mini statement", [(TR, "n_hub_ministmt"), (R, "last five")]),
        ("yes", [(TR, "n_msg_ministmt_sent"), (R, "sent successfully")]),
        ("nothing else thank you", [(R, "thank you for banking"), (DN, "true")]),
    ]),
    ("03 lost card, confirm block, replacement", [
        *AUTH,
        ("I have lost my debit card",
         [(TR, "n_hub_block"), (R, "block"), (NR, "has been successfully blocked")]),
        ("yes please block it", [(TR, "n_hub_replace"), (R, "successfully blocked"),
                                 (R, "replacement")]),
        ("yes", [(TR, "n_msg_replace_done"), (R, "d c 4 5 8 9 2 1"),
                 (R, "five to seven working days")]),
        ("no that's all", [(R, "thank you for banking"), (DN, "true")]),
    ]),
    ("04 lost card but the customer declines the block", [
        *AUTH,
        ("my card is stolen", [(TR, "n_hub_block")]),
        ("no don't block it", [(TR, "n_msg_block_declined"), (R, "not blocked"),
                               (NR, "successfully blocked")]),
        ("no that's all", [(R, "thank you"), (DN, "true")]),
    ]),
    ("05 debit card PIN reset", [
        *AUTH,
        ("I want to reset my debit card PIN", [(TR, "n_ask_pin_otp"), (R, "otp")]),
        ("654321", [(TR, "n_msg_pin_done"), (R, "verification successful"),
                    (R, "pin reset request has been processed")]),
        ("no", [(R, "thank you"), (DN, "true")]),
    ]),
    ("06 failed ATM transaction then service request", [
        *AUTH,
        ("Money was deducted but the ATM transaction failed",
         [(TR, "n_hub_failed_txn"), (R, "five thousand"), (R, "twenty-four hours")]),
        ("yes", [(TR, "n_msg_sr_done"), (R, "s r 7 8 4 5 2 1")]),
        ("no that's all", [(R, "thank you"), (DN, "true")]),
    ]),
    ("07 account statement", [
        *AUTH,
        ("please send me my account statement",
         [(TR, "n_hub_stmt"), (R, "last thirty days")]),
        ("yes", [(TR, "n_msg_stmt_done"), (R, "registered email")]),
        ("no", [(R, "thank you"), (DN, "true")]),
    ]),
    ("08 profile email update", [
        *AUTH,
        ("I want to update my email address", [(TR, "n_ask_profile_otp"), (R, "otp")]),
        ("123456", [(TR, "n_msg_profile_done"), (R, "update request has been registered"),
                    (NR, "has been changed")]),
        ("no that's all", [(R, "thank you"), (DN, "true")]),
    ]),
    ("09 customer changes intent mid-flow", [
        *AUTH,
        ("check my balance", [(TR, "n_hub_balance"), (R, "forty-five thousand")]),
        ("actually my debit card is lost",
         [(TR, "n_hub_block"), (R, "block"), (NR, "two thousand five hundred")]),
        ("yes", [(TR, "n_hub_replace"), (R, "successfully blocked")]),
        ("no", [(TR, "n_msg_noted")]),
        ("no that's all", [(R, "thank you"), (DN, "true")]),
    ]),
    ("10 another service after one completes, no re-authentication", [
        *AUTH,
        ("check my balance", [(TR, "n_hub_balance")]),
        ("no", [(TR, "n_msg_noted"), (R, "anything else")]),
        ("yes one more thing", [(TR, "n_hub_services")]),
        ("I need my mini statement",
         [(TR, "n_hub_ministmt"), (NTR, "n_ask_mobile"), (NTR, "n_ask_otp")]),
        ("yes", [(TR, "n_msg_ministmt_sent")]),
        ("no", [(R, "thank you"), (DN, "true")]),
    ]),
    ("11 language change BEFORE authentication", [
        (MOBILE, [(TR, "n_ask_otp")]),
        ("मेरा OTP है 123456", [(TR, "n_msg_verified"), (LG, "hi-IN"),
                                (SL, '"customer_verified": true'), (R, "पहचान")]),
        ("मेरा बैलेंस कितना है", [(TR, "n_hub_balance"), (R, "पैंतालीस हज़ार")]),
        ("हाँ", [(TR, "n_msg_txns"), (R, "दो हज़ार")]),
        ("नहीं, बस इतना ही", [(R, "धन्यवाद"), (DN, "true")]),
    ]),
    ("12 language change AFTER authentication, state preserved", [
        *AUTH,
        ("मेरा डेबिट कार्ड खो गया है",
         [(TR, "n_hub_block"), (LG, "hi-IN"), (NTR, "n_ask_mobile"),
          (NTR, "n_ask_otp"), (SL, '"customer_verified": true')]),
        ("हाँ, ब्लॉक कर दीजिए", [(TR, "n_hub_replace")]),
        ("हाँ", [(TR, "n_msg_replace_done"), (R, "d c 4 5 8 9 2 1")]),
        ("नहीं", [(R, "धन्यवाद"), (DN, "true")]),
    ]),
    ("13 language change MID-workflow keeps the pending step", [
        *AUTH,
        ("मुझे मिनी स्टेटमेंट चाहिए", [(TR, "n_hub_ministmt"), (LG, "hi-IN")]),
        ("yes please send it",
         [(TR, "n_msg_ministmt_sent"), (NTR, "n_ask_mobile"), (NTR, "n_ask_otp"),
          (SL, '"customer_verified": true')]),
        ("no that's all", [(R, "thank you"), (DN, "true")]),
    ]),
    ("14 unverified caller gets no account facts", [
        ("what is my account balance",
         [(TR, "n_ask_mobile"), (NR, "forty-five thousand"), (DN, "false")]),
    ]),
    ("15 customer ends the conversation immediately after a service", [
        *AUTH,
        ("check my balance", [(TR, "n_hub_balance")]),
        ("no", [(TR, "n_msg_noted"), (R, "anything else")]),
        ("no, I'm done, thank you", [(R, "thank you for banking"), (DN, "true")]),
    ]),
]


def main() -> int:
    c = client(timeout=120)
    only = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    verbose = "-v" in sys.argv
    passed = failed = 0
    failures = []

    for name, turns in SCENARIOS:
        if only and only not in name:
            continue
        session = f"au_{uuid.uuid4().hex[:10]}"
        history, language = [], None
        ok_all, log = True, []
        for text, expectations in turns:
            payload = {"message": text, "sessionId": session, "messages": history}
            if language:
                payload["language"] = language
            r = c.post(f"/bots/{BOT}/testing/chat", json=payload)
            if r.status_code >= 300:
                ok_all = False
                log.append(f"    > {text}\n      HTTP {r.status_code} {r.text[:300]}")
                break
            d = r.json()["data"]
            language = d.get("language") or language
            reply = d.get("reply") or ""
            history += [{"role": "user", "content": text},
                        {"role": "assistant", "content": reply or "(none)"}]
            wf = d.get("workflow") or {}
            trace = wf.get("nodeTrace") or []
            got = {R: reply.lower(),
                   TR: ",".join(trace),
                   RT: d.get("route") or "",
                   SL: json.dumps(wf.get("slots") or {}, ensure_ascii=False).lower(),
                   DN: str(d.get("done")).lower(),
                   LG: d.get("language") or ""}
            log.append(f"    > {text}\n      route={got[RT]} lang={got[LG]} "
                       f"trace={got[TR]} done={got[DN]}\n      < {reply[:240]}")
            for kind, want in expectations:
                if kind == NR:
                    if want.lower() in got[R]:
                        ok_all = False
                        log.append(f"      EXPECT reply WITHOUT '{want}' — FOUND")
                elif kind == NTR:
                    if want in trace:
                        ok_all = False
                        log.append(f"      EXPECT trace WITHOUT '{want}' — FOUND")
                elif want.lower() not in got[kind].lower():
                    ok_all = False
                    log.append(f"      EXPECT {kind} ~ '{want}' — NOT FOUND")
        if ok_all:
            passed += 1
            print(f"PASS {name}")
            if verbose:
                print("\n".join(log))
        else:
            failed += 1
            failures.append(name)
            print(f"FAIL {name}")
            print("\n".join(log))

    print(f"\n{passed} passed, {failed} failed")
    for f in failures:
        print(f"  FAILED: {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
