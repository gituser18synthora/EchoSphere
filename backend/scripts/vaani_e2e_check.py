"""Live Vaani-dialer simulation against the REAL gateway on :9011.

Real components: gateway process (webhook + WS), Redis sessions, MySQL routing,
Sarvam saaras:v3 STT, OpenAI gpt-4o-mini, Sarvam bulbul:v3 TTS (the published
mPokket bot config). The 'caller' is Sarvam REST TTS audio at 8 kHz.
"""

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

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

import httpx
import websockets

from shared.config import get_settings  # noqa: E402  (loads .env into os.environ)

GW = "http://127.0.0.1:9011"
PUBLIC_PREFIX = "ws://192.168.60.123:9011/ws/telephony/vaani/"
NUMBER = "+91 80 4522 1010"
BOTS = [
    ("bot_c2453561ef8c", "DPD 0-7"),
    ("bot_b97b33667066", "DPD 8-30"),
    ("bot_7ed9c825644f", "DPD 30-60"),
    ("bot_39db9985b7d5", "DPD 60-210+"),
]
SECRET = os.environ["TELEPHONY_WEBHOOK_SECRET"]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"X-Webhook-Signature": sig, "X-Webhook-Timestamp": ts,
                  "Content-Type": "application/json"}


async def sarvam_pcm_8k(text: str) -> tuple[bytes, str]:
    """Caller-side speech: Sarvam REST TTS at 8 kHz; synthetic vowels fallback."""
    try:
        async with httpx.AsyncClient(timeout=30) as cx:
            r = await cx.post(
                "https://api.sarvam.ai/text-to-speech",
                headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
                json={"text": text, "target_language_code": "hi-IN",
                      "speaker": "anand", "model": "bulbul:v3",
                      "speech_sample_rate": 8000},
            )
            r.raise_for_status()
            wav_bytes = base64.b64decode(r.json()["audios"][0])
        with wave.open(io.BytesIO(wav_bytes)) as w:
            assert w.getframerate() == 8000, w.getframerate()
            return w.readframes(w.getnframes()), "sarvam-tts"
    except Exception as exc:  # noqa: BLE001
        print(f"  (sarvam caller TTS unavailable: {exc}; using synthetic vowels)")
        rate, dur = 8000, 1.6
        out = bytearray()
        vowels = [(730, 1090), (270, 2290), (300, 870), (530, 1840)]
        for f1, f2 in vowels:
            y1 = [0.0, 0.0]
            y2 = [0.0, 0.0]
            for i in range(int(rate * dur / len(vowels))):
                t = i / rate
                src = 0.6 * ((t * 120) % 1.0 - 0.5)
                r1 = 2 * math.exp(-math.pi * 80 / rate) * math.cos(2 * math.pi * f1 / rate)
                g1 = math.exp(-2 * math.pi * 80 / rate)
                v1 = src + r1 * y1[0] - g1 * y1[1]
                y1 = [v1, y1[0]]
                r2 = 2 * math.exp(-math.pi * 120 / rate) * math.cos(2 * math.pi * f2 / rate)
                g2 = math.exp(-2 * math.pi * 120 / rate)
                v2 = v1 + r2 * y2[0] - g2 * y2[1]
                y2 = [v2, y2[0]]
                out += struct.pack("<h", max(-32000, min(32000, int(v2 * 6000))))
        return bytes(out), "synthetic"


async def session_for(bot_id: str | None, cx: httpx.AsyncClient):
    payload = {"To": NUMBER, "From": "+919812345678",
               "callId": f"E2E-{os.urandom(6).hex()}",
               "variables": {"customer_name": "Rohan", "outstanding_amount": "4500",
                             "overdue_days": "3", "dpd_bucket": "0-7"}}
    if bot_id:
        payload["botId"] = bot_id
    body, headers = signed(payload)
    r = await cx.post(f"{GW}/telephony/webhook/vaani", content=body, headers=headers)
    return r


