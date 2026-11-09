"""Validate explicit human labels and prepare clip selections locally.

Run with --labels <csv> --calls <export.json> --output <selection.json>.
Unverified/unclear rows never become confirmed ground truth. A call scenario
alone cannot label every segment; each confirmed row needs an exact event.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from scripts.shortlist_background_calls import instant

SCENARIOS = {"normal", "soft", "speakerphone", "continuous_background", "call_start", "unknown"}


def selections(labels, calls):
    indexed = {call["control_plane_id"]: call for call in calls}
    result, seen = [], set()
    for row in labels:
        label = row.get("speaker_label", "unverified").strip()
        if label in {"unverified", "unclear", ""}:
            continue
        if label not in {"caller", "background"}:
            raise ValueError(f"Invalid speaker label: {label}")
        scenario = row.get("scenario", "unknown").strip() or "unknown"
        if scenario not in SCENARIOS or not row.get("reviewer", "").strip():
            raise ValueError("A confirmed row needs a valid scenario and reviewer")
        call_id = row["call_id"]
        at = instant(row["event_utc"])
        key = (call_id, at)
        if key in seen:
            raise ValueError("Duplicate/conflicting labels for the same event")
        seen.add(key)
        events = [e for e in indexed[call_id]["events"] if e.get("kind") == "caller_level_segment"]
        matches = [i for i, event in enumerate(events) if instant(event["at"]) == at]
        if len(matches) != 1:
            raise ValueError(f"No unique measured segment for {call_id} at {at}; review without measurement stays outside the acoustic replay")
        # Scenario names are descriptive metadata. Speaker attribution is an
        # independent explicit human label; never infer it from a level score.
        result.append(dict(id=f"reviewed_{len(result)+1:03}", call_id=call_id,
                           indices=matches[0], label=label, review="human_confirmed",
                           scenario=scenario, reviewer=row["reviewer"].strip(),
                           notes=row.get("notes", ""),
                           device="speakerphone" if scenario == "speakerphone" and label == "caller" else "unknown",
                           speakerphone_verified=scenario == "speakerphone" and label == "caller"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--calls", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    exported = json.loads(args.calls.read_text())
    calls = exported["calls"] if isinstance(exported, dict) else exported
    with args.labels.open(newline="") as stream:
        selected = selections(list(csv.DictReader(stream)), calls)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(selected, ensure_ascii=False, indent=2)+"\n")
    print(f"Validated {len(selected)} explicit confirmed segment labels; no audio fetched or settings applied")


if __name__ == "__main__":
    main()
