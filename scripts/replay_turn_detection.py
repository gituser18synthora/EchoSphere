#!/usr/bin/env python
"""Offline replay of the caller-audio front end for turn-detection tuning.

Feeds a recorded caller channel through the SAME components a live call
uses — ``CallerAudioGate`` → ``ConfidenceTrackingSileroVADAnalyzer`` (pipecat
state machine) → the pause-window arithmetic of the turn controller — for a
grid of candidate parameters, and prints what each candidate would have
decided. Use it BEFORE changing tenant Turn Detection values.

    env/bin/python scripts/replay_turn_detection.py call.wav --transport telephony \\
        --grid "confidence=0.58,0.5,0.45;stop_secs=0.3,0.45" \\
        [--events call.json] [--pre-gate]

``--pre-gate`` says the WAV is raw line audio (e.g. an ECHOSPHERE_FS_AUDIO_DEBUG_DIR
capture); without it the caller channel of a call recording is assumed, which
is POST-gate (zeros where the gate was closed), so gate parameters can only
narrow further. ``--events`` is a conversation_transcripts document (Mongo
export) — its turn_timing spans and user turns are compared with each
candidate's VAD segments.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipecat.audio.vad.vad_analyzer import VADParams, VADState  # noqa: E402
from pipecat.frames.frames import InputAudioRawFrame  # noqa: E402

from shared.turn_detection import (  # noqa: E402
    NOISE_GATE_DEFAULTS,
    TURN_DETECTION_DEFAULTS,
)
from voice_runtime.audio_gate import CallerAudioGate  # noqa: E402
from voice_runtime.vad_confidence import ConfidenceTrackingSileroVADAnalyzer  # noqa: E402

FRAME_MS = 20


def load_caller(path: str, channel: int) -> tuple[np.ndarray, int]:
    with wave.open(path) as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if ch > 1:
        pcm = pcm.reshape(-1, ch)[:, channel]
    return pcm.copy(), sr


def parse_grid(spec: str) -> list[dict]:
    """"a=1,2;b=x,y" → every combination as {a:..., b:...}."""
    axes: list[list[tuple[str, float]]] = []
    for part in filter(None, (p.strip() for p in spec.split(";"))):
        key, _, values = part.partition("=")
        axes.append([(key.strip(), float(v)) for v in values.split(",") if v.strip()])
    combos: list[dict] = [{}]
    for axis in axes:
        combos = [{**c, k: v} for c in combos for k, v in axis]
    return combos


async def run_gate(pcm: np.ndarray, sr: int, gate_conf: dict) -> np.ndarray:
    """Post-gate audio for the candidate gate settings."""
    gate = CallerAudioGate(
        noise_margin_db=gate_conf["noise_margin_db"],
        min_speech_ms=gate_conf["min_speech_ms"],
        echo_min_speech_ms=gate_conf["echo_min_speech_ms"],
        hangover_ms=gate_conf["hangover_ms"],
        preroll_ms=gate_conf["preroll_ms"],
        echo_margin_db=gate_conf["echo_margin_db"],
        echo_tail_ms=gate_conf["echo_tail_ms"],
        min_threshold_dbfs=gate_conf["min_threshold_dbfs"],
    )
    out: list[bytes] = []

    async def _push(frame, direction=None):
        if isinstance(frame, InputAudioRawFrame):
            out.append(frame.audio)

    gate.push_frame = _push
    gate._sample_rate = sr
    n = sr * FRAME_MS // 1000
    for i in range(0, len(pcm) - n + 1, n):
        frame = InputAudioRawFrame(audio=pcm[i:i + n].tobytes(), sample_rate=sr, num_channels=1)
        await gate._process_audio(frame, None)
    return np.frombuffer(b"".join(out), dtype="<i2")


def run_vad(pcm: np.ndarray, sr: int, params: VADParams) -> tuple[list[tuple[float, float]], list[float]]:
    analyzer = ConfidenceTrackingSileroVADAnalyzer(sample_rate=sr, params=params)
    analyzer.set_sample_rate(sr)
    n = sr * FRAME_MS // 1000
    segments: list[tuple[float, float]] = []
    start: float | None = None
    prev = VADState.QUIET
    for i in range(0, len(pcm) - n + 1, n):
        state = analyzer._run_analyzer(pcm[i:i + n].tobytes())
        t = i / sr
        if state == VADState.SPEAKING and prev in (VADState.QUIET, VADState.STARTING):
            start = t - params.start_secs
        elif state == VADState.QUIET and prev in (VADState.SPEAKING, VADState.STOPPING) and start is not None:
            segments.append((max(0.0, start), t - params.stop_secs))
            start = None
        prev = state
    if start is not None:
        segments.append((start, len(pcm) / sr))
    return segments, [c for _, c in analyzer._history]


def recorded_spans(events_path: str | None) -> tuple[list[tuple[float, float]], list[tuple[float, str]]]:
    if not events_path:
        return [], []
    doc = json.load(open(events_path))
    ev = doc.get("events") or []

    def ts(a):
        return dt.datetime.fromisoformat(a.replace("Z", "+00:00")).timestamp()

    t0 = next((ts(e["at"]) for e in ev if e.get("kind") == "call_started"), None)
    if t0 is None:
        return [], []
    spans = [
        (e["user_speech_start_at"] / 1000 - t0, e["user_speech_end_at"] / 1000 - t0)
        for e in ev if e.get("kind") == "turn_timing" and e.get("user_speech_start_at")
    ]
    turns = [(t["ts"] - t0, t["text"]) for t in doc.get("turns") or [] if t.get("role") == "user"]
    return spans, turns


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav")
    ap.add_argument("--channel", type=int, default=0, help="caller channel in a stereo recording")
    ap.add_argument("--transport", choices=("telephony", "browser"), default="telephony")
    ap.add_argument("--grid", default="confidence=0.58,0.5;stop_secs=0.3,0.45")
    ap.add_argument("--events", help="conversation_transcripts JSON export to compare against")
    ap.add_argument("--pre-gate", action="store_true", help="the WAV is raw line audio (run the gate)")
    ap.add_argument("--tolerance", type=float, default=1.0,
                    help="seconds of slack when matching recorded VAD turns (call recordings "
                         "drift 2-4 s over a long telephony call — see AlignedStereoRecorder)")
    args = ap.parse_args()

    pcm, sr = load_caller(args.wav, args.channel)
    if sr not in (8000, 16000):
        sys.exit(f"Silero needs 8 or 16 kHz audio, got {sr}")
    base_turn = dict(TURN_DETECTION_DEFAULTS[args.transport])
    base_gate = dict(NOISE_GATE_DEFAULTS[args.transport])
    spans, turns = recorded_spans(args.events)
    print(f"{args.wav}: {len(pcm) / sr:.1f} s @ {sr} Hz; recorded VAD turns={len(spans)} user turns={len(turns)}")
    print(f"{'candidate':48s} {'segs':>4s} {'speech_s':>8s} {'matched':>7s} {'unmatched':>9s} {'p25conf':>7s} {'pause_budget':>12s}")
    for combo in parse_grid(args.grid):
        turn = {**base_turn, **{k: v for k, v in combo.items() if k in base_turn}}
        gate = {**base_gate, **{k: v for k, v in combo.items() if k in base_gate}}
        audio = asyncio.run(run_gate(pcm, sr, gate)) if args.pre_gate else pcm
        params = VADParams(confidence=turn["confidence"], start_secs=turn["start_secs"],
                           stop_secs=turn["stop_secs"], min_volume=turn["min_volume"])
        segments, confs = run_vad(audio, sr, params)
        speech = sum(e - s for s, e in segments)
        tol = args.tolerance
        matched = sum(1 for s0, e0 in spans if any(s <= e0 + tol and e >= s0 - tol for s, e in segments))
        unmatched = sum(1 for s, e in segments if not any(s <= e0 + tol and e >= s0 - tol for s0, e0 in spans))
        p25 = np.percentile([c for c in confs if c > 0.1], 25) if any(c > 0.1 for c in confs) else float("nan")
        budget = turn["stop_secs"] + max(0.2, turn["user_speech_timeout"] - turn["stop_secs"])
        label = ",".join(f"{k}={v:g}" for k, v in combo.items()) or "defaults"
        print(f"{label:48s} {len(segments):4d} {speech:8.1f} {matched:7d} {unmatched:9d} {p25:7.2f} {budget:11.2f}s")


if __name__ == "__main__":
    main()
