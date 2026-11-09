"""Generate local policy-replay evidence; never apply a configuration.

Run: env/bin/python -m scripts.validate_background_replay
All five enforcement/transcript/start/fallback/playout boolean combinations
are crossed with five allowance candidates and two meaningful-word counts.
"""
from __future__ import annotations

import asyncio
import argparse
import csv
from itertools import product
import json
import logging
from pathlib import Path

from tests.background_replay import MANIFEST, load_clips, replay_segment

OUT = Path("docs/background_speech")


def configurations():
    for guard, transcript, start, failclosed, playout, allowance, words in product(
        (False, True), (False, True), (False, True), (False, True), (False, True),
        (0, 4, 6, 8, 12), (2, 3),
    ):
        name = f"G{int(guard)}T{int(transcript)}S{int(start)}F{int(failclosed)}P{int(playout)}A{allowance}N{words}"
        yield name, dict(enforce=guard, require_transcript=transcript,
                         start_protection=start, quiet_failopen_enabled=not failclosed,
                         allowance=allowance, meaningful_words=words), playout


async def evaluate(name, options, playout, clips):
    rows = []
    for clip in clips:
        held = 0
        results = []
        for segment in clip["segments"]:
            result = await replay_segment(segment, held_count=held, **options)
            held = result["held_count"]
            results.append(result)
        interrupted = any(r["interrupted"] for r in results)
        accepted = any(r["accepted"] for r in results)
        rows.append(dict(config=name, clip=clip["id"], label=clip["label"], review=clip["review"],
                         classification="/".join(sorted({r["classification"] for r in results})),
                         interrupted=interrupted, accepted=accepted,
                         background_blocked=(not interrupted and not accepted)
                         if clip["review"] in {"user_confirmed", "human_confirmed"} and clip["label"] == "background" else None,
                         genuine_allowed=(interrupted and accepted)
                         if clip["review"] in {"user_confirmed", "human_confirmed"} and clip["label"] == "caller"
                         and any(s["during_bot_audio"] for s in clip["segments"]) else None))
    confirmed = [r for r in rows if r["background_blocked"] is not None]
    genuine = [r for r in rows if r["genuine_allowed"] is not None]
    candidates = [r for r in rows if r["label"] in {"caller_candidate", "soft_caller_candidate", "soft_overlap_candidate", "caller_interruption_candidate"}]
    controls = {}
    for delta in (0, -8, -15, -20):
        # Acoustic-evidence perturbations, NOT newly recorded/verified callers.
        segment = dict(clips[0]["segments"][0], speech_dbfs=-28.3 + delta,
                       text="एक मिनट रुकिए")
        controls[str(delta)] = (await replay_segment(segment, **options))["interrupted"]
    early = dict(clips[0]["segments"][0], baseline_dbfs=None,
                 speech_dbfs=-28.3, text="एक मिनट रुकिए")
    early_allowed = (await replay_segment(early, **options))["interrupted"]
    # Third-person speech at caller level is deliberately indistinguishable
    # to this amplitude policy: report the residual failure explicitly.
    loud_background = dict(clips[0]["segments"][0], speech_dbfs=-28.3,
                           text="बाद में बात करना")
    loud_passes = (await replay_segment(loud_background, **options))["interrupted"]
    held = 0
    repeat = []
    for _ in range(5):
        quiet = dict(clips[0]["segments"][0], during_bot_audio=False)
        result = await replay_segment(quiet, held_count=held, **options)
        held = result["held_count"]
        repeat.append(result["accepted"])
    summary = dict(config=name, **options, transcript_playout_only=playout,
                   confirmed_background_blocked=sum(r["background_blocked"] for r in confirmed),
                   confirmed_background_total=len(confirmed),
                   confirmed_background_interruptions=sum(r["interrupted"] for r in confirmed),
                   genuine_allowed=sum(r["genuine_allowed"] for r in genuine) if genuine else None,
                   genuine_total=len(genuine),
                   false_positives=sum(not r["genuine_allowed"] for r in genuine) if genuine else None,
                   reason="No verified genuine interruption labels" if not genuine else "Explicitly human-reviewed interruption clips",
                   candidate_inputs_accepted=sum(r["accepted"] for r in candidates),
                   candidate_inputs_total=len(candidates),
                   synthetic_attenuation_interruptions=controls,
                   synthetic_early_caller_allowed=early_allowed,
                   synthetic_loud_background_passes=loud_passes,
                   repeated_quiet_suspect_acceptance=repeat)
    return summary, rows


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args()
    out = args.output
    logging.disable(logging.WARNING)
    clips = load_clips(args.manifest)
    if not clips:
        raise ValueError("No confirmed/review clips in manifest; do not fabricate a test set")
    summaries, details = [], []
    for name, options, playout in configurations():
        summary, rows = await evaluate(name, options, playout, clips)
        summaries.append(summary)
        details.extend(rows)
    out.mkdir(parents=True, exist_ok=True)
    (out / "validation.json").write_text(json.dumps(summaries, indent=2) + "\n")
    with (out / "validation_clips.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(details[0]))
        writer.writeheader(); writer.writerows(details)
    lines = [
        "# Flag combination validation", "",
        f"{len(summaries)} configurations × {len(clips)} real clips = {len(details)} clip-policy evaluations.",
        "Recorded-event/acoustic replay, not end-to-end audio/STT or a live test.", "",
        "G = guard enforcement; T = require transcript; S = startup protection; F = disable quiet fail-open; P = conservative stored transcript; A = bot allowance dB; N = meaningful words. Base margin is fixed at 12 dB; quiet hold limit is 2 (0/1/2/5 boundaries tested separately). All 32 boolean combinations are included, with A in {0,4,6,8,12} and N in {2,3}. Continuous numeric ranges are sampled, not exhausted.", "",
        f"Confirmed background clips: {summaries[0]['confirmed_background_total']}; verified genuine overlap clips: {summaries[0]['genuine_total']}. Genuine allowed and false positives are N/A without positive speaker labels. False positives mean verified genuine interruptions incorrectly held. Candidate input acceptance is separate, provisional, and NOT genuine interruption recall. P changes persistence only; delivery/cancellation tests are separate from this interruption table.", "",
        "Synthetic columns perturb logged acoustic evidence with a three-word transcript: they are neither audio attenuation replay nor speakerphone verification. The early-caller control has no reference. Repeated quiet-suspect patterns and the loud-third-person failure are in validation.json.", "",
        "| Configuration | Background blocked | Background interruptions | Genuine allowed | False positives | Candidate inputs accepted | Synthetic caller interruptions: 0 / −8 / −15 / −20 dB | Early caller |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        controls = " / ".join("allow" if value else "hold" for value in s["synthetic_attenuation_interruptions"].values())
        positives = "N/A" if s["genuine_allowed"] is None else f"{s['genuine_allowed']}/{s['genuine_total']}"
        false_positives = "N/A" if s["false_positives"] is None else str(s["false_positives"])
        lines.append(f"| {s['config']} | {s['confirmed_background_blocked']}/{s['confirmed_background_total']} | {s['confirmed_background_interruptions']} | {positives} | {false_positives} | {s['candidate_inputs_accepted']}/{s['candidate_inputs_total']} | {controls} | {'allow' if s['synthetic_early_caller_allowed'] else 'hold'} |")
    (out / "VALIDATION.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"configurations": len(summaries), "real_clip_evaluations": len(details),
                      "confirmed_background_clips": summaries[0]["confirmed_background_total"],
                      "verified_genuine_clips": summaries[0]["genuine_total"],
                      "output": str(out / "VALIDATION.md")}))


if __name__ == "__main__":
    asyncio.run(main())
