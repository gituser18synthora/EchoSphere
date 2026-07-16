"""Voice worker — realtime call host, separate from the HTTP API process.

Run: uvicorn backend.voice_worker:app --port 8015

Sessions are issued by the main API (POST /api/v1/voice-sessions) which writes
a trusted tenant/bot mapping into Redis; the browser/telephony client then
connects here with only the opaque session id. A failure inside one call is
contained to that call's pipeline task.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.websockets import WebSocketState

from backend.config import get_settings
from backend.db.mongo import Mongo
from backend.db.redis import redis_health_check
from backend.voice_runtime.bot_config import resolve_bot_config
from backend.voice_runtime.session import (
    SessionRecorder,
    end_voice_session,
    load_voice_session,
    update_voice_session,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("backend.voice_worker")

_active_sessions: dict[str, asyncio.Task] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await Mongo.connect()
    yield
    for task in list(_active_sessions.values()):
        task.cancel()
    await Mongo.disconnect()


app = FastAPI(title="EchoSphere Voice Worker", lifespan=lifespan)


@app.get("/health")
async def health():
    redis = await redis_health_check()
    return {
        "status": "up",
        "active_sessions": len(_active_sessions),
        "redis": redis,
    }


@app.websocket("/ws/voice/{session_id}")
async def voice_session(websocket: WebSocket, session_id: str):
    """One realtime call. The session id is the trusted tenant/bot mapping."""
    settings = get_settings()

    session = await load_voice_session(session_id)
    if session is None:
        await websocket.close(code=4401, reason="unknown or expired session")
        return
    if len(_active_sessions) >= settings.voice_worker_concurrency:
        await websocket.close(code=4429, reason="voice worker at capacity")
        return

    await websocket.accept()
    await update_voice_session(session_id, status="connected")

    try:
        config = await resolve_bot_config(
            session["bot_id"],
            require_published=session.get("channel") != "browser",
        )
    except Exception:  # noqa: BLE001
        logger.exception("bot config resolution failed for %s", session_id)
        await websocket.close(code=4404, reason="bot configuration unavailable")
        return

    # Defense-in-depth: the session's tenant must match the bot's tenant.
    if session["tenant_id"] != config.tenant_id:
        logger.error("tenant mismatch for session %s", session_id)
        await websocket.close(code=4403, reason="forbidden")
        return

    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    from backend.knowledge.service import get_knowledge_service
    from backend.orchestration.workflow_engine import get_workflow_engine
    from backend.voice_runtime.pipeline import build_voice_pipeline
    from backend.voice_runtime.serializer import RawPCMSerializer

    recorder = SessionRecorder(
        session_id,
        config,
        channel=session.get("channel", "browser"),
        caller=session.get("caller"),
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=RawPCMSerializer(),
            session_timeout=settings.voice_session_timeout,
        ),
    )

    knowledge = None
    try:
        knowledge = get_knowledge_service()
    except Exception:  # noqa: BLE001 - voice must work without embeddings configured
        logger.warning("knowledge service unavailable — KB routing disabled")

    worker, brain = build_voice_pipeline(
        transport=transport,
        config=config,
        recorder=recorder,
        knowledge_service=knowledge,
        workflow_engine=get_workflow_engine(),
        idle_timeout_secs=float(config.silence_timeout) * 4,
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        await recorder.flush_event("call_started", channel=recorder.channel)
        await brain.speak_greeting()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await worker.cancel()

    @transport.event_handler("on_session_timeout")
    async def on_session_timeout(transport, client):
        await recorder.flush_event("session_timeout")
        await worker.cancel()

    runner = PipelineRunner(handle_sigint=False)
    run_task = asyncio.current_task()
    _active_sessions[session_id] = run_task

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
