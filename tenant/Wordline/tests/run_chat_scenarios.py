"""End-to-end scenarios for the Wordline bot through the LOCAL platform:
POST /bots/{id}/testing/chat → real workflow engine → real tool executor →
the Wordline merchant API on :9023 (no mocks).

Each scenario is a list of (utterance, expectations).  Expectation kinds:
  reply   substring of the lowercased bot reply
  noreply substring that must NOT be in the reply
  trace   node id that must appear in the turn's node trace
  notrace node id that must NOT appear in the turn's node trace
  slot    "name=value" — lowercased str() of a workflow slot
  done    "true" / "false"

Run (API on 9001, merchant API on 9023, services started with
WORDLINE_MERCHANT_API_KEY in their environment):
    env/bin/python tenant/Wordline/tests/run_chat_scenarios.py [name-filter]
    … --base https://<host>/api/v1 --report /tmp/wl_chat_report.json
        # another environment (login via ECHOSPHERE_SUPER_ADMIN_* / WORDLINE_SETUP_*);
        # --report writes the per-scenario results setup/04_go_live.py requires
    env/bin/python tenant/Wordline/tests/run_chat_scenarios.py --api-down
        # only the "unavailable" scenario; stop the merchant API first
    env/bin/python tenant/Wordline/tests/run_chat_scenarios.py --auth-fail
        # only the "unavailable" scenario; run the merchant API under a key the
        # platform does not hold first (WORDLINE_MERCHANT_API_KEY=other ./tenant/Wordline/run.sh)
        # so every lookup answers 401 → failure edge, never the not-found re-ask
    env/bin/python tenant/Wordline/tests/run_chat_scenarios.py --previous-service --base …
        # an environment whose merchant service predates address / contact person
        # (DEMO until its code deploy): the record-aware request / visit scenarios
        # are replaced by their fallback expectations (the original asks run)
"""

from __future__ import annotations

import json
import pathlib
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tenant.Wordline.setup.common import BOT_ID, DEFAULT_BASE, client  # noqa: E402

API_DOWN = "--api-down" in sys.argv or "--auth-fail" in sys.argv
PREVIOUS_SERVICE = "--previous-service" in sys.argv


def _opt(flag: str) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    return None


BASE = _opt("--base") or DEFAULT_BASE
REPORT = _opt("--report")
_consumed = {v for f in ("--base", "--report") for v in (_opt(f),) if v}
FILTER = next((a for a in sys.argv[1:] if not a.startswith("--") and a not in _consumed), "")

