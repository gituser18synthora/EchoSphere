"""Offline replay of the AU Small Finance Bank flow through the REAL engine.

No database, no API, no LLM: the workflow definition from
``au_bank/setup/03_workflow.py`` is fed straight to
``WorkflowEngine.handle_turn_detailed`` with an in-memory checkpointer, so
every branch, slot and language switch is exercised deterministically.

Each scenario is a list of turns: (language, utterance, [(kind, expected)…]).
Expectation kinds:
  reply  — substring of the bot reply (case-insensitive)
  !reply — substring that must NOT appear in the reply
  trace  — substring of the comma-joined node trace
  !trace — node id that must NOT appear in the trace (re-authentication guard)
  slots  — substring of the JSON-dumped slots
  done   — "true" / "false"
  off    — "true" / "false"  (off-script: the flow held its node)

Run:  env/bin/python au_bank/tests/replay_flow.py [name-filter] [-v]
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

import shared.orchestration.workflow_engine as wfe  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "au_workflow", ROOT / "au_bank" / "setup" / "03_workflow.py")
_wf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wf)

DEFINITION = {"id": "wf_bb319e0f6fb5", "version": 99, "name": _wf.WORKFLOW_NAME,
              "nodes": _wf.NODES, "edges": _wf.EDGES}

EN, HI = "en-IN", "hi-IN"
R, NR, TR, NTR, SL, DN, OFF = ("reply", "!reply", "trace", "!trace", "slots",
                               "done", "off")

MOBILE = "9876543210"
OTP = "123456"


def auth(lang=EN):
    """The standard authentication opening (greeting is spoken by the bot)."""
    return [
        (lang, MOBILE, [(TR, "n_ask_mobile"), (R, "OTP" if lang == EN else "OTP")]),
        (lang, OTP, [(TR, "n_msg_verified"), (SL, '"customer_verified": true'),
                     (R, "verified" if lang == EN else "verify"),
                     (R, "assist" if lang == EN else "मदद")]),
    ]


SCENARIOS = [
    ("01 authentication then balance then transactions", [
        *auth(),
        (EN, "I want to check my account balance",
         [(TR, "n_hub_balance"), (R, "forty-five thousand two hundred and eighty"),
          (R, "recent transactions"), (NR, "45,280")]),
        (EN, "yes", [(TR, "n_msg_txns"), (R, "two thousand five hundred"),
                     (R, "fifteen thousand"), (R, "anything else")]),
        (EN, "no that's all", [(TR, "n_msg_close"), (R, "Thank you for banking"),
                               (DN, "true")]),
    ]),

    ("02 authentication then mini statement via SMS", [
        *auth(),
        (EN, "I need my mini statement",
         [(TR, "n_hub_ministmt"), (R, "last five transactions"), (R, "SMS")]),
        (EN, "yes please", [(TR, "n_msg_ministmt_sent"),
                            (R, "sent successfully"), (R, "anything else")]),
        (EN, "nothing else, thank you", [(R, "Thank you for banking"), (DN, "true")]),
    ]),

    ("03 lost card, confirm block, replacement card", [
        *auth(),
        (EN, "I have lost my debit card",
         [(TR, "n_hub_block"), (R, "sorry to hear"), (R, "block your debit card"),
          (R, "would you like me to proceed"), (NR, "has been blocked")]),
        (EN, "yes please block it",
         [(TR, "n_hub_replace"), (R, "successfully blocked"), (R, "replacement card")]),
        (EN, "yes", [(TR, "n_msg_replace_done"), (R, "D C 4 5 8 9 2 1"),
                     (R, "five to seven working days"), (R, "registered address")]),
        (EN, "no thanks", [(TR, "n_msg_close"), (R, "Have a great day"), (DN, "true")]),
    ]),

    ("04 lost card but customer declines the block", [
        *auth(),
        (EN, "my card is stolen", [(TR, "n_hub_block"), (R, "block")]),
        (EN, "no don't block it",
         [(TR, "n_msg_block_declined"), (R, "not blocked"), (NR, "successfully blocked")]),
        (EN, "no that's all", [(R, "Thank you for banking"), (DN, "true")]),
    ]),

    ("05 debit card PIN reset", [
        *auth(),
        (EN, "I want to reset my debit card PIN",
         [(TR, "n_ask_pin_otp"), (R, "OTP"), (NR, "existing PIN"), (NR, "current PIN")]),
        (EN, "654321", [(TR, "n_msg_pin_done"), (R, "Verification successful"),
                        (R, "PIN reset request has been processed"),
                        (R, "registered mobile number")]),
        (EN, "no", [(R, "Thank you for banking"), (DN, "true")]),
    ]),

    ("06 failed ATM transaction then service request", [
        *auth(),
        (EN, "Money was deducted but the ATM transaction failed",
         [(TR, "n_hub_failed_txn"), (R, "five thousand rupees"),
          (R, "twenty-four hours"), (R, "service request")]),
        (EN, "yes", [(TR, "n_msg_sr_done"), (R, "S R 7 8 4 5 2 1"),
                     (R, "twenty-four hours")]),
        (EN, "no that's all", [(R, "Thank you"), (DN, "true")]),
    ]),

    ("07 failed ATM transaction, customer declines the SR", [
        *auth(),
        (EN, "cash not received", [(TR, "n_hub_failed_txn")]),
        (EN, "no", [(TR, "n_msg_sr_declined"), (R, "reversed automatically"),
                    (NR, "S R 7 8 4 5 2 1")]),
        (EN, "bas itna hi", [(R, "Thank you"), (DN, "true")]),
    ]),

    ("08 account statement for the last 30 days", [
        *auth(),
        (EN, "please send me my account statement",
         [(TR, "n_hub_stmt"), (R, "last thirty days")]),
        (EN, "yes", [(TR, "n_msg_stmt_done"), (R, "registered email address"),
                     (R, "processed")]),
        (EN, "no", [(R, "Thank you"), (DN, "true")]),
    ]),

    ("09 profile email update with its own OTP", [
        *auth(),
        (EN, "I want to update my email address",
         [(TR, "n_ask_profile_otp"), (R, "OTP")]),
        (EN, "123456", [(TR, "n_msg_profile_done"), (R, "Verification successful"),
                        (R, "update request has been registered"),
                        (R, "confirmation message"), (NR, "has been changed")]),
        (EN, "no that's all", [(R, "Thank you"), (DN, "true")]),
    ]),

    ("10 customer changes intent mid flow (balance -> lost card)", [
        *auth(),
        (EN, "check my balance", [(TR, "n_hub_balance"), (R, "forty-five thousand")]),
        (EN, "actually my debit card is lost",
         [(TR, "n_hub_block"), (R, "block your debit card"),
          (NR, "recent transactions")]),
        (EN, "yes", [(TR, "n_hub_replace"), (R, "successfully blocked")]),
        (EN, "no", [(TR, "n_msg_noted"), (R, "anything else")]),
        (EN, "no that's all", [(R, "Thank you"), (DN, "true")]),
    ]),

    ("11 second service after the first one completes (no re-auth)", [
        *auth(),
        (EN, "check my balance", [(TR, "n_hub_balance")]),
        (EN, "no", [(TR, "n_msg_noted"), (R, "anything else")]),
        (EN, "yes, one more thing", [(TR, "n_hub_services"), (R, "assist")]),
        # The mini-statement prompt legitimately mentions the registered
        # mobile number as the SMS destination — the re-auth guard is the
        # trace: neither authentication node may be walked again.
        (EN, "I need my mini statement",
         [(TR, "n_hub_ministmt"), (NTR, "n_ask_mobile"), (NTR, "n_ask_otp"),
          (SL, '"customer_verified": true')]),
        (EN, "yes", [(TR, "n_msg_ministmt_sent"), (R, "sent successfully")]),
        (EN, "no", [(R, "Thank you"), (DN, "true")]),
    ]),

    ("12 direct service switch from the anything-else hub", [
        *auth(),
        (EN, "check my balance", [(TR, "n_hub_balance")]),
        (EN, "no", [(TR, "n_msg_noted")]),
        (EN, "I also want my account statement",
         [(TR, "n_hub_stmt"), (NTR, "n_ask_mobile"), (NTR, "n_ask_otp")]),
        (EN, "yes", [(TR, "n_msg_stmt_done"), (R, "registered email")]),
        (EN, "nothing else", [(R, "Thank you"), (DN, "true")]),
    ]),

    # ── language behaviour ────────────────────────────────────────────────
    ("13 language change BEFORE authentication (en -> hi)", [
        (EN, MOBILE, [(TR, "n_ask_mobile"), (R, "OTP")]),
        (HI, OTP, [(TR, "n_msg_verified"), (SL, '"customer_verified": true'),
                   (R, "पहचान"), (R, "मदद")]),
        (HI, "मेरा बैलेंस बताइए",
         [(TR, "n_hub_balance"), (R, "पैंतालीस हज़ार"), (R, "recent transactions")]),
        (HI, "हाँ", [(TR, "n_msg_txns"), (R, "दो हज़ार पाँच सौ")]),
        (HI, "नहीं, बस इतना ही", [(R, "धन्यवाद"), (DN, "true")]),
    ]),

    ("14 language change AFTER authentication (en -> hi), state preserved", [
        *auth(),
        (HI, "मेरा डेबिट कार्ड खो गया है",
         [(TR, "n_hub_block"), (R, "block"), (NTR, "n_ask_mobile"),
          (NTR, "n_ask_otp"), (SL, '"customer_verified": true')]),
        (HI, "हाँ, ब्लॉक कर दीजिए", [(TR, "n_hub_replace"), (R, "block")]),
        (HI, "हाँ", [(TR, "n_msg_replace_done"), (R, "D C 4 5 8 9 2 1"),
                     (R, "पाँच से सात")]),
        (HI, "नहीं", [(R, "धन्यवाद"), (DN, "true")]),
    ]),

    ("15 language change MID-workflow (hi -> en) keeps the pending step", [
        *auth(HI),
        (HI, "मुझे मिनी स्टेटमेंट चाहिए",
         [(TR, "n_hub_ministmt"), (R, "SMS")]),
        (EN, "yes please send it",
         [(TR, "n_msg_ministmt_sent"), (R, "sent successfully"),
          (NTR, "n_ask_mobile"), (NTR, "n_ask_otp"),
          (SL, '"customer_verified": true')]),
        (EN, "no that's all", [(R, "Thank you for banking"), (DN, "true")]),
    ]),

    ("16 language change mid-flow both ways, verification never repeats", [
        *auth(),
        (HI, "मेरा बैलेंस कितना है", [(TR, "n_hub_balance"), (R, "पैंतालीस हज़ार")]),
        (EN, "yes tell me the recent transactions",
         [(TR, "n_msg_txns"), (R, "two thousand five hundred"),
          (NTR, "n_ask_mobile"), (NTR, "n_ask_otp")]),
        (HI, "एक और काम है", [(TR, "n_hub_services"), (R, "मदद")]),
        (HI, "पैसे कट गए लेकिन कैश नहीं मिला",
         [(TR, "n_hub_failed_txn"), (R, "पाँच हज़ार"), (R, "चौबीस")]),
        (EN, "yes raise it", [(TR, "n_msg_sr_done"), (R, "S R 7 8 4 5 2 1")]),
        (HI, "नहीं, धन्यवाद", [(R, "धन्यवाद"), (DN, "true")]),
    ]),

    # ── guard rails ───────────────────────────────────────────────────────
    ("17 unverified caller cannot reach account facts", [
        (EN, "what is my account balance",
         [(TR, "n_ask_mobile"), (R, "mobile number"),
          (NR, "forty-five thousand"), (DN, "false")]),
        (EN, "just tell me the balance",
         [(R, "verify"), (NR, "forty-five thousand"), (DN, "false")]),
    ]),

    ("18 off-script turn holds the pending step (LLM answers it live)", [
        *auth(),
        (EN, "what is the interest rate on a savings account",
         [(OFF, "true"), (DN, "false")]),
    ]),

    ("19 human handover from a service hub", [
        *auth(),
        (EN, "check my balance", [(TR, "n_hub_balance")]),
        (EN, "connect me to a customer care executive",
         [(TR, "n_handover"), (R, "customer care"), (DN, "true")]),
    ]),

    ("20 direct replacement request without a block", [
        *auth(),
        (EN, "I want a replacement card",
         [(TR, "n_hub_replace_direct"), (R, "replacement request")]),
        (EN, "yes", [(TR, "n_msg_replace_done"), (R, "D C 4 5 8 9 2 1")]),
        (EN, "no", [(R, "Thank you"), (DN, "true")]),
    ]),
]


async def main() -> int:
    engine = wfe.WorkflowEngine()

    async def _mem(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    wfe.WorkflowEngine._get_checkpointer = _mem
    wfe.load_workflow_definition = lambda *a, **k: DEFINITION

    only = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    verbose = "-v" in sys.argv
    passed = failed = 0
    failures = []

    for name, turns in SCENARIOS:
        if only and only not in name:
            continue
        session = f"au-{abs(hash(name)) % 10**8}"
        ok_all, log = True, []
        for lang, text, expectations in turns:
            result = await engine.handle_turn_detailed(
                session_id=session, tenant_id="tn_b8897f32d4aa",
                bot_id="bot_ac634648c152", workflow_name="wf_bb319e0f6fb5",
                user_text=text, language=lang,
            )
            reply = result.get("reply") or ""
            got = {
                R: reply.lower(),
                TR: ",".join(result.get("trace") or []),
                SL: json.dumps(result.get("slots") or {}, ensure_ascii=False).lower(),
                DN: str(result.get("done")).lower(),
                OFF: str(bool(result.get("offScript"))).lower(),
            }
            log.append(f"    [{lang}] > {text}\n      trace={got[TR]} done={got[DN]} "
                       f"off={got[OFF]}\n      < {reply[:260]}")
            for kind, want in expectations:
                if kind == NR:
                    if want.lower() in got[R]:
                        ok_all = False
                        log.append(f"      EXPECT reply WITHOUT '{want}' — FOUND")
                elif kind == NTR:
                    if want in (result.get("trace") or []):
                        ok_all = False
                        log.append(f"      EXPECT trace WITHOUT '{want}' — FOUND")
                elif want.lower() not in got[kind]:
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
    raise SystemExit(asyncio.run(main()))
