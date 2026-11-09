"""Recorded-evidence replay through the real level/barge-in policy.

Uses recorded STT and acoustic measurements, not a synthetic claim to have
re-run STT or verified the speaker/device. Audio integrity is checked separately.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pipecat.frames.frames import BotStartedSpeakingFrame, Frame, TranscriptionFrame, VADUserStartedSpeakingFrame
from voice_runtime.barge_in import WordConfirmedBargeInStrategy
from voice_runtime.caller_level import CallerLevelBaseline

MANIFEST = Path(__file__).parent / "fixtures/background_replay_manifest.json"


def load_clips(manifest=MANIFEST):
    return json.loads(Path(manifest).read_text())["clips"]


async def replay_segment(segment, *, enforce=False, allowance=12, **options):
    start_protection = options.get("start_protection", False)
    quiet_failopen = options.pop("quiet_failopen_enabled", True)
    quiet_max_holds = options.pop("quiet_max_holds", 2)
    held_count = options.pop("held_count", 0)
    level = CallerLevelBaseline(
        margin_db=12, bot_audio_allowance_db=allowance, min_segments=3, enforce=enforce,
        start_protection=start_protection, quiet_failopen_enabled=quiet_failopen,
        quiet_max_holds=quiet_max_holds,
    )
    if segment["baseline_dbfs"] is not None:
        level.observe_trusted(segment["baseline_dbfs"])
    during = segment["during_bot_audio"]
    verdict = level.classify(segment["speech_dbfs"], during_bot_audio=during)
    gate = SimpleNamespace(live_speech_ms=segment.get("segment_ms") or 1000,
                           speech_snapshot=lambda: {"speech_dbfs": segment["speech_dbfs"]})
    strategy = WordConfirmedBargeInStrategy(
        min_words=options.pop("min_words", 2), vad_fallback_secs=.8,
        speech_classifier=lambda: level.classify_live(gate), enforce_background=enforce,
        caller_reference_ready=lambda: level.established,
        **options,
    )
    fired = AsyncMock()
    strategy.trigger_user_turn_started = fired
    if during:
        clock = [10.0]
        with patch("voice_runtime.barge_in.time.monotonic", side_effect=lambda: clock[0]):
            await strategy.process_frame(BotStartedSpeakingFrame())
            await strategy.process_frame(VADUserStartedSpeakingFrame())
            clock[0] += segment["observed_sustained_s"]
            await strategy.process_frame(Frame())
            await strategy.process_frame(TranscriptionFrame(text=segment["text"], user_id="replay", timestamp="t"))
    startup_held = start_protection and not level.established and during
    suspect_held = enforce and verdict.suspect and (during or not level.quiet_should_fail_open(held_count))
    accepted = not (startup_held or suspect_held)
    return {"classification": verdict.label, "interrupted": bool(fired.await_count), "accepted": accepted,
            "held_count": held_count + 1 if suspect_held and not during else 0}


async def baseline_table(manifest=MANIFEST):
    rows = []
    for clip in load_clips(manifest):
        results = [await replay_segment(segment) for segment in clip["segments"]]
        rows.append({"clip": clip["id"], "label": clip["label"], "review": clip["review"],
                     "classification": "/".join(sorted({r["classification"] for r in results})),
                     "interrupted": any(r["interrupted"] for r in results),
                     "recorded_interrupted": any(s["recorded_interrupted"] for s in clip["segments"]),
                     "accepted": any(r["accepted"] for r in results)})
    return rows


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(baseline_table(args.manifest)), ensure_ascii=False, indent=2))