T = "trace"
# Workflow shape: the DEMO v12 per-field confirmations ("as per our records,
# your Outlet Name is X — correct?" → bank → asset [→ contact on battery]),
# with the lookup wired in front of them (setup/02, workflow v13 on LOCAL).
YES = "haan theek hai"
SCENARIOS = {
    "paper_roll_found": [
        ("Hello, I need paper roll for my machine", [(T, "n_ask_tid"), ("reply", "terminal")]),
        ("9 1 3 7 9 7 8 4", [(T, "n_api_lookup"), (T, "n_has_ask_merchant"), (T, "n_ask_merchant"),
                             ("reply", "agarwal public school"), ("noreply", "axis bank"),
                             ("noreply", "sharma general store"),
                             ("slot", "merchant_found=true"), ("slot", "customer_verified=true"),
                             ("slot", "terminal_id=91379784")]),
        (YES, [(T, "n_pr_bank"), ("reply", "axis bank")]),
        # TTS-ready "Paytivo 7210 2G plus WiFi" from the 2026-11-10 service;
        # "PAYTIVO-7210-2G+WIFI" from a service that predates it — both say "paytivo".
        (YES, [(T, "n_pr_asset"), ("reply", "paytivo")]),
        (YES, [(T, "n_options"), ("reply", "sms")]),
        ("SMS wala batao", [(T, "n_sms"), ("reply", "575758")]),
        ("no thanks", [(T, "n_end"), ("done", "true")]),
    ],
    "paper_roll_paused_dictation_then_found": [
        ("paper roll chahiye", [(T, "n_ask_tid")]),
        ("One, two, three. Four.", [("notrace", "n_api_lookup"), ("reply", "noted 4 digits")]),
        ("Six. 7 8.", [(T, "n_api_lookup"), (T, "n_tid_retry"), ("reply", "could not find")]),
        ("9 1 3 7 9 7 8 4", [(T, "n_api_lookup_retry"), (T, "n_ask_merchant"),
                             ("reply", "agarwal public school"),
                             ("slot", "spoken_terminal_id_retry=9 1 3 7 9 7 8 4")]),
    ],
    "paper_roll_still_not_found_continues": [
        ("mujhe paper roll chahiye", [(T, "n_ask_tid")]),
        ("12345678", [(T, "n_tid_retry")]),
        ("87654321", [(T, "n_lookup_unverified"), (T, "n_options_unverified"),
                      ("reply", "unable to locate"), ("noreply", "verified your"), ("reply", "ivr")]),
        ("IVR wala batao", [(T, "n_ivr"), ("reply", "one eight six zero")]),
    ],
    "paper_roll_wrong_details_disconnect": [
        ("paper roll request", [(T, "n_ask_tid")]),
        ("91379784", [(T, "n_ask_merchant"), ("reply", "agarwal public school")]),
        ("nahi galat hai", [(T, "n_not_verified"), ("reply", "disconnect"), ("done", "true")]),
    ],
    "battery_natural_tid_contact_readback": [
        ("battery problem hai, jaldi drain ho rahi hai", [(T, "n_bat_tid")]),
        ("1 2 3 1.", [("notrace", "n_bat_api_lookup"), ("reply", "noted 4 digits")]),
        ("410.", [(T, "n_bat_api_lookup"), (T, "n_has_bat_details"), (T, "n_bat_details"),
                  ("reply", "bhanu medical and general store"), ("slot", "terminal_id=1231410a")]),
        (YES, [(T, "n_bat_bank"), ("reply", "idbi bank")]),
        (YES, [(T, "n_bat_asset"), ("reply", "antera al9220u")]),
        (YES, [(T, "n_bat_contact"), ("reply", "9892858883")]),
        # wf v14 (Wordline team, 2026-11-10): "switch off the terminal" step first
        (YES, [(T, "n_bat_switch_off"), ("reply", "switch off")]),
    ],
    "spoken_pairs_one_lookup": [
        ("I need paper roll", [(T, "n_ask_tid")]),
        ("91.", [("reply", "noted 2 digits")]),
        ("37.", [("reply", "noted 4 digits"), ("notrace", "n_api_lookup")]),
        ("97 84.", [(T, "n_api_lookup"), (T, "n_ask_merchant"), ("reply", "agarwal public school")]),
    ],
    "followup_fact_question_after_lookup": [
        ("paper roll request", [(T, "n_ask_tid")]),
        ("91379784", [(T, "n_ask_merchant"), ("reply", "agarwal public school")]),
        ("which bank is my terminal with?", [("reply", "axis")]),
    ],
}
# 2026-11-10: the API returns TTS-ready values plus address / contact_person;
# with a found record the request / visit steps read them back (workflow step 3).
AGARWAL_ADDRESS = "agarwal public school, bicholi mardana road, indore, madhya pradesh 452016"
EYE_WORLD_ADDRESS = ("shop number 17, saifee park, 680 dr. mascarenhas road, mazgaon, mumbai, "
                     "maharashtra 400010")
