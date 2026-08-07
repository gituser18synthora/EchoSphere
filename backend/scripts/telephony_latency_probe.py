"""Telephony latency probe — caller-perceived response time, per turn.

Drives REAL calls through the Vaani gateway (webhook → WebSocket → 20 ms
media cadence) and measures, from the dialer's side of the wire, the time
between the caller's last speech byte leaving the uplink and the first bot
audio byte arriving back — the latency a person on the phone actually feels.
Worker-side ``turn_timing`` records (same session id) attribute the inside
of that number to pipeline stages.

Usage:
    env/bin/python backend/scripts/telephony_latency_probe.py \
        [--turns "text1" "text2" ...] [--label NAME] [--barge-in] [--json OUT]

Each --turns entry is spoken by the simulated caller (Sarvam TTS via
SARVAM_API_KEY, mono 8 kHz PCM16) after the bot's previous reply goes quiet.
Secrets come from the environment / .env and are never printed.
"""

import argparse
import asyncio
import base64
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
import shared.config  # noqa: F401,E402  (side effect: load .env)

import websockets  # noqa: E402

_SIM_PATH = _REPO / "backend" / "scripts" / "vaani_dialer_sim.py"
_spec = importlib.util.spec_from_file_location("vaani_dialer_sim", _SIM_PATH)
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

QUIET_GAP = 0.7          # bot counted as done speaking after this much silence
REPLY_TIMEOUT = 20.0     # give up waiting for a reply after this long
BYTES_PER_SECOND = 16000  # 8 kHz PCM16 mono


class ProbeCall(sim.DialerCall):
    """DialerCall that timestamps inbound bot media for latency measurement."""

    def __init__(self, ws, session_id: str):
        super().__init__(ws, session_id)
        self.last_media_at: float | None = None
        self.media_bytes_total = 0

    async def pump(self, until, timeout: float) -> bool:
        """Consume events, stamping media arrivals; returns True when `until`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                async with asyncio.timeout(max(0.05, deadline - time.monotonic())):
                    raw = await self.ws.recv()
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                return False
            event = json.loads(raw)
            if event.get("event") == "media":
                self.last_media_at = time.monotonic()
                self.media_bytes_total += len(
                    base64.b64decode(event["media"]["payload"])
                )
            if until(event):
                return True
        return False

    async def wait_quiet(self, *, timeout: float = 30.0) -> None:
        """Wait until bot audio has been silent for QUIET_GAP seconds."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            last = self.last_media_at
            if last is not None and time.monotonic() - last >= QUIET_GAP:
                return
            got = await self.pump(lambda e: False, timeout=0.1)
            if not got:
                last = self.last_media_at
                if last is None:
                    continue
                if time.monotonic() - last >= QUIET_GAP:
                    return

    async def speak_and_measure(self, pcm: bytes, label: str) -> dict:
        """Queue caller speech; measure last-byte-out → first-reply-byte-in."""
        speech_seconds = len(pcm) / BYTES_PER_SECOND
        baseline_bytes = self.media_bytes_total
        self.speak_queue.put_nowait(pcm)
        # The uplink drains at exactly real time (20 ms frames).
        await asyncio.sleep(speech_seconds)
        speech_end = time.monotonic()

        first_reply_at: float | None = None

        def until(event) -> bool:
            nonlocal first_reply_at
            if (
                event.get("event") == "media"
                and self.media_bytes_total > baseline_bytes
                and first_reply_at is None
            ):
                first_reply_at = self.last_media_at
                return True
            return False

        await self.pump(until, timeout=REPLY_TIMEOUT)
        latency_ms = (
            round((first_reply_at - speech_end) * 1000, 1)
            if first_reply_at is not None else None
        )
        return {
            "turn": label,
            "speech_seconds": round(speech_seconds, 2),
            "caller_latency_ms": latency_ms,
        }


async def run_probe(args) -> dict:
    url, session_id = await sim.open_call(args)
    results: list[dict] = []
    async with websockets.connect(sim.ws_local(args, url)) as ws:
        call = ProbeCall(ws, session_id)
        await call.handshake()
        # Greeting: wait for it to fully play out before the first turn.
        await call.pump(lambda e: call.last_media_at is not None, timeout=15.0)
        await call.wait_quiet()

        for i, text in enumerate(args.turns, 1):
            pcm, source = await sim.caller_audio(args, text)
            sim.log(f"turn {i}: speaking {text!r} ({source})", session_id)
            measured = await call.speak_and_measure(pcm, f"{i}:{text[:24]}")
            sim.log(f"turn {i}: caller-felt latency = "
                    f"{measured['caller_latency_ms']} ms", session_id)
            results.append(measured)
            if args.barge_in and i == 1:
                # Interrupt the reply mid-playback with the next utterance.
                await asyncio.sleep(0.6)
                continue
            await call.wait_quiet()

        await call.send_stop()
        await call.pump(lambda e: e.get("event") == "stop", timeout=6.0)
        call.close_uplink()
    return {"session_id": session_id, "label": args.label, "turns": results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=sim.DEFAULT_BASE)
    parser.add_argument("--to", default=sim.DEFAULT_TO)
    parser.add_argument("--bot", default=None)
    parser.add_argument("--no-rewrite", action="store_true")
    parser.add_argument("--wav", default=None)
    parser.add_argument("--raw", default=None)
    parser.add_argument("--label", default="probe")
    parser.add_argument("--barge-in", action="store_true")
    parser.add_argument("--json", default=None, help="append JSON result to file")
    parser.add_argument("--turns", nargs="+", required=True)
    args = parser.parse_args()

    outcome = asyncio.run(run_probe(args))
    print(json.dumps(outcome, ensure_ascii=False))
    if args.json:
        with open(args.json, "a") as fh:
            fh.write(json.dumps(outcome, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