async def redis_session(url: str):
    from shared.voice_sessions import load_voice_session

    return await load_voice_session(url.rstrip("/").rsplit("/", 1)[-1])


async def full_call(ws_url: str) -> dict:
    stats = dict(media_in=0, media_bytes=0, clears=0, stops=0, closed=False,
                 dup_code=None, reply_media_after_clear=0)
    local = ws_url.replace("ws://192.168.60.123:9011", "ws://127.0.0.1:9011")
    utterance, source = await sarvam_pcm_8k("हाँ जी, बोलिए। मैं सुन रहा हूँ।")
    stats["caller_audio_source"] = source
    sid = "MZe2e" + os.urandom(6).hex()
    chunk_no = 0
    speak_queue: asyncio.Queue[bytes] = asyncio.Queue()

    async with websockets.connect(local, open_timeout=10) as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "websocket"}))
        await ws.send(json.dumps({
            "event": "start", "streamSid": sid,
            "start": {"streamSid": sid,
                      "mediaFormat": {"encoding": "audio/lin",
                                      "sampleRate": 8000, "channels": 1}}}))

        async def uplink():
            nonlocal chunk_no
            pending = b""
            while True:
                if not pending:
                    try:
                        pending = speak_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pending = b""
                frame, pending = ((pending[:320], pending[320:]) if pending
                                  else (b"\x00" * 320, b""))
                chunk_no += 1
                await ws.send(json.dumps({
                    "event": "media", "streamSid": sid,
                    "media": {"chunk": chunk_no,
                              "timestamp": str(int(time.time() * 1000)),
                              "payload": base64.b64encode(frame).decode()}}))
                await asyncio.sleep(0.02)

        up = asyncio.create_task(uplink())
        barge_sent = False
        try:
            # Phase 1: greeting media; barge in after a few chunks.
            async with asyncio.timeout(60):
                while True:
                    msg = json.loads(await ws.recv())
                    ev = msg.get("event")
                    if ev == "media":
                        stats["media_in"] += 1
                        stats["media_bytes"] += len(base64.b64decode(msg["media"]["payload"]))
                        if stats["clears"]:
                            stats["reply_media_after_clear"] += 1
                            if stats["reply_media_after_clear"] >= 3:
                                break  # got a real post-barge-in bot reply
                        elif stats["media_in"] == 3 and not barge_sent:
                            barge_sent = True
                            await speak_queue.put(utterance)
                            # duplicate connection while the call is live → 4409
                            try:
                                dup = await websockets.connect(local, open_timeout=10)
                                await dup.recv()
                            except websockets.exceptions.ConnectionClosed as e:
                                stats["dup_code"] = e.rcvd.code if e.rcvd else None
                            except Exception as e:  # noqa: BLE001
                                stats["dup_code"] = f"handshake:{type(e).__name__}"
                    elif ev == "clear":
                        stats["clears"] += 1
                    elif ev == "stop":
                        stats["stops"] += 1
        except TimeoutError:
            stats["phase1_timeout"] = True

        # Phase 2: dialer hangs up.
        await ws.send(json.dumps({"event": "stop", "streamSid": sid,
                                  "stop": {"reason": "callended"}}))
        try:
            async with asyncio.timeout(25):
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("event") == "stop":
                        stats["stops"] += 1
                    elif msg.get("event") == "media":
                        stats["media_in"] += 1
        except websockets.exceptions.ConnectionClosed:
            stats["closed"] = True
        except TimeoutError:
            pass
        finally:
            up.cancel()
    return stats


