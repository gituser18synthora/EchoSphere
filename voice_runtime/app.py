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
import time
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
# Mandatory secret-leakage guardrail at the logging boundary: no handler
# (std logging or pipecat/loguru) can write a provider credential.
from shared.logging_utils import install_log_redaction  # noqa: E402

install_log_redaction()
logger = logging.getLogger("voice_runtime.app")

# Fail fast on missing mandatory configuration before serving any call.
from shared.config import validate_settings  # noqa: E402

validate_settings("voice-runtime")

_active_sessions: dict[str, asyncio.Task] = {}


async def _close_websocket(
    websocket: WebSocket, code: int = 1000, reason: str | None = None
) -> None:
    """Idempotent WebSocket close — safe on every shutdown path.

    The pipecat transport owns the socket during the call and usually sends
    the close frame itself (client disconnect, cancellation, worker end).
    Starlette raises ``RuntimeError: Cannot call "send" once a close message
    has been sent`` on a second close, so this checks the server-side state
    first and swallows the race where the transport closes in between.
    """
    if websocket.application_state == WebSocketState.DISCONNECTED:
        return
    try:
        await websocket.close(code=code, reason=reason or "")
    except RuntimeError:
        # Close frame already sent (or the client vanished mid-close).
        pass
    except Exception:  # noqa: BLE001 — closing must never raise into teardown
        logger.debug("websocket close failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await Mongo.connect()
    # Post-call intelligence worker (summary / outcome / Next Best Action):
    # the durable queue is the conversation_memories table, so an embedded
    # poller per process is safe (single-row optimistic claims) and a restart
    # loses nothing.
    post_call_stop: asyncio.Event | None = None
    post_call_task: asyncio.Task | None = None
    if get_settings().post_call_worker_embedded:
        from shared.post_call.processor import run_worker as run_post_call_worker

        post_call_stop = asyncio.Event()
        post_call_task = asyncio.create_task(run_post_call_worker(post_call_stop))
    yield
    for task in list(_active_sessions.values()):
        task.cancel()
    if post_call_task is not None and post_call_stop is not None:
        post_call_stop.set()
        try:
            await asyncio.wait_for(post_call_task, timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            post_call_task.cancel()
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
        await _close_websocket(websocket, code=4404, reason="unknown provider")
        return
    session = await load_voice_session(session_id)
    if session is None:
        await _close_websocket(websocket, code=4401, reason="unknown or expired session")
        return
    if session_id in _active_sessions:
        # Reject duplicates BEFORE the handshake read: a duplicate connection
        # that never sends `start` must not park inside receive_text() holding
        # a socket while the real stream is live. (_run_call re-checks.)
        await websocket.accept()
        await _close_websocket(websocket, code=4409, reason="session already active")
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
            await _close_websocket(websocket, code=4400, reason="invalid stream handshake")
            return
        if start_message is None:
            await _close_websocket(websocket, code=4400, reason="missing stream start message")
            return
    # Importing Pipecat and its serializers can take longer than the
    # mod_audio_stream WebSocket handshake timeout on the first call after a
    # process restart. The socket must already be accepted before that work.
    from voice_runtime.telephony import build_media_serializer

    try:
        transport = websocket.query_params.get("transport")
        serializer = build_media_serializer(
            provider,
            start_message=start_message,
            transport=transport,
        )
    except ApiError as exc:
        await _close_websocket(websocket, code=4400, reason=exc.message[:100])
        return
    await _run_call(
        websocket,
        session_id,
        session,
        serializer=serializer,
        telephony_provider=provider,
        media_transport=transport,
    )


@app.websocket("/ws/voice/{session_id}")
async def voice_session(websocket: WebSocket, session_id: str):
    """One realtime call (browser test client). The session id is the trusted
    tenant/bot mapping issued by the API."""
    session = await load_voice_session(session_id)
    if session is None:
        await _close_websocket(websocket, code=4401, reason="unknown or expired session")
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
    media_transport: str | None = None,
):
    settings = get_settings()
    if len(_active_sessions) >= settings.voice_worker_concurrency:
        await _close_websocket(websocket, code=4429, reason="voice worker at capacity")
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
        await _close_websocket(websocket, code=4409, reason="session already active")
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
        await _close_websocket(websocket, code=4404, reason="bot configuration unavailable")
        return

    # Defense-in-depth: the session's tenant must match the bot's tenant.
    if session["tenant_id"] != config.tenant_id:
        logger.error("tenant mismatch for session %s", session_id)
        _active_sessions.pop(session_id, None)
        await _close_websocket(websocket, code=4403, reason="forbidden")
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

    # Effective guardrails for this bot (mandatory platform rules ∪ the bot's
    # explicit profile, else the tenant default), plus the tenant's ACTIVE
    # compliance policies, resolved server-side at call start. The guardrail
    # loader FAILS CLOSED onto the built-in mandatory floor, so a
    # control-plane outage can never run a call without PII/secret/tool/
    # injection protection. Hits are buffered onto the recorder as they
    # happen and the trigger ledger is persisted at finalize.
    from shared.compliance import (
        check_calling_window,
        load_active_policies_sync,
        record_policy_trigger_sync,
    )
    from shared.guardrails import (
        GuardrailEngine,
        load_effective_guardrails_sync,
        register_session_engine,
        release_session_engine,
    )

    # Independent control-plane reads; loading them serially added a full DB
    # round trip to every call's time-to-greeting.
    effective_guardrails, compliance_policies = await asyncio.gather(
        asyncio.to_thread(
            load_effective_guardrails_sync, config.tenant_id, config.bot_id
        ),
        asyncio.to_thread(load_active_policies_sync, config.tenant_id),
    )

    # Second deterministic checkpoint (the webhook already refused before the
    # session was minted; this covers every other path to a live telephony
    # pipeline): a development profile that disables telephony, or an active
    # policy's calling window, closes the socket before any audio.
    if telephony_provider:
        blocked_reason = None
        blocking_policy = None
        if effective_guardrails.has("outbound_call_block"):
            blocked_reason = ("outbound_call_block",
                             "telephony disabled by the bot's guardrail profile")
        else:
            for policy in compliance_policies:
                if not policy.applies(
                    channel="phone",
                    direction=policy.effective_direction(None),
                ):
                    continue
                decision = check_calling_window(policy)
                if not decision.allowed:
                    blocked_reason = (
                        "calling_window",
                        f"{decision.reason} (local {decision.local_time})",
                    )
                    blocking_policy = policy
                    break
        if blocked_reason is not None:
            rule, detail = blocked_reason
            await recorder.flush_event(
                "call_blocked_by_policy", rule=rule, detail=detail,
            )
            await asyncio.to_thread(
                record_policy_trigger_sync,
                tenant_id=config.tenant_id, bot_id=config.bot_id,
                session_id=session_id, rule=rule, action="block",
                stage="call", outcome="blocked", policy=blocking_policy,
                channel=recorder.channel, detail=detail,
            )
            _active_sessions.pop(session_id, None)
            await _close_websocket(websocket, code=4403, reason="call not permitted")
            return

    def _on_guardrail_hit(hit) -> None:
        recorder.add_event(
            "guardrail_trigger",
            code=hit.rule.code,
            action=hit.action,
            stage=hit.stage,
            detail=hit.detail,
            policy_code=hit.policy_code,
            policy_version=hit.policy_version,
            outcome=hit.outcome,
        )

    guardrails = GuardrailEngine(
        effective_guardrails, on_hit=_on_guardrail_hit,
        compliance=compliance_policies,
    )
    recorder.guardrails = guardrails
    # Deep call sites that only know the session id (workflow api nodes →
    # ToolExecutor) resolve this engine from the registry.
    register_session_engine(session_id, guardrails)
    await recorder.flush_event(
        "guardrails_loaded",
        profile_id=effective_guardrails.profile_id,
        profile_code=effective_guardrails.profile_code,
        profile_version=effective_guardrails.profile_version,
        rules=[r.code for r in effective_guardrails.rules],
        policies=[f"{p.code}@v{p.version}" for p in compliance_policies],
        degraded=effective_guardrails.degraded,
    )

    # Runtime context: the server-trusted user/customer details this call
    # runs against. The GENERIC path (tenant-defined schema: User Details
    # API, manual test JSON, or stored records — any domain) is tried first;
    # bots without a schema fall back to the legacy loan-collection table so
    # existing behavior is preserved bit-for-bit. Both paths are bounded and
    # fail-open — a lookup failure degrades the call, never blocks it.
    from shared.customer_context import load_customer_context
    from shared.runtime_context import load_runtime_context

    session_variables = session.get("variables") or {}
    runtime_context = await load_runtime_context(
        config.tenant_id,
        config.bot_id,
        phone=session.get("caller"),
        record_id=(
            session.get("customer_context_id")
            or session_variables.get("customer_context_id")
        ),
        session_variables=session_variables,
        system_values={
            "call_channel": telephony_provider or session.get("channel", "browser"),
            "bot_language": config.language,
        },
    )
    customer_context = None
    if runtime_context is None:
        customer_context = await load_customer_context(
            config.tenant_id,
            config.bot_id,
            context_id=(
                session.get("customer_context_id")
                or session_variables.get("customer_context_id")
            ),
            phone=session.get("caller"),
        )
    if customer_context is not None:
        recorder.customer_context_id = customer_context.context_id
        await recorder.flush_event(
            "customer_context_loaded",
            context_id=customer_context.context_id,
            customer_verified=customer_context.customer_verified,
            account_disputed=customer_context.account_disputed,
            complaint_pending=customer_context.complaint_pending,
            payment_status=customer_context.payment_status,
        )
    elif runtime_context is not None:
        recorder.runtime_context_record_id = runtime_context.record_id
        await recorder.flush_event(
            "runtime_context_loaded",
            schema_id=runtime_context.schema_id,
            record_id=runtime_context.record_id,
            source_mode=runtime_context.source_mode,
            domain_policy=runtime_context.domain_policy,
            values=len(runtime_context.values),
            load_error=runtime_context.load_error,
        )

    # Previous conversation memory: the customer's latest analyzed call for
    # THIS tenant+bot, resolved exactly the way the customer themselves was
    # (context record → legacy context → phone tail). Works for every
    # direction combination — inbound and outbound both land here. Gated on
    # the tenant's use_previous_call_summary switch, enforced inside
    # load_previous_memory so stored history is never injected without the
    # tenant's explicit opt-in. Bounded and fail-open: an immediately
    # recalled customer whose previous call is still being summarized simply
    # gets the memory before it, or none.
    previous_memory = None
    try:
        from shared.post_call.recall import load_previous_memory

        previous_memory = await load_previous_memory(
            config.tenant_id,
            config.bot_id,
            runtime_context_record_id=recorder.runtime_context_record_id,
            customer_context_id=recorder.customer_context_id,
            phone=session.get("caller"),
            exclude_session_id=session_id,
        )
    except Exception:  # noqa: BLE001 — memory must never block a call
        logger.warning("previous-memory load failed for %s", session_id, exc_info=True)
    if previous_memory is not None:
        await recorder.flush_event(
            "previous_memory_loaded",
            previous_memory_source_conversation_id=previous_memory.conversation_id,
            outcome=previous_memory.call_outcome,
            next_best_action_type=previous_memory.next_action,
            matched_by=previous_memory.matched_by,
            memory_status=previous_memory.status,
        )

    media_serializer = serializer or RawPCMSerializer()
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=media_serializer,
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
        # mod_audio_fork resamples the caller/read stream to 16 kHz. This also
        # satisfies the Sarvam SDK's per-message audio contract. The legacy
        # mod_audio_stream path remains 8 kHz.
        stt_sample_rate = (
            16000
            if telephony_provider == "freeswitch"
            and media_transport == "audio_fork"
            else 8000
        )
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
            "gender": engine.get("voice_gender", "neutral"),
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
            "gender": tts_conf.get("voice_gender", "neutral"),
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
            call_context=session_variables or None,
            customer_context=customer_context,
            runtime_context=runtime_context,
            transport_kind=transport_kind,
            previous_memory=previous_memory,
            guardrails=guardrails,
        )
    except Exception:  # noqa: BLE001 — misconfigured providers must not crash the worker
        logger.exception("pipeline construction failed for %s", session_id)
        await recorder.flush_event("pipeline_build_failed")
        release_session_engine(session_id)
        _active_sessions.pop(session_id, None)
        await _close_websocket(websocket, code=4500, reason="voice engine configuration error")
        return

    # FreeSWITCH call control: the fork serializer turns the brain's
    # telephony_control transfer into the module's transfer message. The hook
    # below fires at that exact moment — it records the request, marks the
    # session transferred (so teardown neither kills the now-agent-owned
    # channel nor reports a generic shutdown), and starts the ESL transfer
    # monitor that follows the dialplan's bridge/hangup events.
    call_uuid = session.get("call_id")
    if telephony_provider == "freeswitch" and serializer is not None:
        from voice_runtime.freeswitch import start_transfer_monitor

        async def _on_freeswitch_control(message: dict) -> None:
            if message.get("event") != "transfer":
                return
            recorder.transferred = True
            # Background persistence: the hook runs inside serialize(), so a
            # degraded Mongo must never delay the transfer message itself.
            recorder.flush_event_soon(
                "transfer_requested",
                reason=message.get("reason") or "transfer",
                call_uuid=call_uuid,
                transport=media_transport or "audio_stream",
            )
            start_transfer_monitor(
                session_id=session_id, call_uuid=call_uuid, recorder=recorder
            )

        serializer.on_telephony_control = _on_freeswitch_control

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
        # The media peer (FreeSWITCH/dialer/browser) closed the socket first:
        # caller hangup or provider-side teardown. Recorded so an unexpected
        # end can be attributed without guessing.
        recorder.flush_event_soon("client_disconnected")
        await worker.cancel()

    @transport.event_handler("on_session_timeout")
    async def on_session_timeout(transport, client):
        # Pipecat's session_timeout is ABSOLUTE call age, not inactivity: it
        # fires once, voice_session_timeout seconds after connect, even while
        # audio is streaming both ways. Cutting a live conversation at that
        # mark presented as "the call just disconnected"; max_call_duration
        # already bounds runaway calls, so only an abandoned socket (no
        # caller media for 60s) is ended here.
        last_media = getattr(media_serializer, "last_media_at", 0.0) or 0.0
        if last_media and time.monotonic() - last_media < 60.0:
            await recorder.flush_event("session_timeout_ignored_active_media")
            logger.info(
                "voice session %s passed the session timer with live media — "
                "keeping the call (bounded by max_call_duration)", session_id,
            )
            return
        await recorder.flush_event("session_timeout")
        await worker.cancel()

    runner = PipelineRunner(handle_sigint=False)
    # The session was already claimed in _active_sessions at connection time.
    max_duration_handle = asyncio.get_running_loop().call_later(
        config.max_call_duration, lambda: asyncio.ensure_future(worker.cancel())
    )
    try:
        await runner.run(worker)
        await recorder.finalize(
            reason="transferred" if recorder.transferred else "completed"
        )
    except asyncio.CancelledError:
        await recorder.finalize(
            reason="transferred" if recorder.transferred else "worker_shutdown"
        )
        raise
    except Exception:  # noqa: BLE001 - one call must not take down the worker
        logger.exception("voice session %s crashed", session_id)
        await recorder.finalize(reason="error")
    finally:
        max_duration_handle.cancel()
        if telephony_provider == "freeswitch" and not recorder.transferred:
            # The bot ending the call must end the PSTN leg too — the
            # dialplan script cannot see the media socket close, and a
            # transferred caller now belongs to the agent (never killed).
            # Best-effort and silent when the caller already hung up.
            from voice_runtime.freeswitch import hangup_channel_soon

            hangup_channel_soon(call_uuid, session_id=session_id)
        _active_sessions.pop(session_id, None)
        release_session_engine(session_id)
        await end_voice_session(session_id)
        # _close_websocket is idempotent: it no-ops when the transport (or a
        # client disconnect) already completed the close handshake.
        await _close_websocket(websocket)
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