_TO_OPTIONS = [
    ("I need paper roll", [(T, "n_ask_tid")]),
    ("91379784", [(T, "n_ask_merchant"), ("reply", "agarwal public school")]),
    (YES, [(T, "n_pr_bank"), ("reply", "axis bank")]),
    (YES, [(T, "n_pr_asset")]),
    (YES, [(T, "n_options")]),
]
_TO_DAMAGED = [
    ("battery problem hai", [(T, "n_bat_tid")]),
    ("1974857A", [(T, "n_bat_details"), ("reply", "eye world optics")]),
    (YES, [(T, "n_bat_bank")]),
    (YES, [(T, "n_bat_asset")]),
    (YES, [(T, "n_bat_contact"), ("reply", "9920125552")]),
    (YES, [(T, "n_bat_switch_off")]),
    ("haan switch off kar diya", [(T, "n_bat_check")]),
    ("battery phool gayi hai, swollen hai", [(T, "n_bat_damaged")]),
]
RECORD_SCENARIOS = {
    "paper_roll_tts_ready_values": [
        ("I need paper roll", [(T, "n_ask_tid")]),
        ("91379784", [(T, "n_ask_merchant"), ("reply", "your outlet name is agarwal public school."),
                      ("slot", f"address={AGARWAL_ADDRESS}"), ("slot", "contact_person=k c tiwari"),
                      ("slot", "outlet_name=agarwal public school")]),
        (YES, [(T, "n_pr_bank"), ("reply", "axis bank")]),
        # the tester may switch to Hindi after "haan theek hai": language-neutral checks
        (YES, [(T, "n_pr_asset"), ("reply", "model paytivo 7210 2g plus wifi"),
               ("noreply", "paytivo-7210")]),
    ],
    "paper_roll_raise_with_record": [
        *_TO_OPTIONS,
        ("aap request raise kar do", [(T, "n_raise_has_record"), (T, "n_raise_record"),
                                      ("notrace", "n_raise"),
                                      ("reply", AGARWAL_ADDRESS), ("reply", "k c tiwari"),
                                      ("noreply", "first, please share"), ("noreply", "{")]),
        ("sab sahi hai, weekly off sunday hai, subah 10 se shaam 6 baje, 5 rolls chahiye",
         [(T, "n_anything_else"), ("slot", "request_details=sab sahi hai, weekly off sunday hai, "
                                           "subah 10 se shaam 6 baje, 5 rolls chahiye")]),
    ],
    "battery_damaged_visit_with_record": [
        *_TO_DAMAGED,
        ("haan aage badho", [(T, "n_bat_visit_has_record"), (T, "n_bat_visit_record"),
                             ("notrace", "n_bat_visit"),
                             ("reply", EYE_WORLD_ADDRESS), ("reply", "ali h"), ("noreply", "{")]),
        ("address sahi hai, sunday off hai, subah 11 baje aa jaiye",
         [(T, "n_has_docket"), ("reply", "docket"),
          ("slot", "visit_details=address sahi hai, sunday off hai, subah 11 baje aa jaiye")]),
    ],
}
# LLM follow-ups once the details are verified (options hub): the verified
# context carries the TTS-ready values and the model says them as written.
# Opt-in (--llm-followups): the Testing Studio chat generates these replies with
# max_tokens=120 on a reasoning model and sometimes gets an empty completion
# (→ "Sorry, could you tell me a bit more…" / the off-script placeholder) —
# a platform tester limit, not a bot behaviour; the voice path has its own budget.
LLM_FOLLOWUPS = {
    "followup_contact_person_question": [
        *_TO_OPTIONS,
        ("What is the contact person name in your records?", [("reply", "k c tiwari"),
                                                               ("noreply", "t i w a r i")]),
    ],
    "followup_registered_address_question": [
        *_TO_OPTIONS,
        ("What is my registered address?", [("reply", "bicholi mardana road"),
                                            ("noreply", "merchant pin code")]),
    ],
}
# The same journeys on a service without the new fields: the gates stay false
# and the ORIGINAL asks run, word for word.
PREVIOUS_SERVICE_SCENARIOS = {
    "paper_roll_raise_previous_service": [
        *_TO_OPTIONS,
        ("aap request raise kar do", [(T, "n_raise_has_record"), (T, "n_raise"),
                                      ("notrace", "n_raise_record"),
                                      # original script asks for every detail (EN or HI)
                                      ("reply", "outlet name"), ("noreply", "bicholi")]),
    ],
    "battery_damaged_visit_previous_service": [
        *_TO_DAMAGED,
        ("haan aage badho", [(T, "n_bat_visit_has_record"), (T, "n_bat_visit"),
                             ("notrace", "n_bat_visit_record"), ("reply", "complete address"),
                             ("noreply", "saifee")]),
    ],
}
if PREVIOUS_SERVICE:
    SCENARIOS.update(PREVIOUS_SERVICE_SCENARIOS)
