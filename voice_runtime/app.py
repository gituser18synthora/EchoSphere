"""Voice worker — realtime call host, separate from the HTTP API process.

Run: env/bin/python -m voice_runtime.app

The module entry point reads VOICE_WORKER_HOST/VOICE_WORKER_PORT from .env and
passes them explicitly to Uvicorn.

Sessions are issued by the main API (POST /api/v1/voice-sessions) which writes
a trusted tenant/bot mapping into Redis; the browser/telephony client then
connects here with only the opaque session id. A failure inside one call is
contained to that call's pipeline task.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.websockets import WebSocketState

from shared.config import get_settings
from shared.db.mongo import Mongo
from shared.db.redis import redis_health_check
from shared.bot_config import resolve_bot_config
from shared.errors import install_error_handlers
from shared.voice_sessions import (
    end_voice_session,
    load_voice_session,
    update_voice_session,
)
from voice_runtime.recording import SessionRecorder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("voice_runtime.app")

# Fail fast on missing mandatory configuration before serving any call.
from shared.config import validate_settings  # noqa: E402

validate_settings("voice-runtime")

_active_sessions: dict[str, asyncio.Task] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await Mongo.connect()
    yield
    for task in list(_active_sessions.values()):
        task.cancel()
    await Mongo.disconnect()


app = FastAPI(title="EchoSphere Voice Worker", lifespan=lifespan)
install_error_handlers(app)


@app.get("/")
async def root():
    return {"service": "EchoSphere Voice Worker", "status": "up"}


@app.get("/health")
async def health():
    redis = await redis_health_check()
    return {
        "status": "up",
        "active_sessions": len(_active_sessions),
        "redis": redis,
    }


@app.post("/telephony/webhook/{provider}")
async def inbound_call_webhook(provider: str, request: Request):
    """Signed dialer webhook on the SAME host:port as the media WebSocket.

    External dialers (Vaani) get exactly one public endpoint pair:
    POST /telephony/webhook/{provider} here mints the session whose
    /ws/telephony/{provider}/{session_id} URL is returned in the response.
    Sessions live in Redis, so any worker instance can host the call.
    """
    from shared.telephony_webhooks import handle_inbound_call_webhook

    return await handle_inbound_call_webhook(provider, request)


@app.websocket("/ws/telephony/{provider}/{session_id}")
async def telephony_session(websocket: WebSocket, provider: str, session_id: str):
    """Provider media stream (Twilio/Telnyx/Plivo/Exotel/Vaani/FreeSWITCH).

    The session was issued by the signed inbound-call webhook, so the tenant/
    bot mapping is already trusted. Providers that send a JSON start message
    get a handshake read before the pipeline starts.
    """
    import json as _json

    from shared.errors import ApiError
    from shared.telephony import SUPPORTED_PROVIDERS
    from voice_runtime.telephony import build_media_serializer

    if provider not in SUPPORTED_PROVIDERS:
        await websocket.close(code=4404, reason="unknown provider")
        return
    session = await load_voice_session(session_id)
    if session is None:
        await websocket.close(code=4401, reason="unknown or expired session")
        return
    if session_id in _active_sessions:
        # Reject duplicates BEFORE the handshake read: a duplicate connection
        # that never sends `start` must not park inside receive_text() holding
        # a socket while the real stream is live. (_run_call re-checks.)
        await websocket.accept()
        await websocket.close(code=4409, reason="session already active")
        return
    await websocket.accept()

    start_message: dict | None = None
    if provider in ("twilio", "telnyx", "plivo", "exotel", "vaani"):
        # Read messages until the provider's stream-start event arrives. The
        # deadline bounds a client that connects and then goes silent.
        try:
            async with asyncio.timeout(10):
                for _ in range(4):
                    message = _json.loads(await websocket.receive_text())
                    event = message.get("event") or message.get("event_type")
                    if event in ("start", "streamStart", "media_start"):
                        start_message = message
                        break
        except Exception:  # noqa: BLE001 — timeout, disconnect or bad JSON
            await websocket.close(code=4400, reason="invalid stream handshake")
            return
        if start_message is None:
            await websocket.close(code=4400, reason="missing stream start message")
            return
    try:
        serializer = build_media_serializer(provider, start_message=start_message)
    except ApiError as exc:
        await websocket.close(code=4400, reason=exc.message[:100])
        return
    await _run_call(websocket, session_id, session, serializer=serializer,
                    telephony_provider=provider)


@app.websocket("/ws/voice/{session_id}")
async def voice_session(websocket: WebSocket, session_id: str):
    """One realtime call (browser test client). The session id is the trusted
    tenant/bot mapping issued by the API."""
    session = await load_voice_session(session_id)
    if session is None:
        await websocket.close(code=4401, reason="unknown or expired session")
        return
    await websocket.accept()
    await _run_call(websocket, session_id, session)


async def _run_call(
    websocket: WebSocket,
    session_id: str,
    session: dict,
    *,
    serializer=None,
    telephony_provider: str | None = None,
):
    settings = get_settings()
    if len(_active_sessions) >= settings.voice_worker_concurrency:
        await websocket.close(code=4429, reason="voice worker at capacity")
        return
    if session_id in _active_sessions:
        # A second live connection for the same session (telephony retry or
        # reconnect while the first socket is still up) would run a second
        # pipeline over the same call: duplicate greeting, duplicate STT/TTS
        # usage, duplicate billing. One session id, one media stream.
        logger.warning(
            "voice session %s already active — rejecting duplicate connection",
            session_id,
        )
        await websocket.close(code=4409, reason="session already active")
        return
    # Claim the session BEFORE any awaits so a concurrent connect can't slip
    # through between the check above and pipeline start.
    _active_sessions[session_id] = asyncio.current_task()

    await update_voice_session(session_id, status="connected")

    try:
        config = await resolve_bot_config(
            session["bot_id"],
            require_published=session.get("channel") != "browser",
        )
    except Exception:  # noqa: BLE001
        logger.exception("bot config resolution failed for %s", session_id)
        _active_sessions.pop(session_id, None)
        await websocket.close(code=4404, reason="bot configuration unavailable")
        return

    # Defense-in-depth: the session's tenant must match the bot's tenant.
    if session["tenant_id"] != config.tenant_id:
        logger.error("tenant mismatch for session %s", session_id)
        _active_sessions.pop(session_id, None)
        await websocket.close(code=4403, reason="forbidden")
        return

    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    from shared.knowledge.service import get_knowledge_service
    from shared.orchestration.workflow_engine import get_workflow_engine
    from voice_runtime.pipeline import build_voice_pipeline
    from voice_runtime.serializer import RawPCMSerializer

    recorder = SessionRecorder(
        session_id,
        config,
        channel=telephony_provider or session.get("channel", "browser"),
        caller=session.get("caller"),
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer or RawPCMSerializer(),
            session_timeout=settings.voice_session_timeout,
        ),
    )

    knowledge = None
    try:
        knowledge = get_knowledge_service()
    except Exception:  # noqa: BLE001 - voice must work without embeddings configured
        logger.warning("knowledge service unavailable — KB routing disabled")

    # Transport-aware audio formats: browser plays PCM at the configured
    # browser rate; telephony serializers expect the 8 kHz PSTN world.
    transport_kind = "telephony" if telephony_provider else "browser"
    audio_conf = (config.audio_settings or {}).get(transport_kind) or {}
    if telephony_provider:
        tts_sample_rate = int(audio_conf.get("sampleRate", 8000))
        stt_sample_rate = 8000
    else:
        tts_sample_rate = int(audio_conf.get("sampleRate", 24000))
        stt_sample_rate = 16000

    # Session parameters the browser test client needs BEFORE any audio:
    # the actual output sample rate (never assume a rate client-side) plus
    # readable voice/language info and configuration warnings for the UI.
    tts_conf = config.tts or {}
    voices = {
        locale: {
            "provider": engine.get("provider", ""),
            "voice": engine.get("voice_name") or engine.get("voice", ""),
        }
        for locale, engine in (tts_conf.get("language_map") or {}).items()
    }
    client_info = {
        "botName": config.bot_name,
        "sampleRate": tts_sample_rate,
        "language": config.language,
        "languages": config.languages or [config.language],
        "voices": voices,
        "defaultVoice": {
            "provider": tts_conf.get("provider", ""),
            "voice": tts_conf.get("voice_name") or tts_conf.get("voice", ""),
        },
        "warnings": config.language_warnings or {},
    }

    try:
        worker, brain = build_voice_pipeline(
            transport=transport,
            config=config,
            recorder=recorder,
            knowledge_service=knowledge,
            workflow_engine=get_workflow_engine(),
            tts_sample_rate=tts_sample_rate,
            stt_sample_rate=stt_sample_rate,
            idle_timeout_secs=float(config.silence_timeout) * 4,
            client_info=client_info,
            call_context=session.get("variables") or None,
            transport_kind=transport_kind,
        )
    except Exception:  # noqa: BLE001 — misconfigured providers must not crash the worker
        logger.exception("pipeline construction failed for %s", session_id)
        await recorder.flush_event("pipeline_build_failed")
        _active_sessions.pop(session_id, None)
        await websocket.close(code=4500, reason="voice engine configuration error")
        return

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        await recorder.flush_event(
            "call_started",
            channel=recorder.channel,
            call_id=session.get("call_id"),
        )
        await brain.speak_greeting()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await worker.cancel()

    @transport.event_handler("on_session_timeout")
    async def on_session_timeout(transport, client):
        await recorder.flush_event("session_timeout")
        await worker.cancel()

    runner = PipelineRunner(handle_sigint=False)
    # The session was already claimed in _active_sessions at connection time.
    max_duration_handle = asyncio.get_running_loop().call_later(
        config.max_call_duration, lambda: asyncio.ensure_future(worker.cancel())
    )
    try:
        await runner.run(worker)
        await recorder.finalize(reason="completed")
    except asyncio.CancelledError:
        await recorder.finalize(reason="worker_shutdown")
        raise
    except Exception:  # noqa: BLE001 - one call must not take down the worker
        logger.exception("voice session %s crashed", session_id)
        await recorder.finalize(reason="error")
    finally:
        max_duration_handle.cancel()
        _active_sessions.pop(session_id, None)
        await end_voice_session(session_id)
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close()
        logger.info("voice session %s ended (turns=%d)", session_id, len(recorder.turns))


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.voice_worker_host,
        port=settings.voice_worker_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
