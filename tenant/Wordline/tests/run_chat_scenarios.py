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
    env/bin/python tenant/Wordline/tests/run_chat_scenarios.py --api-down
        # only the "unavailable" scenario; stop the merchant API first
    env/bin/python tenant/Wordline/tests/run_chat_scenarios.py --auth-fail
        # only the "unavailable" scenario; run the merchant API under a key the
        # platform does not hold first (WORDLINE_MERCHANT_API_KEY=other ./tenant/Wordline/run.sh)
        # so every lookup answers 401 → failure edge, never the not-found re-ask
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tenant.Wordline.setup.common import BOT_ID, DEFAULT_BASE, client  # noqa: E402

API_DOWN = "--api-down" in sys.argv or "--auth-fail" in sys.argv
FILTER = next((a for a in sys.argv[1:] if not a.startswith("--")), "")

T = "trace"
SCENARIOS = {
    "paper_roll_found": [
        ("Hello, I need paper roll for my machine", [(T, "n_ask_tid"), ("reply", "terminal")]),
        ("9 1 3 7 9 7 8 4", [(T, "n_api_lookup"), (T, "n_ask_merchant"),
                             ("reply", "agarwal public school"), ("reply", "axis bank"),
                             ("reply", "paytivo-7210-2g+wifi"), ("noreply", "sharma general store"),
                             ("slot", "merchant_found=true"), ("slot", "customer_verified=true"),
                             ("slot", "terminal_id=91379784")]),
        ("haan sahi hai", [(T, "n_options"), ("reply", "sms")]),
        ("SMS wala batao", [(T, "n_sms"), ("reply", "575758")]),
        ("no thanks", [(T, "n_end"), ("done", "true")]),
    ],
    "paper_roll_retry_then_found": [
        ("paper roll chahiye", [(T, "n_ask_tid")]),
        ("12345678", [(T, "n_api_lookup"), (T, "n_tid_retry"), ("notrace", "n_ask_merchant"),
                      ("reply", "could not find")]),
        ("7926 0453", [(T, "n_api_lookup_retry"), (T, "n_ask_merchant"),
                       ("reply", "kunal sweets"), ("reply", "kotak bank"),
                       ("slot", "spoken_terminal_id_retry=7926 0453")]),
    ],
    "paper_roll_still_not_found_continues": [
        ("mujhe paper roll chahiye", [(T, "n_ask_tid")]),
        ("12345678", [(T, "n_tid_retry")]),
        ("87654321", [(T, "n_lookup_unverified"), (T, "n_options_unverified"),
                      ("reply", "unable to locate"), ("noreply", "verified your"),
                      ("reply", "ivr")]),
        ("IVR wala batao", [(T, "n_ivr"), ("reply", "one eight six zero")]),
    ],
    "battery_found_contact_readback": [
        ("battery problem hai, jaldi drain ho rahi hai", [(T, "n_bat_tid")]),
        ("1231410", [(T, "n_bat_api_lookup"), (T, "n_bat_details"),
                     ("reply", "bhanu medical and general store"), ("reply", "idbi bank"),
                     ("reply", "9892858883"), ("slot", "terminal_id=1231410a")]),
        ("haan sahi hai", [(T, "n_bat_check"), ("reply", "switch off")]),
    ],
    "followup_fact_question_after_lookup": [
        ("paper roll request", [(T, "n_ask_tid")]),
        ("91379784", [(T, "n_ask_merchant"), ("reply", "agarwal public school")]),
        ("which bank is my terminal with?", [("reply", "axis")]),
    ],
}
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
    c = client(DEFAULT_BASE)
    suite = API_DOWN_SCENARIOS if API_DOWN else SCENARIOS
    total = passed = 0
    all_failures: dict[str, list[str]] = {}
    for name, turns in suite.items():
        if FILTER and FILTER not in name:
            continue
        total += 1
        failures = run(c, name, turns)
        if failures:
            all_failures[name] = failures
        else:
            passed += 1
    print(f"\n{passed}/{total} scenarios passed")
    for name, failures in all_failures.items():
        print(f"  {name}:")
        for f in failures:
            print(f"    - {f}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
