"""Vaani dialer simulator — test the EchoSphere telephony gateway end to end
without the real dialer.

Speaks the exact dialer contract from docs/VAANI_INTEGRATION.md: signed
webhook → per-call WebSocket → connected/start/media/stop, and logs every
control event with timestamps and the session id.

Usage:
    env/bin/python backend/scripts/vaani_dialer_sim.py webhook [--bot BOT_ID]
    env/bin/python backend/scripts/vaani_dialer_sim.py full-call [--say TEXT | --wav FILE | --raw FILE]
    env/bin/python backend/scripts/vaani_dialer_sim.py invalid-signature
    env/bin/python backend/scripts/vaani_dialer_sim.py protocol-events
    env/bin/python backend/scripts/vaani_dialer_sim.py barge-in
    env/bin/python backend/scripts/vaani_dialer_sim.py transfer
    env/bin/python backend/scripts/vaani_dialer_sim.py negative
    env/bin/python backend/scripts/vaani_dialer_sim.py abrupt-disconnect

Secrets: the webhook secret is read from the TELEPHONY_WEBHOOK_SECRET
environment variable (loaded from the project .env when run inside the repo).
Nothing is hardcoded and the secret is never printed.

Caller audio: --wav (mono 16-bit WAV; resampled to 8 kHz if needed), --raw
(headerless 8 kHz PCM16 little-endian mono), or --say TEXT rendered via Sarvam
TTS (needs SARVAM_API_KEY), falling back to synthetic vowel audio that still
trips voice-activity detection.
"""

import argparse
import asyncio
import base64
import hashlib
import hmac
import io
import json
import math
import os
import struct
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

try:  # inside the repo: load .env for TELEPHONY_WEBHOOK_SECRET / SARVAM_API_KEY
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import shared.config  # noqa: F401  (side effect: load_dotenv)
except Exception:  # noqa: BLE001 — standalone mode: rely on the environment
    pass

import httpx
import websockets

DEFAULT_BASE = os.environ.get("VAANI_SIM_BASE", "http://127.0.0.1:9011")
DEFAULT_TO = os.environ.get("VAANI_SIM_TO", "+91 80 4522 1010")

FAILURES: list[str] = []


def log(msg: str, session: str = "-") -> None:
    stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{stamp}] [{session}] {msg}")


def check(name: str, ok: bool, detail: str = "", session: str = "-") -> bool:
    log(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), session)
    if not ok:
        FAILURES.append(name)
    return ok


def secret() -> str:
    value = os.environ.get("TELEPHONY_WEBHOOK_SECRET", "")
    if not value:
        sys.exit("TELEPHONY_WEBHOOK_SECRET is not set — export it or run inside the repo")
    return value


def sign(body: bytes, *, ts: int | None = None, key: str | None = None) -> dict:
    ts = ts if ts is not None else int(time.time())
    sig = hmac.new((key or secret()).encode(), f"{ts}.".encode() + body,
                   hashlib.sha256).hexdigest()
    return {"X-Webhook-Signature": sig, "X-Webhook-Timestamp": str(ts),
            "Content-Type": "application/json"}


def call_body(args, **extra) -> bytes:
    payload = {
        "To": args.to, "From": "+919812345678",
        "callId": f"SIM-{os.urandom(6).hex()}",
        "variables": {"customer_name": "Rohan Sharma", "outstanding_amount": "4500",
                      "overdue_days": "3", "dpd_bucket": "0-7"},
    }
    if getattr(args, "bot", None):
        payload["botId"] = args.bot
    payload.update(extra)
    return json.dumps(payload).encode()


def post_webhook(args, body: bytes, headers: dict) -> httpx.Response:
    url = f"{args.base}/telephony/webhook/vaani"
    log(f"POST {url}")
    return httpx.post(url, content=body, headers=headers, timeout=20)


def ws_local(args, public_url: str) -> str:
    """Rewrite the public host to --base's host for same-machine testing."""
    if args.no_rewrite:
        return public_url
    base_host = args.base.split("//", 1)[1]
    path = public_url.split("/ws/", 1)[1]
    return f"ws://{base_host}/ws/{path}"


