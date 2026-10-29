"""Jeeves (bot_44a50e8c4146): bring the bot's summaryFields up to date.

The full list = the workflow-slot fields of jeeves/setup/01_structured_summary.py
(``SUMMARY_FIELDS``, refreshed in place: labels, options and the Address
Changed / Updated Address fields) + the analyst fields of jeeves/summary_intents.py,
minus the fields that module retired. Any other field found on the bot is kept.
Since the 2026-10-28 import the bot runs workflow v22, which 01 does not
recognise, so this script is the one that writes the fields.

Dry run by default. Writes through the normal voice-settings API — the same
path the Studio uses — so nothing is applied to a database directly.

    env/bin/python jeeves/setup/03_summary_intents.py                 # dry run against local
    env/bin/python jeeves/setup/03_summary_intents.py --apply         # write (local by default)
    env/bin/python jeeves/setup/03_summary_intents.py --dump out.json # offline policy patch
    env/bin/python jeeves/setup/03_summary_intents.py --policy existing-policy.json --dump out.json

Env: ECHOSPHERE_API_BASE (default http://127.0.0.1:9001/api/v1),
     JEEVES_SETUP_EMAIL / JEEVES_SETUP_PASSWORD (a Jeeves tenant admin, or a super admin).
Idempotent: re-running after a successful apply reports nothing to change.
Does not run the workflow setup, publish a release, or restart anything.
"""

import argparse
import json
import os
from pathlib import Path
import runpy
import sys

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jeeves.summary_intents import BOT_ID, INTENT_SUMMARY_FIELDS, RETIRED_FIELDS, enhance_summary_fields  # noqa: E402


def workflow_fields() -> list[dict]:
    """The slot-only fields 01 owns, at its target stage (no API, no workflow check)."""
    return runpy.run_path(str(ROOT / "jeeves/setup/01_structured_summary.py"), run_name="jeeves_fields")["SUMMARY_FIELDS"]


def target_summary_fields(existing: list[dict]) -> list[dict]:
    """01's fields first (refreshed), then every other field the bot carries
    (analyst fields refreshed, retired ones dropped), then missing analyst fields."""
    owned = {f["name"]: f for f in workflow_fields()}
    rest = [f for f in existing if isinstance(f, dict) and f.get("name") not in owned]
    return enhance_summary_fields([*owned.values(), *rest])


def _client(base: str) -> requests.Session:
    email = os.environ.get("JEEVES_SETUP_EMAIL")
    password = os.environ.get("JEEVES_SETUP_PASSWORD")
    if not email or not password:
        sys.exit("set JEEVES_SETUP_EMAIL / JEEVES_SETUP_PASSWORD (a Jeeves tenant admin)")
    s = requests.Session()
    r = s.post(f"{base}/auth/login", json={"email": email, "password": password}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"login failed: {r.status_code} {r.text[:300]}")
    s.headers["Authorization"] = "Bearer " + r.json()["data"]["token"]
    s.base = base  # type: ignore[attr-defined]
    s.get, s.put = (lambda p, **k: requests.Session.get(s, base + p, timeout=60, **k),
                    lambda p, **k: requests.Session.put(s, base + p, timeout=60, **k))
    return s


def _check(r, what: str):
    if r.status_code >= 300:
        sys.exit(f"FAIL {what}: {r.status_code} {r.text[:500]}")
    print(f"ok   {what}")
    return r.json().get("data")


def _describe(before: list[dict], after: list[dict]) -> list[str]:
    old = {f.get("name"): f for f in before}
    lines = [f"- {name:<26} retired" for name in old if name in RETIRED_FIELDS]
    for spec in after:
        name = spec.get("name")
        if name not in old:
            lines.append(f"+ {name:<26} {spec.get('type'):<7} new")
        elif old[name] != spec:
            changed = sorted(k for k in set(old[name]) | set(spec) if old[name].get(k) != spec.get(k))
            lines.append(f"~ {name:<26} {spec.get('type'):<7} changed: {', '.join(changed)}")
        else:
            lines.append(f"= {name:<26} {spec.get('type'):<7} unchanged")
    return lines


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--base", default=os.environ.get("ECHOSPHERE_API_BASE", "http://127.0.0.1:9001/api/v1"))
    parser.add_argument("--policy", type=Path, help="offline: an existing goalPolicy JSON to enhance")
    parser.add_argument("--dump", type=Path, help="offline: write the enhanced policy patch here and exit")
    args = parser.parse_args(argv)

    if args.dump:
        policy = json.loads(args.policy.read_text()) if args.policy else {"summaryFields": []}
        policy["summaryFields"] = target_summary_fields(policy.get("summaryFields", policy.get("summary_fields", [])))
        policy.pop("summary_fields", None)
        args.dump.write_text(json.dumps({"botId": BOT_ID, "goalPolicy": policy}, ensure_ascii=False, indent=2) + "\n")
        print(f"Wrote local policy patch for {BOT_ID}: {args.dump}")
        return

    c = _client(args.base)
    settings = _check(c.get(f"/bots/{BOT_ID}/voice-settings"), "read voice settings")
    policy = dict(settings.get("goalPolicy") or {})
    before = [f for f in (policy.get("summaryFields") or []) if isinstance(f, dict)]
    after = target_summary_fields(before)
    if before == after:
        print(f"     summaryFields already up to date ({len(after)} fields, "
              f"{len(INTENT_SUMMARY_FIELDS)} analyst) — nothing to change")
        return
    print(f"     summaryFields: {len(before)} → {len(after)}")
    for line in _describe(before, after):
        print("    ", line)
    if not args.apply:
        print("dry run — re-run with --apply to write")
        return
    policy["summaryFields"] = after
    saved = _check(c.put(f"/bots/{BOT_ID}/voice-settings", json={"goalPolicy": policy}),
                   f"goalPolicy.summaryFields ({len(after)})")
    print(f"     now {len((saved or {}).get('goalPolicy', {}).get('summaryFields', []))} fields")


if __name__ == "__main__":
    main()