else:
    SCENARIOS.update(RECORD_SCENARIOS)
    if "--llm-followups" in sys.argv:
        SCENARIOS.update(LLM_FOLLOWUPS)
API_DOWN_SCENARIOS = {
    "lookup_unavailable_continues": [
        ("paper roll chahiye", [(T, "n_ask_tid")]),
        ("91379784", [(T, "n_api_lookup"), (T, "n_lookup_unavailable"),
                      (T, "n_options_unverified"), ("reply", "unable to verify"),
                      ("notrace", "n_tid_retry")]),
    ],
}


def run(c, name: str, turns) -> list[str]:
    session = f"wl_{name[:12]}_{uuid.uuid4().hex[:6]}"
    history: list[dict] = []
    failures: list[str] = []
    print(f"\n== {name} ({session})")
    for utterance, expectations in turns:
        r = c.post(f"/bots/{BOT_ID}/testing/chat",
                   json={"message": utterance, "sessionId": session, "messages": history})
        if r.status_code >= 300:
            failures.append(f"{utterance!r}: HTTP {r.status_code} {r.text[:200]}")
            break
        d = r.json()["data"]
        history += [{"role": "user", "content": utterance},
                    {"role": "assistant", "content": d.get("reply") or ""}]
        wf = d.get("workflow") or {}
        trace = [str(n) for n in (wf.get("nodeTrace") or [])]
        slots = {str(k): str(v).lower() for k, v in (wf.get("slots") or {}).items()}
        reply = (d.get("reply") or "").lower()
        print(f"  you> {utterance}")
        print(f"  bot> {(d.get('reply') or '')[:220]}")
        print(f"       route={d.get('route')} done={d.get('done')} trace={trace}")
        for kind, want in expectations:
            ok = {
                "reply": lambda: want.lower() in reply,
                "noreply": lambda: want.lower() not in reply,
                "trace": lambda: want in trace,
                "notrace": lambda: want not in trace,
                "slot": lambda: slots.get(want.split("=", 1)[0]) == want.split("=", 1)[1].lower(),
                "done": lambda: str(d.get("done")).lower() == want,
            }[kind]()
            if not ok:
                failures.append(f"{utterance!r}: expected {kind} {want!r}")
                print(f"       FAIL {kind} {want!r}")
    return failures


def main() -> int:
    c = client(BASE)
    suite = API_DOWN_SCENARIOS if API_DOWN else SCENARIOS
    total = passed = 0
    all_failures: dict[str, list[str]] = {}
    results: dict[str, dict] = {}
    for name, turns in suite.items():
        if FILTER and FILTER not in name:
            continue
        total += 1
        failures = run(c, name, turns)
        results[name] = {"passed": not failures, "turns": len(turns), "failures": failures}
        if failures:
            all_failures[name] = failures
        else:
            passed += 1
    if REPORT:
        import time
        pathlib.Path(REPORT).write_text(json.dumps({
            "base": BASE, "bot": BOT_ID, "finished_at": time.time(),
            "scenarios": results}, indent=1), encoding="utf-8")
        print(f"report written: {REPORT}")
    print(f"\n{passed}/{total} scenarios passed")
    for name, failures in all_failures.items():
        print(f"  {name}:")
        for f in failures:
            print(f"    - {f}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
