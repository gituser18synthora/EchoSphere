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

# Provider SDK errors can embed request headers (API keys) in exception text
# — scrub every std-logging and loguru (pipecat) line before it reaches
# journald. Observed live 2026-07-29: sarvamai's 403 error printed the full
# api-subscription-key.
from shared.logging_utils import install_log_redaction  # noqa: E402

install_log_redaction()

# Fail fast on missing mandatory configuration before serving any call.
from shared.config import validate_settings  # noqa: E402

validate_settings("voice-runtime")

_active_sessions: dict[str, asyncio.Task] = {}

# Every telephony media serializer speaks the 8 kHz PSTN world: the
# FreeSWITCH streamAudio envelope declares sampleRate 8000, Vaani's contract
# is L16@8k, and the pipecat Twilio/Telnyx/Plivo/Exotel serializers encode
# G.711-rate audio. There is exactly ONE correct pipeline output rate.
TELEPHONY_SAMPLE_RATE = 8000


def resolve_telephony_sample_rate(
    audio_conf: dict, *, bot_id: str = "?", provider: str = "?"
) -> int:
    """Telephony output rate, clamped to the serializers' 8 kHz contract.

    A bot configured with any other telephony rate would generate PCM at that
    rate while the wire envelope still declares 8000 — played at the wrong
    speed on the caller's phone (16k config = half speed). Configuration must
    never be able to produce that, so it is forced with a loud warning.
    """
    configured = int(audio_conf.get("sampleRate", TELEPHONY_SAMPLE_RATE))
    if configured != TELEPHONY_SAMPLE_RATE:
        logger.warning(
            "bot %s telephony audio_settings.sampleRate=%d is not supported by "
            "the %s media stream (fixed L16@8k) — forcing %d so playback speed "
            "stays correct",
            bot_id, configured, provider, TELEPHONY_SAMPLE_RATE,
        )
    return TELEPHONY_SAMPLE_RATE


def session_timeout_should_cancel(recorder) -> bool:
    """Whether the pipecat session timeout may cancel this call.

    Pipecat's ``session_timeout`` is an ABSOLUTE one-shot timer from
    connection start — not an inactivity timeout. Killing an in-progress
    conversation at exactly VOICE_SESSION_TIMEOUT seconds is a mid-call
    disconnect, so the timer is only honored for sessions that never became
    a call (no greeting, no turns): those are dead sockets worth reaping.
    Live calls stay bounded by ``max_call_duration``.
    """
    return len(getattr(recorder, "turns", []) or []) == 0


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

    if provider not in SUPPORTED_PROVIDERS:
        logger.warning(
            "telephony ws rejected (4404 unknown provider %r) session=%s",
            provider, session_id,
        )
        await websocket.close(code=4404, reason="unknown provider")
        return
    session = await load_voice_session(session_id)
    if session is None:
        logger.warning(
            "telephony ws rejected (4401 unknown/expired session) provider=%s "
            "session=%s", provider, session_id,
        )
        await websocket.close(code=4401, reason="unknown or expired session")
        return
    if session_id in _active_sessions:
        # Reject duplicates BEFORE the handshake read: a duplicate connection
        # that never sends `start` must not park inside receive_text() holding
        # a socket while the real stream is live. (_run_call re-checks.)
        logger.warning(
            "telephony ws rejected (4409 duplicate connection) provider=%s "
            "session=%s", provider, session_id,
        )
        await websocket.accept()
        await websocket.close(code=4409, reason="session already active")
        return
    await websocket.accept()
    peer = websocket.client.host if websocket.client else "?"
    logger.info(
        "telephony ws connected: provider=%s session=%s call_id=%s peer=%s",
        provider, session_id, session.get("call_id"), peer,
    )

    start_message: dict | None = None
    if provider in ("twilio", "telnyx", "plivo", "exotel", "vaani"):
        # Read messages until the provider's stream-start event arrives. The
        # deadline bounds a client that connects and then goes silent.
        try:
            async with asyncio.timeout(10):
                for _ in range(4):
                    message = _json.loads(await websocket.receive_text())
                    event = message.get("event") or message.get("event_type")
                    logger.info(
                        "telephony ws handshake event: provider=%s session=%s "
                        "event=%s", provider, session_id, event,
                    )
                    if event in ("start", "streamStart", "media_start"):
                        start_message = message
                        break
        except Exception:  # noqa: BLE001 — timeout, disconnect or bad JSON
            logger.warning(
                "telephony ws handshake failed (4400) provider=%s session=%s",
                provider, session_id,
            )
            await websocket.close(code=4400, reason="invalid stream handshake")
            return
        if start_message is None:
            logger.warning(
                "telephony ws missing stream start (4400) provider=%s session=%s",
                provider, session_id,
            )
            await websocket.close(code=4400, reason="missing stream start message")
            return
        start_body = start_message.get("start") or {}
        logger.info(
            "telephony stream start: provider=%s session=%s streamSid=%s "
            "mediaFormat=%s track=%s",
            provider, session_id,
            start_body.get("streamSid") or start_message.get("streamSid")
            or start_body.get("stream_sid") or start_body.get("streamId")
            or start_body.get("stream_id"),
            start_body.get("mediaFormat") or start_body.get("media_format"),
            start_body.get("track"),
        )
    # Importing Pipecat and its serializers can take longer than the
    # mod_audio_stream WebSocket handshake timeout on the first call after a
    # process restart. The socket must already be accepted before that work.
    import inspect

    from voice_runtime.telephony import build_media_serializer

    factory_kwargs: dict = {"start_message": start_message}
    if "session_id" in inspect.signature(build_media_serializer).parameters:
        factory_kwargs["session_id"] = session_id  # tags media-stage log lines
    try:
        serializer = build_media_serializer(provider, **factory_kwargs)
    except ApiError as exc:
        logger.warning(
            "telephony serializer rejected (4400) provider=%s session=%s: %s",
            provider, session_id, exc.message,
        )
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
        logger.warning(
            "voice session %s rejected (4429): worker at capacity (%d active)",
            session_id, len(_active_sessions),
        )
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
        tts_sample_rate = resolve_telephony_sample_rate(
            audio_conf, bot_id=session["bot_id"], provider=telephony_provider
        )
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
        logger.info(
            "call started: session=%s channel=%s call_id=%s — speaking greeting",
            session_id, recorder.channel, session.get("call_id"),
        )
        await recorder.flush_event(
            "call_started",
            channel=recorder.channel,
            call_id=session.get("call_id"),
        )
        await brain.speak_greeting()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(
            "client disconnected: session=%s — cancelling pipeline", session_id
        )
        await worker.cancel()

    @transport.event_handler("on_session_timeout")
    async def on_session_timeout(transport, client):
        if not session_timeout_should_cancel(recorder):
            # Absolute timer fired mid-conversation — never a reason to
            # drop a live call (max_call_duration still bounds it).
            logger.info(
                "session timeout timer fired for ACTIVE session=%s "
                "(turns=%d) — ignoring; call stays up",
                session_id, len(recorder.turns),
            )
            await recorder.flush_event("session_timeout_ignored_active")
            return
        logger.info("session timeout: session=%s — cancelling pipeline", session_id)
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
