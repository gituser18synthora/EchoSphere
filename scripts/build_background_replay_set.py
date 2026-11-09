"""Extract a local, reviewable replay set from read-only call exports.

No server config, services or code is modified. Audio stays in ignored local
storage; the checked-in manifest contains only the selected event evidence.
Unverified speaker identity/device labels must not become ground truth.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import wave

ROOT = Path(__file__).resolve().parents[1]
SELECTION = [
    ("incident_later", "cv_7dc1675ff773", 2, "background", "user_confirmed"),
    ("incident_now_1", "cv_7dc1675ff773", 5, "background_candidate", "unverified"),
    ("incident_now_2", "cv_7dc1675ff773", 6, "background_candidate", "unverified"),
    ("incident_identity", "cv_7dc1675ff773", 1, "caller_candidate", "unverified"),
    ("incident_yes", "cv_7dc1675ff773", 3, "caller_candidate", "unverified"),
    ("incident_tomorrow", "cv_7dc1675ff773", 4, "caller_candidate", "unverified"),
    ("incident_soft_ok", "cv_7dc1675ff773", 7, "soft_caller_candidate", "unverified"),
    ("recent_normal_1", "cv_948773909cf9", 2, "caller_candidate", "unverified"),
    ("recent_normal_2", "cv_948773909cf9", 3, "caller_candidate", "unverified"),
    ("start_incident", "cv_7dc1675ff773", 0, "call_start_unknown", "unverified"),
    ("start_quiet", "cv_434cd7562fba", 0, "call_start_unknown", "unverified"),
    ("start_hello", "cv_c191017d2061", 1, "call_start_unknown", "unverified"),
    ("start_latest", "cv_10814a1fad98", 0, "call_start_unknown", "unverified"),
    ("soft_recent_1", "cv_948773909cf9", 5, "soft_caller_candidate", "unverified"),
    ("soft_recent_2", "cv_434cd7562fba", 8, "soft_caller_candidate", "unverified"),
    ("soft_overlap", "cv_c191017d2061", 3, "soft_overlap_candidate", "unverified"),
    ("overlap_words", "cv_7094a4ab5cc3", 5, "caller_interruption_candidate", "unverified"),
    ("overlap_no", "cv_434cd7562fba", 5, "caller_interruption_candidate", "unverified"),
    ("continuous_review_1", "cv_434cd7562fba", [0, 1, 2, 3, 4, 5], "continuous_speech_review", "unverified"),
    ("continuous_review_2", "cv_7dc1675ff773", [4, 5, 6], "continuous_speech_review", "unverified"),
]


def instant(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "storage/tmp/background_replay")
    parser.add_argument("--fetch-readonly", action="store_true")
    parser.add_argument("--calls", type=Path, help="Optional local export (list or seven-day envelope)")
    parser.add_argument("--selection", type=Path, help="Validated human-reviewed selections JSON")
    parser.add_argument("--manifest", type=Path, default=ROOT / "tests/fixtures/background_replay_manifest.json")
    args = parser.parse_args()
    exported = json.loads((args.calls or args.source / "calls.json").read_text())
    calls = {d["control_plane_id"]: d for d in (exported["calls"] if isinstance(exported, dict) else exported)}
    selections = json.loads(args.selection.read_text()) if args.selection else [
        dict(id=i, call_id=c, indices=n, label=l, review=r) for i, c, n, l, r in SELECTION
    ]
    if not selections:
        raise ValueError("No confirmed selections; leave existing manifests unchanged")
    (args.source / "raw").mkdir(parents=True, exist_ok=True)
    (args.source / "clips").mkdir(exist_ok=True)
    clips = []
    for selected in selections:
        clip_id, call_id, indices, label, review = (selected[k] for k in ("id", "call_id", "indices", "label", "review"))
        if not re.fullmatch(r"[A-Za-z0-9_-]+", clip_id):
            raise ValueError("Unsafe clip ID")
        call = calls[call_id]
        relative = call["recording"]["path"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+\.wav", relative):
            raise ValueError("Unsafe recording path")
        raw = args.source / "raw" / f"{call_id}.wav"
        if not raw.exists() and args.fetch_readonly:
            subprocess.run([
                "scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                f"echosphere-live:/var/www/html/EchoSphere/storage/recordings/{relative}", str(raw),
            ], check=True)
        events = [e for e in call["events"] if e.get("kind") == "caller_level_segment"]
        chosen = [events[i] for i in (indices if isinstance(indices, list) else [indices])]
        start_at = instant(call["started_at"])
        lo = max(0, min(instant(e["at"]) - start_at - (e.get("segment_ms") or 1000) / 1000 - .5 for e in chosen))
        hi = max(instant(e["at"]) - start_at + .3 for e in chosen)
        with wave.open(str(raw), "rb") as wav:
            rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
            hi = min(hi, wav.getnframes() / rate)
            wav.setpos(int(lo * rate))
            pcm = wav.readframes(int((hi - lo) * rate))
        out = args.source / "clips" / f"{clip_id}.wav"
        with wave.open(str(out), "wb") as wav:
            wav.setparams((channels, width, rate, 0, "NONE", "not compressed"))
            wav.writeframes(pcm)
        evidence = []
        for e in chosen:
            offset = instant(e["at"]) - start_at
            confirmations = [b for b in call["events"] if b.get("kind") == "barge_in_confirmed" and 0 <= instant(e["at"]) - instant(b["at"]) < 1.5]
            evidence.append({
                "text": e["text"], "speech_dbfs": e["speech_dbfs"], "baseline_dbfs": e.get("baseline_dbfs"),
                "during_bot_audio": e["during_bot_audio"], "vad_seconds": e.get("vad_speech_s") or 0,
                "segment_ms": e.get("segment_ms"), "offset_s": round(offset, 3),
                "recorded_classification": e["label"], "recorded_action": e["action"],
                "recorded_interrupted": bool(confirmations),
                "observed_sustained_s": max([b.get("sustained_s") or 0 for b in confirmations] or [e.get("vad_speech_s") or 0]),
            })
        clips.append({
            "id": clip_id, "call_id": call_id, "label": label, "review": review,
            "device": selected.get("device", "unknown"),
            "speakerphone_verified": selected.get("speakerphone_verified", False),
            **{k: selected[k] for k in ("scenario", "reviewer", "notes") if k in selected},
            "file": f"clips/{clip_id}.wav", "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
            "start_s": round(lo, 3), "end_s": round(hi, 3), "sample_rate": rate,
            "alignment": "Event timestamps with padding; not word-aligned ground truth.", "segments": evidence,
        })
    manifest = {
        "schema_version": 1, "source": "Local extraction from read-only demo recording export",
        "audio_root": str(args.source.relative_to(ROOT)) if args.source.is_relative_to(ROOT) else str(args.source), "clips": clips,
        "limitations": [
            "Only explicitly confirmed reviews establish speaker ground truth.",
            "Unverified labels are review candidates, not verified caller/background attribution.",
            "Device labels require explicit owner/reviewer confirmation; unknown otherwise.",
            "Continuous windows require review; continuous background conversation is not established.",
            "Recording timestamps are approximate; event replay is not a full VAD/STT/audio end-to-end test.",
        ],
    }
    target = args.manifest
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"Extracted {len(clips)} clips; manifest: {target}")


if __name__ == "__main__":
    main()