# ── caller audio ───────────────────────────────────────────────────────────

def load_wav_8k(path: str) -> bytes:
    with wave.open(path) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            sys.exit(f"{path}: need mono 16-bit WAV")
        rate, pcm = w.getframerate(), w.readframes(w.getnframes())
    if rate == 8000:
        return pcm
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    out_n = int(len(samples) * 8000 / rate)
    out = (samples[min(int(i * rate / 8000), len(samples) - 1)] for i in range(out_n))
    log(f"resampled {path} {rate} Hz → 8000 Hz")
    return struct.pack(f"<{out_n}h", *out)


def load_raw_8k(path: str) -> bytes:
    """Load headerless 8 kHz PCM16 little-endian mono audio."""
    pcm = Path(path).read_bytes()
    if not pcm:
        sys.exit(f"{path}: raw PCM file is empty")
    if len(pcm) % 2:
        sys.exit(f"{path}: raw PCM16 must contain an even number of bytes")
    return pcm


def synthetic_speech_8k(seconds: float = 1.6) -> bytes:
    """Formant-filtered vowels — synthetic but VAD-tripping caller audio."""
    rate, out = 8000, bytearray()
    for f1, f2 in [(730, 1090), (270, 2290), (300, 870), (530, 1840)]:
        y1, y2 = [0.0, 0.0], [0.0, 0.0]
        for i in range(int(rate * seconds / 4)):
            t = i / rate
            src = 0.6 * ((t * 120) % 1.0 - 0.5)
            r1, g1 = (2 * math.exp(-math.pi * 80 / rate) * math.cos(2 * math.pi * f1 / rate),
                      math.exp(-2 * math.pi * 80 / rate))
            v1 = src + r1 * y1[0] - g1 * y1[1]
            y1 = [v1, y1[0]]
            r2, g2 = (2 * math.exp(-math.pi * 120 / rate) * math.cos(2 * math.pi * f2 / rate),
                      math.exp(-2 * math.pi * 120 / rate))
            v2 = v1 + r2 * y2[0] - g2 * y2[1]
            y2 = [v2, y2[0]]
            out += struct.pack("<h", max(-32000, min(32000, int(v2 * 6000))))
    return bytes(out)


async def caller_audio(args, text: str) -> tuple[bytes, str]:
    if getattr(args, "wav", None):
        return load_wav_8k(args.wav), f"wav:{args.wav}"
    if getattr(args, "raw", None):
        return load_raw_8k(args.raw), f"raw-pcm16le-8k:{args.raw}"
    api_key = os.environ.get("SARVAM_API_KEY", "")
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=30) as cx:
                r = await cx.post(
                    "https://api.sarvam.ai/text-to-speech",
                    headers={"api-subscription-key": api_key},
                    json={"text": text, "target_language_code": "hi-IN",
                          "speaker": "anand", "model": "bulbul:v3",
                          "speech_sample_rate": 8000})
                r.raise_for_status()
            with wave.open(io.BytesIO(base64.b64decode(r.json()["audios"][0]))) as w:
                return w.readframes(w.getnframes()), "sarvam-tts"
        except Exception as exc:  # noqa: BLE001
            log(f"Sarvam caller TTS unavailable ({exc}); using synthetic audio")
    return synthetic_speech_8k(), "synthetic"


# ── dialer-side call session ───────────────────────────────────────────────

