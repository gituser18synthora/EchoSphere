"""Audio-level (telephony path) conversation harness for the Haier rejection bot.

Plays the scripted caller of ``haier_test_cases.json`` (cases whose ``modes``
include "voice") as real 8 kHz audio over the SAME websocket protocol
FreeSWITCH's mod_audio_fork uses, against the telephony runtime of the chosen
environment. Nothing is dialled: the session is minted directly (see
``create_session.py``) with ``call_id=None``, so no ESL command ever reaches
the real switch. Real STT, endpointing, barge-in, decision model, workflow,
TTS and recording run exactly as on a phone call.

Caller audio = Sarvam bulbul:v3 TTS at 8 kHz (cached), optional babble noise.
The bot's audio is modelled as a far-end playout buffer (playAudio appends,
killAudio clears), which decides when "the bot finished speaking".

    env/bin/python -B haier/qa/run_voice_cases.py --env local --output storage/tmp/haier_qa/voice_local.json
    env/bin/python -B haier/qa/run_voice_cases.py --env demo  --output storage/tmp/haier_qa/voice_demo.json

Per call it saves a stereo WAV (left = caller, right = bot as played) and the
session's transcript/checkpoint fetched read-only after the call.
Options: --only ID[,ID], --repeat N, --cases FILE, --artifacts DIR.
Test sessions are normal conversations of the bot in that environment
(channel freeswitch, no caller number, no call id).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import random
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from shared.providers.base import ProviderConfig  # noqa: E402
from shared.providers.factory import get_tts_provider  # noqa: E402

RATE = 8000
FRAME_BYTES = 320  # 20 ms of s16le mono at 8 kHz
ENVS = {
    "local": {"ws": "ws://127.0.0.1:9002/ws/telephony/freeswitch/{sid}?transport=audio_fork"},
    "demo": {"ws": "ws://192.168.60.123:9011/ws/telephony/freeswitch/{sid}?transport=audio_fork"},
}
CALLER_VOICES = ["rahul", "shreya", "amit", "simran", "mohit", "tanya", "sumit", "roopa"]
QUIET_S = 1.5          # bot silence that counts as "finished speaking"
SPEECH_DBFS = -32.0    # louder 20 ms frames are bot speech; ambience is ~-45 dBFS
BOT_WAIT_S = 25.0      # max wait for the bot to speak/finish before proceeding
END_WAIT_S = 30.0      # after the script, wait for the bot to close the call


def run_env_script(env: str, script: Path, args: list[str], timeout: int = 120) -> str:
    if env == "local":
        cmd = [str(REPO / "env/bin/python"), "-B", str(script), *args]
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    else:
        quoted = " ".join("'" + a.replace("'", "'\"'\"'") + "'" for a in args)
        remote = f"cd /var/www/html/EchoSphere && PYTHONDONTWRITEBYTECODE=1 timeout {timeout - 5} env/bin/python - {quoted}"
        with script.open("rb") as fh:
            proc = subprocess.run(["ssh", "echosphere-live", remote], stdin=fh, capture_output=True,
                                  text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{script.name} failed on {env}: {proc.stderr[-600:]}")
    return proc.stdout


def create_session(env: str, variables: dict) -> str:
    out = run_env_script(env, REPO / "haier/qa/create_session.py", [json.dumps(variables, ensure_ascii=False)])
    m = re.search(r"SESSION=(vs_[A-Za-z0-9_\-]+)", out)
    if not m:
        raise RuntimeError(f"no session id in output: {out[-300:]}")
    return m.group(1)


def fetch_sessions(env: str, sids: list[str]) -> dict:
    out = run_env_script(env, REPO / "haier/qa/fetch_sessions.py", sids, timeout=180)
    return json.loads(out.split("===JSON-BEGIN===")[1].split("===JSON-END===")[0])


# ----------------------------------------------------------------------------
# caller audio
# ----------------------------------------------------------------------------
class Voice:
    def __init__(self, cache: Path):
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)

    async def pcm(self, text: str, speaker: str, language: str) -> bytes:
        key = hashlib.sha1(f"{speaker}|{language}|{text}".encode()).hexdigest()
        path = self.cache / f"{key}.pcm"
        if path.exists():
            return path.read_bytes()
        tts = get_tts_provider(ProviderConfig(provider="sarvam", model="bulbul:v3", voice=speaker,
                                              language=language, api_key_reference="env:SARVAM_API_KEY",
                                              timeout_seconds=30, extra={"output_sample_rate": RATE}))
        result = await tts.synthesize(text, voice=speaker, language=language)
        audio = result.audio
        if result.sample_rate != RATE:
            from shared.audio.pcm import resample_pcm
            audio = resample_pcm(audio, result.sample_rate, RATE)
        path.write_bytes(audio)
        return audio


def is_english(text: str) -> bool:
    return not re.search(r"[ऀ-ॿ]", text)


async def babble(voice: Voice, seconds: float) -> np.ndarray:
    lines = [("आज बाज़ार में बहुत भीड़ थी और सब लोग सब्ज़ी खरीद रहे थे।", "vijay"),
             ("कल मैच में आखिरी ओवर में छक्का लगा, सबने खूब तालियाँ बजाईं।", "kavya"),
             ("मौसम विभाग ने अगले दो दिन बारिश की संभावना बताई है।", "aditya")]
    tracks = []
    for text, spk in lines:
        tracks.append(np.frombuffer(await voice.pcm(text, spk, "hi-IN"), dtype="<i2").astype(np.float32))
    n = int(seconds * RATE)
    mix = np.zeros(n, dtype=np.float32)
    for i, t in enumerate(tracks):
        reps = int(np.ceil((n + i * 900) / max(1, len(t)))) + 1
        looped = np.tile(t, reps)[i * 900:i * 900 + n]
        mix += looped
    return mix


def mix_noise(speech: bytes, noise: np.ndarray | None, rel_db: float = -12.0) -> bytes:
    s = np.frombuffer(speech, dtype="<i2").astype(np.float32)
    if noise is None:
        return speech
    pad = int(0.4 * RATE)
    s = np.concatenate([np.zeros(pad, np.float32), s, np.zeros(pad, np.float32)])
    n = noise[:len(s)] if len(noise) >= len(s) else np.resize(noise, len(s))
    rms_s = np.sqrt(np.mean(s[s != 0] ** 2)) if np.any(s) else 1000.0
    rms_n = np.sqrt(np.mean(n ** 2)) or 1.0
    out = s + n * (rms_s / rms_n) * (10 ** (rel_db / 20))
    return np.clip(out, -32000, 32000).astype("<i2").tobytes()


# ----------------------------------------------------------------------------
# one call
# ----------------------------------------------------------------------------
class Call:
    def __init__(self):
        self.t0 = time.monotonic()
        self.playout_end = 0.0      # monotonic time the far end finishes playing
        self.bot_segments: list[list[float]] = []  # [start, end] played windows
        self.bot_pcm: list[tuple[float, bytes]] = []
        self.caller_pcm: list[tuple[float, bytes]] = []
        self.queue = bytearray()
        self.closed = False
        self.close_info = None
        self.caller_utt: list[dict] = []
        self.log: list[str] = []
        self.kills = 0

    def now(self) -> float:
        return time.monotonic()

    def rel(self, t: float) -> float:
        return round(t - self.t0, 2)

    def bot_speaking(self) -> bool:
        return bool(self.bot_segments) and self.now() < self.bot_segments[-1][1]

    def speech_end(self) -> float:
        return self.bot_segments[-1][1] if self.bot_segments else 0.0

    def on_bot_audio(self, audio: bytes):
        # The far end plays every chunk back to back. Background ambience is
        # streamed continuously at ~-45 dBFS; only frames louder than
        # SPEECH_DBFS count as the bot speaking.
        now = self.now()
        start = max(now, self.playout_end)
        samples = np.frombuffer(audio[:len(audio) - len(audio) % 2], dtype="<i2").astype(np.float32)
        for k in range(0, len(samples), 160):
            frame = samples[k:k + 160]
            if not len(frame):
                break
            rms = float(np.sqrt(np.mean(frame ** 2))) or 1.0
            t = start + k / RATE
            if 20 * np.log10(rms / 32768.0) > SPEECH_DBFS:
                if self.bot_segments and t - self.bot_segments[-1][1] <= 0.35:
                    self.bot_segments[-1][1] = t + len(frame) / RATE
                else:
                    self.bot_segments.append([t, t + len(frame) / RATE])
        self.playout_end = start + len(samples) / RATE
        self.bot_pcm.append((start, audio))

    def on_kill(self):
        now = self.now()
        self.kills += 1
        self.bot_segments = [seg for seg in self.bot_segments if seg[0] < now]
        if self.bot_segments and self.bot_segments[-1][1] > now:
            self.bot_segments[-1][1] = now
        self.playout_end = now
        self.bot_pcm = [(s, a) for s, a in self.bot_pcm if s < now]
        self.log.append(f"{self.rel(now)} killAudio")

    def last_bot_start_after(self, t: float) -> float | None:
        for s, e in self.bot_segments:
            if s >= t:
                return s
        return None


async def run_call(env: str, sid: str, turns: list[dict], voice: Voice, case_seed: int) -> Call:
    import websockets

    call = Call()
    rng = random.Random(case_seed)
    speaker = CALLER_VOICES[case_seed % len(CALLER_VOICES)]
    url = ENVS[env]["ws"].format(sid=sid)
    async with websockets.connect(url, max_size=None, open_timeout=15, ping_interval=None) as ws:
        call.t0 = time.monotonic()

        async def sender():
            next_t = time.monotonic()
            while not call.closed:
                if len(call.queue) >= FRAME_BYTES:
                    frame = bytes(call.queue[:FRAME_BYTES])
                    del call.queue[:FRAME_BYTES]
                elif call.queue:
                    frame = bytes(call.queue) + b"\x00" * (FRAME_BYTES - len(call.queue))
                    call.queue.clear()
                else:
                    noise = (np.random.default_rng().normal(0, 18, FRAME_BYTES // 2)).astype("<i2")
                    frame = noise.tobytes()
                call.caller_pcm.append((time.monotonic(), frame))
                try:
                    await ws.send(frame)
                except Exception:  # noqa: BLE001
                    call.closed = True
                    break
                next_t += 0.02
                await asyncio.sleep(max(0.0, next_t - time.monotonic()))

        async def receiver():
            try:
                async for message in ws:
                    if isinstance(message, (bytes, bytearray)):
                        continue
                    try:
                        data = json.loads(message)
                    except ValueError:
                        continue
                    kind = data.get("type")
                    if kind == "playAudio":
                        audio = base64.b64decode(((data.get("data") or {}).get("audioContent")) or "")
                        if audio:
                            call.on_bot_audio(audio)
                    elif kind == "killAudio":
                        call.on_kill()
                    else:
                        call.log.append(f"{call.rel(call.now())} msg {kind}")
            except Exception as exc:  # noqa: BLE001
                call.close_info = repr(exc)[:200]
            finally:
                call.closed = True

        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())
        last_caller_end = call.t0

        async def wait_until(pred, timeout):
            end = time.monotonic() + timeout
            while time.monotonic() < end and not call.closed:
                if pred():
                    return True
                await asyncio.sleep(0.05)
            return pred()

        try:
            for idx, turn in enumerate(turns):
                if call.closed:
                    break
                after = turn.get("after", "bot_done")
                delay = float(turn.get("delay", 0.6 if after == "bot_done" else 0.0))
                if after == "bot_done":
                    ok = await wait_until(lambda: call.last_bot_start_after(last_caller_end) is not None, BOT_WAIT_S)
                    if not ok:
                        call.log.append(f"{call.rel(call.now())} turn{idx}: bot never started after caller")
                    await wait_until(lambda: (not call.bot_speaking()) and call.now() - call.speech_end() >= QUIET_S, BOT_WAIT_S + 30)
                    await asyncio.sleep(delay)
                elif after == "bot_start":
                    ok = await wait_until(lambda: call.last_bot_start_after(last_caller_end) is not None, BOT_WAIT_S)
                    start = call.last_bot_start_after(last_caller_end) or call.now()
                    await asyncio.sleep(max(0.0, start + delay - call.now()))
                else:  # immediate: pause after the caller's previous utterance
                    await asyncio.sleep(max(0.0, last_caller_end + delay - call.now()))
                if call.closed:
                    break
                if turn.get("hangup"):
                    call.log.append(f"{call.rel(call.now())} caller hangs up")
                    break
                if turn.get("silence"):
                    t_s = call.now()
                    await asyncio.sleep(float(turn["silence"]))
                    last_caller_end = t_s  # silence counts as a (non-)turn for "bot_done" waits
                    call.caller_utt.append({"i": idx, "silence": turn["silence"], "start": call.rel(t_s)})
                    continue
                text = turn["say"]
                lang = "en-IN" if is_english(text) else "hi-IN"
                pcm = await voice.pcm(text, turn.get("voice") or speaker, lang)
                if turn.get("noise"):
                    pcm = mix_noise(pcm, await babble(voice, len(pcm) / (2 * RATE) + 1.0))
                t_s = call.now()
                call.queue.extend(pcm)
                dur = len(pcm) / (2 * RATE)
                await asyncio.sleep(dur + 0.04)
                last_caller_end = call.now()
                call.caller_utt.append({"i": idx, "say": text, "start": call.rel(t_s), "end": call.rel(last_caller_end),
                                        "bot_speaking_at_start": bool(t_s < call.speech_end())})
            # script done: wait for the bot to answer and (ideally) close the call
            end = time.monotonic() + END_WAIT_S
            while time.monotonic() < end and not call.closed:
                if call.now() - max(call.speech_end(), last_caller_end) > 12.0:
                    break
                await asyncio.sleep(0.1)
        finally:
            call.closed = True
            for t in (send_task, recv_task):
                t.cancel()
            await asyncio.gather(send_task, recv_task, return_exceptions=True)
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
    return call


def response_gaps(call: Call) -> list[float]:
    gaps = []
    for u in call.caller_utt:
        if "end" not in u:
            continue
        end_abs = call.t0 + u["end"]
        nxt = call.last_bot_start_after(end_abs)
        if nxt is not None:
            gaps.append(round(nxt - end_abs, 2))
    return gaps


def save_wav(call: Call, path: Path):
    total = max([s + len(a) / (2 * RATE) for s, a in call.bot_pcm] + [t + 0.02 for t, _ in call.caller_pcm] + [call.t0]) - call.t0
    n = int((total + 0.5) * RATE)
    left = np.zeros(n, np.int16)
    right = np.zeros(n, np.int16)
    for t, f in call.caller_pcm:
        i = int((t - call.t0) * RATE)
        a = np.frombuffer(f, dtype="<i2")
        left[i:i + len(a)] = a[:max(0, n - i)]
    for s, a in call.bot_pcm:
        i = int((s - call.t0) * RATE)
        b = np.frombuffer(a, dtype="<i2")
        right[i:i + len(b)] = b[:max(0, n - i)]
    st = np.stack([left, right], axis=1).reshape(-1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(st.astype("<i2").tobytes())


# ----------------------------------------------------------------------------
async def main(opts) -> int:
    sys.path.insert(0, str(REPO / "haier/qa"))
    from run_text_cases import evaluate  # same expectation rules as the text harness

    spec = json.loads(opts.cases.read_text(encoding="utf-8"))
    payloads = spec["payloads"]
    only = {s.strip() for s in (opts.only or "").split(",") if s.strip()}
    voice = Voice(opts.artifacts / "audio_cache")
    outdir = opts.artifacts / f"voice_{opts.env}"
    outdir.mkdir(parents=True, exist_ok=True)
    results = []
    if opts.output and opts.output.exists() and opts.resume:
        results = json.loads(opts.output.read_text())["results"]
    done = {(r["id"], r["repeat"]) for r in results}
    jobs = []
    for case in spec["cases"]:
        if "voice" not in case.get("modes", []):
            continue
        if only and case["id"] not in only:
            continue
        reps = opts.repeat or int(case.get("repeat", 1))
        for rep in range(1, reps + 1):
            if (case["id"], rep) not in done:
                jobs.append((case, rep))
    for case, rep in jobs:
        seed = int(hashlib.sha1(f"{case['id']}{rep}".encode()).hexdigest(), 16)
        rec = {"id": case["id"], "repeat": rep, "reason": case["reason"], "title": case["title"],
               "priority": case.get("priority"), "patterns": case.get("patterns"), "env": opts.env}
        try:
            sid = create_session(opts.env, payloads[case["reason"]])
            rec["session_id"] = sid
            call = await run_call(opts.env, sid, case["turns"], voice, seed)
            rec.update({"caller_utterances": call.caller_utt, "bot_segments": [[call.rel(s), call.rel(e)] for s, e in call.bot_segments],
                        "response_gaps": response_gaps(call), "kills": call.kills, "log": call.log,
                        "close_info": call.close_info})
            wav = outdir / f"{case['id']}_{rep}_{sid}.wav"
            save_wav(call, wav)
            rec["wav"] = str(wav)
        except Exception as exc:  # noqa: BLE001
            rec["harness_error"] = repr(exc)[:400]
        results.append(rec)
        print(f"ran {case['id']}#{rep} sid={rec.get('session_id')} gaps={rec.get('response_gaps')} err={rec.get('harness_error')}", flush=True)
        if opts.output:
            opts.output.write_text(json.dumps({"env": opts.env, "results": results}, ensure_ascii=False, indent=1, default=str))
        await asyncio.sleep(2.0)
    # fetch transcripts + checkpoints (read-only) and evaluate
    await asyncio.sleep(8.0)
    sids = [r["session_id"] for r in results if r.get("session_id")]
    fetched = {}
    for i in range(0, len(sids), 20):
        fetched.update(fetch_sessions(opts.env, sids[i:i + 20]))
    cases = {c["id"]: c for c in spec["cases"]}
    for r in results:
        f = fetched.get(r.get("session_id"), {})
        r["fetched"] = f
        ck = (f or {}).get("checkpoint") or {}
        slots = (ck.get("slots") or {}) if isinstance(ck, dict) else {}
        bot_turns = [t["text"] for t in (f.get("turns") or []) if t.get("role") == "bot"]
        r["outcome"] = slots.get("outcome")
        r["bot_turns"] = bot_turns
        r["user_turns"] = [t["text"] for t in (f.get("turns") or []) if t.get("role") == "user"]
        res = {"outcome": r["outcome"], "bot_turns": bot_turns, "rows": [], "error": r.get("harness_error")}
        fails = evaluate(cases[r["id"]], res, spec.get("global_say_none") or [], mode="voice")
        exp = cases[r["id"]].get("expect") or {}
        if "max_dead_air_s" in exp and r.get("response_gaps"):
            worst = max(r["response_gaps"])
            if worst > exp["max_dead_air_s"]:
                fails.append(f"dead air {worst}s after a caller turn (max {exp['max_dead_air_s']})")
        if f.get("missing"):
            fails.append("transcript not persisted")
        r["failures"] = fails
        r["pass"] = not fails
    if opts.output:
        opts.output.write_text(json.dumps({"env": opts.env, "results": results}, ensure_ascii=False, indent=1, default=str))
    passed = sum(1 for r in results if r.get("pass"))
    for r in results:
        print(f"{'PASS' if r.get('pass') else 'FAIL'} {r['id']}#{r['repeat']} outcome={r.get('outcome')} {'; '.join(r.get('failures') or [])[:240]}")
    print(f"\n{passed}/{len(results)} voice conversations passed on {opts.env}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", choices=sorted(ENVS), required=True)
    p.add_argument("--cases", type=Path, default=Path(__file__).with_name("haier_test_cases.json"))
    p.add_argument("--only", default="")
    p.add_argument("--repeat", type=int, default=0)
    p.add_argument("--output", type=Path)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--artifacts", type=Path, default=REPO / "storage/tmp/haier_qa")
    raise SystemExit(asyncio.run(main(p.parse_args())))
