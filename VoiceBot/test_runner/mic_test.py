"""
CLI test runner for VoiceBotOrchestrator (mic in / speaker out).

Usage (from repo root Synthora-AI):
  python -m voicebot.test_runner.mic_test --voicebot-id <id> [--caller-phone +1...]

Usage (from voicebot/ directory):
  python -m test_runner.mic_test --voicebot-id <id> [--caller-phone +1...]

Context: REDIS_URL, MONGO_URI in .env (see config.settings).

After seeding with scripts/run_final_config_test.py, use e.g.
  --voicebot-id vb_4dfa73dc775b --caller-phone +15550001111
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

# voicebot/test_runner/mic_test.py -> package dir = voicebot/, repo = parent
_VOICEBOT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _VOICEBOT_DIR.parent
for _p in (_REPO_ROOT, _VOICEBOT_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import numpy as np
import sounddevice as sd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("mic_test")

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"


def record_until_enter() -> bytes:
    try:
        print("\n[MIC] Press ENTER to start recording...")
        try:
            input()
        except EOFError:
            print("[MIC] EOF detected — ending recording session")
            raise KeyboardInterrupt()

        print("[MIC] Recording... Press ENTER to stop.\n")
        chunks = []

        def callback(indata, frames, time, status):
            chunks.append(indata.copy())

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            callback=callback,
        ):
            try:
                input()
            except EOFError:
                print("[MIC] EOF — stopping recording")

        if not chunks:
            return b""
        audio = np.concatenate(chunks, axis=0)
        return audio.tobytes()

    except KeyboardInterrupt:
        raise  # Re-raise so main loop catches it


def play_audio(audio_bytes: bytes, sample_rate: int = 8000) -> None:
    if not audio_bytes:
        return
    try:
        audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
        sd.play(audio_array, samplerate=sample_rate)
        sd.wait()
    except Exception as e:
        logger.error("Audio playback error: %s", e)


async def run_test(voicebot_id: str, caller_phone: str) -> None:
    import redis as redis_sync

    from adapters.audio_utils import resample_pcm_to_8k
    from config.settings import Settings
    from voicebot.config_layer.db import (
        COLLECTION_CALLER_GRAPHS,
        MongoDB,
        create_indexes,
    )
    from voicebot.config_layer.loader import ConfigLoader
    from orchestrator.orchestrator import VoiceBotOrchestrator

    settings = Settings()
    mongo_uri = (settings.mongo_uri or "").strip()
    if not mongo_uri:
        print("[FATAL] MONGO_URI is empty — set it in .env")
        return

    _uri_show = (mongo_uri[:45] + "...") if len(mongo_uri) > 45 else mongo_uri
    print("\n" + "=" * 60)
    print(" SYNTHORA VOICEBOT — MIC TEST")
    print("=" * 60)
    print(f" Voicebot ID : {voicebot_id}")
    print(f" Caller Phone: {caller_phone}")
    print(f" MONGO_URI   : {_uri_show}")
    print(f" MONGO_DB    : {settings.mongo_db_name}")
    print(f" REDIS_URL   : {settings.redis_url}")
    print("=" * 60 + "\n")

    r_sync = redis_sync.from_url(
        (settings.redis_url or "").strip() or "redis://localhost:6379",
        decode_responses=True,
    )
    stale = r_sync.keys("session:*")
    if stale:
        r_sync.delete(*stale)
        print(
            f"[CLEANUP] Cleared {len(stale)} stale Redis session(s): "
            f"{stale}",
        )
    else:
        print("[CLEANUP] No stale sessions ✅")

    print("\n[INIT] Connecting to MongoDB...")
    await MongoDB.connect()
    await create_indexes()

    print(f"[INIT] Loading config: {voicebot_id}")
    loader = ConfigLoader()
    config = await loader.load(voicebot_id)
    print(f"[INIT] Loaded: {config.name} ✅")
    print(
        f"[INIT] LLM   : {config.engine.llm_provider_id} / "
        f"{config.engine.llm_model_id}",
    )
    print(f"[INIT] STT   : {config.engine.stt_provider_id}")
    print(f"[INIT] TTS   : {config.engine.tts_provider_id}")
    try:
        goals_on = [
            k for k, v in config.goals.model_dump().items()
            if v is True and isinstance(v, bool)
        ]
        print(f"[INIT] Goals : {goals_on if goals_on else 'none enabled'}")
    except Exception:
        print("[INIT] Goals : (not configured)")

    print(f"[INIT] Voice : {config.engine.voice_id}")
    print(f"[INIT] Prompt: {config.engine.system_role[:80]}...")

    orchestrator = VoiceBotOrchestrator(config)
    call_id = str(uuid.uuid4())
    print(f"\n[CALL] call_id = {call_id}")

    print("[CALL] Initializing...\n")
    greeting_audio = await orchestrator.initialize(
        call_id=call_id,
        caller_phone=caller_phone,
    )

    session_keys = r_sync.keys("session:*")
    if session_keys:
        print(f"[VERIFY] Redis session created ✅ : {session_keys[0]}")
    else:
        print("[VERIFY] ⚠️  Redis session NOT found after initialize()")

    print("[CALL] Playing greeting...")
    play_audio(greeting_audio)

    turn = 0
    extraction = None

    async def do_end_call(reason: str) -> dict | None:
        nonlocal extraction
        print(f"\n[CALL] Ending call (reason={reason})...")
        try:
            extraction = await orchestrator.end_call(reason=reason)
            print("[CALL] end_call() completed ✅")
        except Exception as e:
            print(f"[CALL] end_call() error: {e}")
            import traceback
            traceback.print_exc()
            leftover = r_sync.keys("session:*")
            if leftover:
                r_sync.delete(*leftover)
                print(
                    f"[CLEANUP] Force deleted {len(leftover)} Redis keys",
                )
        return extraction

    try:
        while True:
            turn += 1
            print(f"\n{'─' * 55}")
            print(f" TURN {turn}")
            print(f"{'─' * 55}")

            audio_bytes = record_until_enter()
            if not audio_bytes:
                print("[WARN] No audio recorded. Try again.")
                continue

            audio_bytes = resample_pcm_to_8k(audio_bytes, SAMPLE_RATE)
            print("[PIPELINE] Processing utterance...")

            try:
                response_audio = await orchestrator.handle_utterance(
                    audio_bytes,
                )
            except Exception as e:
                print(f"[ERROR] handle_utterance failed: {e}")
                import traceback
                traceback.print_exc()
                continue

            all_turns = orchestrator.call_state.turns
            user_turns = [t for t in all_turns if t.role == "user"]
            bot_turns = [t for t in all_turns if t.role == "assistant"]

            print(f"\n RESULT:")
            if user_turns:
                lu = user_turns[-1]
                print(f"   Caller  : {lu.content}")
                if lu.intent:
                    print(
                        f"   Intent  : {lu.intent} "
                        f"({(lu.confidence or 0):.0%})",
                    )
            if bot_turns:
                lb = bot_turns[-1]
                if lb.content and lb.content.strip():
                    print(f"   Bot     : {lb.content}")
                else:
                    print("   Bot     : ⚠️  EMPTY RESPONSE")

            cs = orchestrator.call_state
            print(f"\n MEMORY:")
            print(f"   Turns in memory : {len(all_turns)}")
            print(f"   Turn count      : {cs.turn_count}")
            print(f"   Sentiment       : {cs.sentiment_trend}")
            if cs.running_summary:
                print(
                    f"   Summary         : {cs.running_summary[:100]}",
                )

            r_keys = r_sync.keys("session:*")
            print(f"\n REDIS:")
            if r_keys:
                try:
                    raw = r_sync.get(r_keys[0])
                    if raw:
                        sd_data = json.loads(raw)
                        r_turns = len(sd_data.get("turns", []))
                        m_turns = len(all_turns)
                        in_sync = r_turns == m_turns
                        print(f"   Key             : {r_keys[0]}")
                        print(f"   Turns stored    : {r_turns}")
                        print(f"   Memory turns    : {m_turns}")
                        print(
                            f"   In sync         : "
                            f"{'✅' if in_sync else '❌'}",
                        )
                        if not in_sync:
                            print(
                                f"   ⚠️  MISMATCH: memory={m_turns} "
                                f"redis={r_turns}",
                            )
                except Exception as e:
                    print(f"   Read error: {e}")
            else:
                print("   ❌ No session key found!")

            if response_audio:
                print("\n[TTS] Playing response...")
                play_audio(response_audio)
            else:
                print("\n[TTS] ⚠️  No audio")

            if orchestrator.call_state.escalation_triggered:
                print("\n[ESCALATION] Triggered. Ending call.")
                await do_end_call("escalation")
                break

    except KeyboardInterrupt:
        print("\n\n[CTRL+C] Graceful shutdown...")
        await do_end_call("ctrl_c")

    except Exception as e:
        print(f"\n[ERROR] Unexpected: {e}")
        import traceback
        traceback.print_exc()
        await do_end_call("error")

    print("\n" + "=" * 60)
    print(" FINAL VERIFICATION")
    print("=" * 60)

    try:
        db = MongoDB.db()
        graph = await db[COLLECTION_CALLER_GRAPHS].find_one({
            "voicebot_id": voicebot_id,
            "caller_phone": caller_phone,
        })
        if graph:
            nodes = graph.get("nodes", [])
            edges = graph.get("edges", [])
            history = graph.get("call_history", [])
            print(" MongoDB Graph  : ✅ SAVED")
            print(f"   Nodes        : {len(nodes)}")
            print(f"   Edges        : {len(edges)}")
            print(f"   Call history : {len(history)}")
            print(
                f"   Caller name  : "
                f"{graph.get('caller_name', 'not captured')}",
            )
            if history:
                print(
                    f"   Summary      : "
                    f"{history[-1].get('summary', '')[:100]}",
                )
            if nodes:
                print("   Sample nodes :")
                for n in nodes[:5]:
                    print(
                        f"     [{n.get('type')}] "
                        f"{n.get('key')} = {n.get('value')}",
                    )
        else:
            print(" MongoDB Graph  : ❌ NOT SAVED")
            print("   Check [end_call] logs above for errors")
    except Exception as e:
        print(f" MongoDB check error: {e}")

    remaining = r_sync.keys("session:*")
    if not remaining:
        print(" Redis session  : ✅ DELETED")
    else:
        print(f" Redis session  : ❌ STILL EXISTS: {remaining}")
        r_sync.delete(*remaining)
        print(" Redis session  : 🔧 Force deleted")

    if extraction:
        print(f"\n ENTITY EXTRACTION:")
        print(json.dumps(extraction, indent=2, default=str))
    else:
        print("\n ENTITY EXTRACTION: None")

    cs = orchestrator.call_state
    if cs:
        print(f"\n Total turns    : {cs.turn_count}")
        print(
            f" Duration       : {cs.call_duration_minutes():.1f} min",
        )

    print("=" * 60)
    await MongoDB.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description="Synthora VoiceBot Mic Test",
    )
    parser.add_argument("--voicebot-id", required=True)
    parser.add_argument(
        "--caller-phone",
        default="+919999999999",
    )
    args = parser.parse_args()
    asyncio.run(run_test(args.voicebot_id, args.caller_phone))


if __name__ == "__main__":
    main()

#to test(python -m voicebot.test_runner.mic_test --voicebot-id vb_6eeef214d2ce --caller-phone +9015214225)