class DialerCall:
    """One simulated Vaani call: uplink pacing, event logging, stats."""

    def __init__(self, ws, session_id: str):
        self.ws = ws
        self.sid = session_id
        self.stream_sid = f"MZsim{os.urandom(6).hex()}"
        self.chunk_no = 0
        self.speak_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.stats = {"media_in": 0, "media_bytes": 0, "clears": 0, "stops": 0,
                      "transfers": [], "closed": False, "media_after_clear": 0}
        self._uplink_task: asyncio.Task | None = None

    async def handshake(self) -> None:
        await self.ws.send(json.dumps({"event": "connected", "protocol": "websocket"}))
        await self.ws.send(json.dumps({
            "event": "start", "streamSid": self.stream_sid,
            "start": {"track": "inbound", "streamSid": self.stream_sid,
                      "mediaFormat": {"encoding": "audio/lin",
                                      "sampleRate": 8000, "channels": 1}}}))
        log(f"sent connected + start (streamSid={self.stream_sid})", self.sid)
        self._uplink_task = asyncio.create_task(self._uplink())

    async def _uplink(self) -> None:
        """Real-time 20 ms cadence: queued speech, silence otherwise."""
        pending = b""
        while True:
            if not pending:
                try:
                    pending = self.speak_queue.get_nowait()
                    log(f"caller speaking ({len(pending)} bytes ≈ "
                        f"{len(pending) / 16000:.1f}s)", self.sid)
                except asyncio.QueueEmpty:
                    pending = b""
            frame, pending = ((pending[:320], pending[320:]) if pending
                              else (b"\x00" * 320, b""))
            self.chunk_no += 1
            await self.ws.send(json.dumps({
                "event": "media", "streamSid": self.stream_sid,
                "media": {"chunk": self.chunk_no,
                          "timestamp": str(int(time.time())),
                          "payload": base64.b64encode(frame).decode()}}))
            await asyncio.sleep(0.02)

    async def events(self, *, until, timeout: float):
        """Yield decoded events until `until(stats, event)` is true or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                async with asyncio.timeout(max(0.1, deadline - time.monotonic())):
                    raw = await self.ws.recv()
            except (TimeoutError, websockets.exceptions.ConnectionClosed) as exc:
                if isinstance(exc, websockets.exceptions.ConnectionClosed):
                    self.stats["closed"] = True
                    log("socket closed by EchoSphere", self.sid)
                return
            event = json.loads(raw)
            kind = event.get("event")
            if kind == "media":
                self.stats["media_in"] += 1
                self.stats["media_bytes"] += len(base64.b64decode(event["media"]["payload"]))
                if self.stats["clears"]:
                    self.stats["media_after_clear"] += 1
                if self.stats["media_in"] == 1:
                    log("first bot media chunk received", self.sid)
            elif kind == "clear":
                self.stats["clears"] += 1
                log(f"CLEAR received: {event.get('clear')}", self.sid)
            elif kind == "transfer":
                self.stats["transfers"].append(event.get("transfer") or {})
                log(f"TRANSFER received: {event.get('transfer')}", self.sid)
            elif kind == "stop":
                self.stats["stops"] += 1
                log(f"STOP received: {event.get('stop')}", self.sid)
            else:
                log(f"other event: {json.dumps(event)[:120]}", self.sid)
            if until(self.stats, event):
                return

    async def send_stop(self) -> None:
        log("dialer sends stop", self.sid)
        await self.ws.send(json.dumps({"event": "stop", "streamSid": self.stream_sid,
                                       "stop": {"reason": "callended"}}))

    async def send_dtmf(self, digit: str = "5") -> None:
        """Send the currently unsupported Vaani-style DTMF event."""
        log(f"dialer sends dtmf digit={digit} (no acknowledgement expected)", self.sid)
        await self.ws.send(json.dumps({
            "event": "dtmf",
            "streamSid": self.stream_sid,
            "dtmf": {"digit": digit, "duration": 120},
        }))

    async def send_markers(self) -> None:
        """Exercise both common marker spellings; EchoSphere ignores both."""
        for event_name, payload_name in (("mark", "mark"), ("marker", "marker")):
            log(f"dialer sends {event_name} (no acknowledgement expected)", self.sid)
            await self.ws.send(json.dumps({
                "event": event_name,
                "streamSid": self.stream_sid,
                payload_name: {"name": f"sim-{event_name}-1"},
            }))

    async def drain_until_close(self, timeout: float = 25) -> None:
        await self.events(until=lambda s, e: False, timeout=timeout)

    def close_uplink(self) -> None:
        if self._uplink_task:
            self._uplink_task.cancel()


async def open_call(args, **body_extra) -> tuple[str, str]:
    """Webhook → (public ws url, session id). Exits on non-200."""
    body = call_body(args, **body_extra)
    response = post_webhook(args, body, sign(body))
    if response.status_code != 200:
        sys.exit(f"webhook failed: {response.status_code} {response.text}")
    url = response.json()["url"]
    session_id = url.rsplit("/", 1)[-1]
    log(f"webhook OK → {url}", session_id)
    return url, session_id


# ── commands ───────────────────────────────────────────────────────────────

async def cmd_webhook(args) -> None:
    body = call_body(args)
    headers = sign(body)
    log(f"request body: {body.decode()}")
    log("request headers: X-Webhook-Timestamp=" + headers["X-Webhook-Timestamp"]
        + " X-Webhook-Signature=" + headers["X-Webhook-Signature"][:12] + "…")
    response = post_webhook(args, body, headers)
    log(f"response {response.status_code}: {response.text}")
    ok = response.status_code == 200 and "/ws/telephony/vaani/vs_" in response.text
    check("webhook returns a per-call WebSocket URL", ok)
    if ok:
        log("NOTE: the session was not connected; it expires from Redis after 900s")


async def cmd_invalid_signature(args) -> None:
    body = call_body(args)

    response = httpx.post(f"{args.base}/telephony/webhook/vaani", content=body,
                          headers={"Content-Type": "application/json"}, timeout=20)
    check("missing signature headers → 403", response.status_code == 403,
          str(response.status_code))

    headers = sign(body)
    headers["X-Webhook-Signature"] = "0" * 64
    response = post_webhook(args, body, headers)
    check("wrong signature → 403", response.status_code == 403, str(response.status_code))

    response = post_webhook(args, body, sign(body, ts=int(time.time()) - 3600))
    check("stale timestamp (1h old) → 403", response.status_code == 403,
          str(response.status_code))

    headers = sign(body)
    first = post_webhook(args, body, headers)
    replay = post_webhook(args, body, headers)
    check("replayed signature → first 200, replay 403",
          first.status_code == 200 and replay.status_code == 403,
          f"first={first.status_code} replay={replay.status_code}")


async def cmd_protocol_events(args) -> None:
    """Prove DTMF/marker events are safely ignored without killing the call."""
    url, sid = await open_call(args)
    async with websockets.connect(ws_local(args, url), open_timeout=10) as ws:
        call = DialerCall(ws, sid)
        await call.handshake()
        await call.events(until=lambda s, e: s["media_in"] >= 1, timeout=60)
        check("call established before unsupported events",
              call.stats["media_in"] >= 1, session=sid)
        protocol_ok = True
        try:
            await call.send_dtmf(args.dtmf_digit)
            await call.send_markers()
            await asyncio.sleep(0.25)
            await call.send_stop()
        except websockets.exceptions.ConnectionClosed as exc:
            protocol_ok = False
            log(f"socket closed while sending protocol events: {exc}", sid)
        check("DTMF/mark/marker ignored without protocol failure",
              protocol_ok, session=sid)
        await call.drain_until_close()
        call.close_uplink()
    check("protocol-events call closed cleanly", call.stats["closed"], session=sid)
    log(f"stats: {call.stats}", sid)


async def cmd_full_call(args) -> None:
    utterance, source = await caller_audio(args, args.say)
    url, sid = await open_call(args)
    async with websockets.connect(ws_local(args, url), open_timeout=10) as ws:
        call = DialerCall(ws, sid)
        await call.handshake()
        await call.events(until=lambda s, e: s["media_in"] >= 3, timeout=60)
        check("bot greeting media received", call.stats["media_in"] >= 3,
              f"{call.stats['media_in']} chunks, caller audio={source}", sid)
        await call.speak_queue.put(utterance)
        before = call.stats["media_in"]
        await call.events(until=lambda s, e: s["media_in"] >= before + 3, timeout=60)
        check("bot replied after caller speech", call.stats["media_in"] >= before + 3,
              f"{call.stats['media_in'] - before} reply chunks", sid)
        await call.send_stop()
        await call.drain_until_close()
        call.close_uplink()
    check("≤1 outbound stop", call.stats["stops"] <= 1, str(call.stats["stops"]), sid)
    check("socket closed cleanly", call.stats["closed"], session=sid)
    log(f"stats: {call.stats}", sid)


async def cmd_barge_in(args) -> None:
    utterance, source = await caller_audio(args, args.say)
    url, sid = await open_call(args)
    async with websockets.connect(ws_local(args, url), open_timeout=10) as ws:
        call = DialerCall(ws, sid)
        await call.handshake()
        # Interrupt while the greeting is still streaming.
        await call.events(until=lambda s, e: s["media_in"] >= 2, timeout=60)
        log(f"barging in over the greeting (audio={source})", sid)
        await call.speak_queue.put(utterance)
        await call.events(until=lambda s, e: s["clears"] >= 1, timeout=30)
        check("clear received on barge-in", call.stats["clears"] >= 1,
              f"clears={call.stats['clears']}", sid)
        await call.events(until=lambda s, e: s["media_after_clear"] >= 3, timeout=60)
        check("fresh bot media after clear", call.stats["media_after_clear"] >= 3,
              f"{call.stats['media_after_clear']} chunks", sid)
        await call.send_stop()
        await call.drain_until_close()
        call.close_uplink()
    log(f"stats: {call.stats}", sid)


async def cmd_transfer(args) -> None:
    text = args.say if args.say != DEFAULT_SAY else "मुझे एजेंट से बात करनी है। एजेंट से बात कराओ।"
    utterance, source = await caller_audio(args, text)
    url, sid = await open_call(args)
    async with websockets.connect(ws_local(args, url), open_timeout=10) as ws:
        call = DialerCall(ws, sid)
        await call.handshake()
        await call.events(until=lambda s, e: s["media_in"] >= 3, timeout=60)
        log(f"caller asks for an agent (audio={source})", sid)
        await call.speak_queue.put(utterance)
        await call.events(until=lambda s, e: s["transfers"], timeout=90)
        ok = bool(call.stats["transfers"])
        check("transfer event received", ok,
              json.dumps(call.stats["transfers"][:1]), sid)
        if ok:
            log("dialer would now run its agent-transfer flow", sid)
        await call.send_stop()
        await call.drain_until_close()
        call.close_uplink()
    log(f"stats: {call.stats}", sid)


async def cmd_negative(args) -> None:
    # Routing negatives (each body unique → no replay collisions).
    body = call_body(args, botId="bot_does_not_exist")
    check("unknown botId → 404", post_webhook(args, body, sign(body)).status_code == 404)

    body = call_body(args, botId="../../etc/passwd")
    check("malformed botId → 422", post_webhook(args, body, sign(body)).status_code == 422)

    body = call_body(args, botId="bot-101")  # demo bot of another tenant
    check("cross-tenant botId → 404", post_webhook(args, body, sign(body)).status_code == 404)

    body = json.dumps({"callId": f"SIM-{os.urandom(4).hex()}"}).encode()
    check("missing To → 422", post_webhook(args, body, sign(body)).status_code == 422)

    body = call_body(args)
    body = body.replace(args.to.encode(), b"+10000000000")
    check("unmapped number → 404", post_webhook(args, body, sign(body)).status_code == 404)

    # Dead-session WebSocket → upgrade rejected with HTTP 403.
    dead = f"{args.base.replace('http', 'ws', 1)}/ws/telephony/vaani/vs_does_not_exist"
    try:
        async with websockets.connect(dead, open_timeout=10) as ws:
            await ws.recv()
        check("dead session WS rejected", False, "connected?!")
    except websockets.exceptions.InvalidStatus as exc:
        check("dead session WS → HTTP 403 at upgrade",
              exc.response.status_code == 403, str(exc.response.status_code))
    except Exception as exc:  # noqa: BLE001
        check("dead session WS rejected", True, type(exc).__name__)

    # Bad start handshake → close 4400.
    url, sid = await open_call(args)
    async with websockets.connect(ws_local(args, url), open_timeout=10) as ws:
        for _ in range(4):
            await ws.send(json.dumps({"event": "not-a-start"}))
        try:
            async with asyncio.timeout(15):
                await ws.recv()
            check("bad handshake → close 4400", False, "got a message instead", sid)
        except websockets.exceptions.ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            check("bad handshake → close 4400", code == 4400, str(code), sid)

    # Duplicate connection while a call is live → 4409.
    url, sid = await open_call(args)
    async with websockets.connect(ws_local(args, url), open_timeout=10) as ws:
        call = DialerCall(ws, sid)
        await call.handshake()
        await call.events(until=lambda s, e: s["media_in"] >= 1, timeout=60)
        try:
            async with websockets.connect(ws_local(args, url), open_timeout=10) as dup:
                await dup.recv()
            check("duplicate connection → 4409", False, "second socket lived", sid)
        except websockets.exceptions.ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            check("duplicate connection → 4409", code == 4409, str(code), sid)
        except websockets.exceptions.InvalidStatus as exc:
            check("duplicate connection → 4409", False,
                  f"HTTP {exc.response.status_code}", sid)
        await call.send_stop()
        await call.drain_until_close()
        call.close_uplink()


async def cmd_abrupt_disconnect(args) -> None:
    url, sid = await open_call(args)
    ws = await websockets.connect(ws_local(args, url), open_timeout=10)
    call = DialerCall(ws, sid)
    await call.handshake()
    await call.events(until=lambda s, e: s["media_in"] >= 2, timeout=60)
    call.close_uplink()
    log("dropping the socket abruptly (no close frame, no stop event)", sid)
    try:
        ws.transport.abort()  # TCP RST — as close to a network drop as we can get
    except AttributeError:
        await ws.close()

    # Server must clean the session up; the dead URL must then be rejected.
    deadline = time.monotonic() + 30
    rejected = False
    while time.monotonic() < deadline and not rejected:
        await asyncio.sleep(2)
        try:
            probe = await websockets.connect(ws_local(args, url), open_timeout=10)
            await probe.close()
        except websockets.exceptions.InvalidStatus as exc:
            rejected = exc.response.status_code == 403
        except Exception:  # noqa: BLE001
            pass
    check("session destroyed after abrupt disconnect (reconnect → HTTP 403)",
          rejected, session=sid)
    log("recovery path for a real dialer: send a NEW webhook, get a new URL", sid)


DEFAULT_SAY = "हाँ जी, बोलिए। मैं सुन रहा हूँ।"

COMMANDS = {
    "webhook": cmd_webhook,
    "full-call": cmd_full_call,
    "invalid-signature": cmd_invalid_signature,
    "protocol-events": cmd_protocol_events,
    "barge-in": cmd_barge_in,
    "transfer": cmd_transfer,
    "negative": cmd_negative,
    "abrupt-disconnect": cmd_abrupt_disconnect,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help=f"gateway base URL (default {DEFAULT_BASE})")
    parser.add_argument("--to", default=DEFAULT_TO,
                        help="EchoSphere-mapped dialed number (default: mPokket DID)")
    parser.add_argument("--bot", default=None, help="botId for per-campaign routing")
    parser.add_argument("--say", default=DEFAULT_SAY,
                        help="caller utterance text (Sarvam TTS)")
    audio = parser.add_mutually_exclusive_group()
    audio.add_argument("--wav", default=None,
                       help="mono 16-bit WAV file to use as caller audio")
    audio.add_argument("--raw", default=None,
                       help="headerless 8 kHz PCM16 little-endian mono audio")
    parser.add_argument("--dtmf-digit", default="5",
                        help="digit sent by protocol-events (default 5)")
    parser.add_argument("--no-rewrite", action="store_true",
                        help="connect to the returned public URL as-is")
    args = parser.parse_args()

    asyncio.run(COMMANDS[args.command](args))
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
