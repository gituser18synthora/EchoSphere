"""Prepare a reviewable local goal-policy patch; never contact a server.

Usage: env/bin/python jeeves/setup/03_summary_intents.py --dump /tmp/summary-policy.json
Optionally --policy existing-policy.json to preserve custom fields/settings.
Apply only to bot_44a50e8c4146; this does not publish or change a workflow.
"""

import argparse
import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jeeves.summary_intents import BOT_ID, enhance_summary_fields


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--dump", type=Path, required=True)
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text()) if args.policy else {
        "summaryFields": runpy.run_path(str(ROOT / "jeeves/setup/01_structured_summary.py"))["SUMMARY_FIELDS"]
    }
    policy["summaryFields"] = enhance_summary_fields(
        policy.get("summaryFields", policy.get("summary_fields", []))
    )
    policy.pop("summary_fields", None)
    args.dump.write_text(json.dumps({"botId": BOT_ID, "goalPolicy": policy}, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote local policy patch for {BOT_ID}: {args.dump}")


if __name__ == "__main__":
    main()
