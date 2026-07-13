"""
CLI test runner for VoiceBotOrchestrator (continuous mic / speaker with VAD).

Usage (from repo root Synthora-AI):
  python -m voicebot.test_runner.continuous_mic_test --voicebot-id <id> [--caller-phone +1...]

Usage (from voicebot/ directory):
  python -m test_runner.continuous_mic_test --voicebot-id <id> [--caller-phone +1...]

Context: REDIS_URL, MONGO_URI in .env (see config.settings).

After seeding with scripts/run_final_config_test.py, use e.g.
  --voicebot-id vb_4dfa73dc775b --caller-phone +15550001111
"""

#python -m voicebot.test_runner.continuous_mic_test --voicebot-id vb_50d8cd024ae4 --caller-phone +9015214225

import argparse
import asyncio
import json
import logging
import signal
import sys
import uuid
from datetime import datetime
from pathlib import Path

# voicebot/test_runner/continuous_mic_test.py -> package dir = voicebot/, repo = parent
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
logger = logging.getLogger("continuous_mic_test")

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"


async def run_test(voicebot_id: str, caller_phone: str) -> None:
    import redis as redis_sync

    from voicebot.adapters.audio_utils import resample_pcm_to_8k
    from voicebot.audio.continuous_io import ContinuousAudio
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
    print(" SYNTHORA VOICEBOT — CONTINUOUS MIC TEST")
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

    continuous_audio = ContinuousAudio()

    from voicebot.audio.tts_stream_player import TTSStreamPlayer

    tts_player = TTSStreamPlayer(
        tts_adapter=orchestrator.tts_adapter,
        suppression_flag=continuous_audio.suppression_event,
        sample_rate=8000,
        barge_in_event=continuous_audio.barge_in_event,
        continuous_audio=continuous_audio,
    )
    print("[CALL] Playing greeting...")
    await continuous_audio.play(greeting_audio, sample_rate=8000)

    turn = 0
    extraction = None
    call_ended = False

    async def do_end_call(reason: str) -> dict | None:
        nonlocal extraction, call_ended
        if call_ended:
            return extraction
        call_ended = True
        continuous_audio.request_shutdown()
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

    loop = asyncio.get_running_loop()

    def _request_mic_shutdown() -> None:
        loop.call_soon_threadsafe(continuous_audio.request_shutdown)

    try:
        loop.add_signal_handler(signal.SIGINT, _request_mic_shutdown)
    except NotImplementedError:
        # Windows: add_signal_handler not available
        signal.signal(signal.SIGINT, lambda *_: _request_mic_shutdown())

    try:
        print("\n[MIC] Continuous listening (VAD). Ctrl+C to end.\n")
        async for utterance_16k in continuous_audio.utterances():
            turn += 1
            print(f"\n{'─' * 55}")
            print(f" TURN {turn}")
            print(f"{'─' * 55}")

            print("[PIPELINE] Processing utterance...")
            audio_8k = resample_pcm_to_8k(utterance_16k, SAMPLE_RATE)

            try:
                response_audio = await orchestrator.handle_utterance(
                    audio_8k,
                    tts_stream_player=tts_player,
                )
            except Exception as e:
                print(f"[ERROR] handle_utterance failed: {e}")
                import traceback
                traceback.print_exc()
                continue

            if not tts_player.played_to_speaker:
                await continuous_audio.play(response_audio, sample_rate=8000)

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

            if tts_player.played_to_speaker:
                print("\n[TTS] Played via streaming player ✅")
            elif response_audio:
                print("\n[TTS] Playing response (fallback)...")
            else:
                print("\n[TTS] ⚠️  No audio")

            if orchestrator.call_state.escalation_triggered:
                print("\n[ESCALATION] Triggered. Ending call.")
                await do_end_call("escalation")
                break

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n\n[CTRL+C] Graceful shutdown...")
        await do_end_call("ctrl_c")

    except Exception as e:
        print(f"\n[ERROR] Unexpected: {e}")
        import traceback
        traceback.print_exc()
        await do_end_call("error")

    finally:
        continuous_audio.request_shutdown()
        if not call_ended:
            print("\n[CALL] Shutting down (cleanup)...")
            await do_end_call("shutdown")

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
        u = cs.usage
        print(f"\n USAGE (this call):")
        print(f"   STT audio     : {u.stt_audio_seconds:.2f} s")
        print(
            f"   LLM tokens    : {u.llm_input_tokens} in / "
            f"{u.llm_output_tokens} out",
        )
        print(f"   TTS audio     : {u.tts_audio_seconds:.2f} s")

    print("=" * 60)
    await MongoDB.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description="Synthora VoiceBot Continuous Mic Test",
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