async def main():
    async with httpx.AsyncClient(timeout=20) as cx:
        r = await cx.get(f"{GW}/health")
        check("gateway /health", r.status_code == 200 and r.json()["redis"]["ok"], r.text[:80])

        r = await cx.post(f"{GW}/telephony/webhook/vaani", json={"To": NUMBER})
        check("unsigned webhook → 403", r.status_code == 403, str(r.status_code))

        body, headers = signed({"callId": "E2E-missing-to"})
        r = await cx.post(f"{GW}/telephony/webhook/vaani", content=body, headers=headers)
        check("missing To → 422", r.status_code == 422, str(r.status_code))

        body, headers = signed({"To": NUMBER, "botId": "bot_does_not_exist",
                                "callId": f"E2E-{os.urandom(4).hex()}"})
        r = await cx.post(f"{GW}/telephony/webhook/vaani", content=body, headers=headers)
        check("unknown botId → 404", r.status_code == 404, str(r.status_code))

        body, headers = signed({"To": NUMBER, "botId": "bot-101",
                                "callId": f"E2E-{os.urandom(4).hex()}"})
        r = await cx.post(f"{GW}/telephony/webhook/vaani", content=body, headers=headers)
        check("cross-tenant botId → 404", r.status_code == 404, str(r.status_code))

        replay_body, replay_headers = signed({"To": NUMBER,
                                              "callId": f"E2E-{os.urandom(4).hex()}"})
        r = await cx.post(f"{GW}/telephony/webhook/vaani", content=replay_body,
                          headers=replay_headers)
        first_url = r.json().get("url", "") if r.status_code == 200 else ""
        r2 = await cx.post(f"{GW}/telephony/webhook/vaani", content=replay_body,
                           headers=replay_headers)
        check("replayed signature → 403",
              r.status_code == 200 and r2.status_code == 403,
              f"first={r.status_code} replay={r2.status_code}")

        call_url = None
        for bot_id, label in BOTS:
            r = await session_for(bot_id, cx)
            ok = r.status_code == 200
            url = r.json().get("url", "") if ok else ""
            sess = await redis_session(url) if ok else None
            routed = bool(sess and sess["bot_id"] == bot_id
                          and sess["tenant_id"] == "tn_22a809aecf66")
            check(f"webhook routes {label} ({bot_id})",
                  ok and routed and url.startswith(PUBLIC_PREFIX),
                  url[:70])
            if bot_id == "bot_c2453561ef8c" and ok:
                call_url = url

        check("returned URL uses TELEPHONY_PUBLIC_WS_BASE (192.168.60.123:9011)",
              bool(call_url and call_url.startswith(PUBLIC_PREFIX)), str(call_url))

    stats = await full_call(call_url)
    print(f"  call stats: {stats}")
    check("caller audio source", True, stats["caller_audio_source"])
    check("bot greeting media received (real TTS)",
          stats["media_in"] >= 3 and stats["media_bytes"] > 0,
          f"{stats['media_in']} chunks / {stats['media_bytes']} bytes")
    check("barge-in produced clear event", stats["clears"] >= 1, str(stats["clears"]))
    check("bot reply after caller speech (STT→LLM→TTS)",
          stats["reply_media_after_clear"] >= 3,
          str(stats["reply_media_after_clear"]))
    check("duplicate live connection → 4409", stats["dup_code"] == 4409,
          str(stats["dup_code"]))
    check("≤1 outbound stop", stats["stops"] <= 1, str(stats["stops"]))
    check("socket closed after stop", stats["closed"], "")

    # The worker deletes the session in its teardown, which may drain for a
    # few seconds after the client socket closes — poll instead of snapshotting.
    sess = await redis_session(call_url)
    deadline = time.time() + 25
    while sess is not None and time.time() < deadline:
        await asyncio.sleep(1)
        sess = await redis_session(call_url)
    check("session single-use (deleted after call)", sess is None, str(sess)[:80])
    try:
        ws = await websockets.connect(
            call_url.replace("ws://192.168.60.123:9011", "ws://127.0.0.1:9011"),
            open_timeout=10)
        await ws.recv()
        check("reconnect to dead session rejected", False, "connected?!")
    except websockets.exceptions.InvalidStatus as e:
        check("reconnect to dead session rejected (HTTP 403)",
              e.response.status_code == 403, str(e.response.status_code))
    except websockets.exceptions.ConnectionClosed as e:
        check("reconnect to dead session rejected (close)", True,
              str(e.rcvd.code if e.rcvd else None))

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


asyncio.run(main())
