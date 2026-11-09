"""Build an unverified human-review shortlist from a local seven-day export.

No recording is classified as a genuine caller or speakerphone automatically.
Interruption-to-transcript associations are temporal candidates, not alignment.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

OUT = Path("docs/background_speech")


def instant(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def delta(event):
    measured, reference = event.get("speech_dbfs"), event.get("baseline_dbfs")
    return round(measured - reference, 1) if measured is not None and reference is not None else None


def shortlist(export):
    rows = []
    for call in export["calls"]:
        starts = instant(call["started_at"])
        confirmations = [e for e in call["events"] if e.get("kind") == "barge_in_confirmed"]
        matched = set()
        for event in call["events"]:
            if event.get("kind") != "caller_level_segment":
                continue
            at = instant(event["at"])
            nearby = [(index, c) for index, c in enumerate(confirmations)
                      if 0 <= (at - instant(c["at"])).total_seconds() <= 2.5]
            confirmation = min(nearby, key=lambda pair: abs((at - instant(pair[1]["at"])).total_seconds())) if nearby else None
            suspect = event.get("label") == "background_suspect"
            if not suspect and not (confirmation and event.get("during_bot_audio")):
                continue
            if confirmation:
                matched.add(confirmation[0])
            offset = max(0, (at - starts).total_seconds())
            duration = (event.get("segment_ms") or 0) / 1000
            reason = "background_suspect" if suspect else "overlap + nearby interruption"
            rows.append(dict(call_id=call["control_plane_id"], call_started_utc=starts.isoformat(),
                             event_utc=at.isoformat(), event_ist=(at + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                             offset_s=round(offset, 2), review_from_s=round(max(0, offset-duration-1), 2),
                             review_until_s=round(offset+1, 2), speech_dbfs=event.get("speech_dbfs"),
                             baseline_dbfs=event.get("baseline_dbfs"), delta_db=delta(event),
                             during_bot_audio=bool(event.get("during_bot_audio")),
                             reason=reason, nearby_interruption=bool(confirmation),
                             association="nearest preceding confirmation within 2.5 s; not word alignment" if confirmation else "no matched confirmation",
                             transcript=re.sub(r"\d{5,}", "[number]", event.get("text", ""))[:120],
                             review="unverified"))
        # Some calls predate level instrumentation. Keep their interruption
        # events reviewable; never fabricate a level or a transcript match.
        for index, event in enumerate(confirmations):
            if index in matched:
                continue
            at = instant(event["at"])
            offset = max(0, (at-starts).total_seconds())
            rows.append(dict(call_id=call["control_plane_id"], call_started_utc=starts.isoformat(),
                             event_utc=at.isoformat(), event_ist=(at+timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                             offset_s=round(offset, 2), review_from_s=round(max(0, offset-2), 2), review_until_s=round(offset+2, 2),
                             speech_dbfs=None, baseline_dbfs=None, delta_db=None,
                             during_bot_audio=True, reason="interruption; no matched level segment",
                             nearby_interruption=True, association="unmatched confirmation; transcript unavailable",
                             transcript="", review="unverified"))
    return sorted(rows, key=lambda r: r["event_utc"], reverse=True)


def choose_review(rows, limit):
    chosen, used = [], set()
    selected_indices = set()
    def add(index, row):
        if len(chosen) < limit and index not in selected_indices:
            chosen.append((index, row)); used.add(row["call_id"]); selected_indices.add(index)
    # Keep both quiet "अभी" events and the known incident easy to review,
    # plus missing-reference and normal-level overlap candidates.
    for index, row in enumerate(rows):
        if row["call_id"] == "cv_7dc1675ff773" and row["transcript"] in {"बाद में।", "अभी।"}:
            add(index, row)
    for predicate in (
        lambda r: r["reason"] == "overlap + nearby interruption" and r["delta_db"] is None,
        lambda r: r["reason"] == "overlap + nearby interruption" and r["delta_db"] is not None and r["delta_db"] > -8,
    ):
        taken = 0
        for index, row in enumerate(rows):
            if predicate(row) and row["call_id"] not in used:
                add(index, row); taken += 1
            if taken >= 2:
                break
    # Put instrumented incidents and different calls first; preserve coverage
    # of missing-reference starts and uninstrumented older calls in the CSV.
    priority = sorted(enumerate(rows), key=lambda pair: (
        pair[1]["delta_db"] is None,
        not (pair[1]["nearby_interruption"] and pair[1]["delta_db"] is not None and pair[1]["delta_db"] < -12),
        pair[1]["reason"] != "background_suspect", pair[0]))
    for index, row in priority:
        if row["call_id"] not in used:
            add(index, row)
        if len(chosen) == limit:
            break
    for index, row in priority:
        if len(chosen) >= limit:
            break
        add(index, row)
    return [dict(candidate_id=f"review_{index+1:03}", **row) for index, row in chosen]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("storage/tmp/background_replay/seven_day_calls.json"))
    parser.add_argument("--limit", type=int, default=24)
    args = parser.parse_args()
    export = json.loads(args.source.read_text())
    rows = shortlist(export)
    selected = choose_review(rows, args.limit)
    OUT.mkdir(exist_ok=True, parents=True)
    with (OUT / "review_candidates.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    payload = dict(since_utc=export["since_utc"], until_utc=export["until_utc"],
                   recordings_scanned=len(export["calls"]), total_candidates=len(rows),
                   shortlist=selected)
    (OUT / "review_shortlist.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2)+"\n")
    lines = ["# Seven-day listening shortlist", "",
             f"Read-only snapshot: {export['since_utc']} through {export['until_utc']}. Same bot and tenant only. {len(export['calls'])} recorded calls scanned; {len(rows)} candidate events in review_candidates.csv; {len(selected)} prioritized below.", "",
             "All new review labels are **unverified**. A suspect or a recorded interruption does not establish speaker identity, softness, or speakerphone use. Previously confirmed incident_later remains the only confirmed background item in the replay manifest. Timestamp ranges are padded event windows, not word-aligned boundaries. Δ is speech minus caller reference (negative means quieter); N/A means the reference/measurement was absent. IST is UTC+05:30. Nearby interruption matching is temporal only (preceding 2.5 s).", "",
             "## Listening review", "",
             "Open the conversation by its ID, play the indicated offset window, and label the audible speaker and scenario. Mark unclear audio as unclear. Supply labels in review_labels_template.csv; the importer validates them before adding any evidence.", "",
             "| Candidate | Call ID | Event time (IST) | Listen offsets (s) | Δ dB | Why shortlisted | Transcript snippet | Status |",
             "|---|---|---|---|---|---|---|---|"]
    for row in selected:
        snippet = row["transcript"].replace("|", "/").replace("\n", " ") or "N/A — no reliable matched transcript"
        lines.append(f"| {row['candidate_id']} | {row['call_id']} | {row['event_ist']} | {row['review_from_s']}–{row['review_until_s']} | {row['delta_db'] if row['delta_db'] is not None else 'N/A'} | {row['reason']} | {snippet} | unverified |")
    (OUT / "REVIEW_SHORTLIST.md").write_text("\n".join(lines)+"\n")
    template = OUT / "review_labels_template.csv"
    existing = list(csv.DictReader(template.open(newline=""))) if template.exists() else []
    if not existing or all(r.get("speaker_label", "unverified") in {"", "unverified"} for r in existing):
        with template.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["candidate_id", "call_id", "event_utc", "speaker_label", "scenario", "reviewer", "notes"])
            writer.writeheader()
            writer.writerows({"candidate_id": r["candidate_id"], "call_id": r["call_id"], "event_utc": r["event_utc"], "speaker_label": "unverified"} for r in selected)
    print(json.dumps({"recordings_scanned":len(export["calls"]), "candidate_events":len(rows), "shortlisted":len(selected)}))


if __name__ == "__main__":
    main()
