"""Telephony gateway — the voice-worker app bound to the public dialer port.

Run alongside the regular worker (``python -m voice_runtime.app``, port 9002):

    env/bin/python -m voice_runtime.gateway    # TELEPHONY_GATEWAY_PORT (9011)

Dialers (Vaani) then reach ONE public host:port for both surfaces:

    POST http://<host>:9011/telephony/webhook/vaani
    ws://<host>:9011/ws/telephony/vaani/{session_id}

Voice sessions are handed off through Redis, so the gateway serves its own
webhook-minted sessions while browser sessions keep using the 9002 worker.
Set TELEPHONY_PUBLIC_WS_BASE to this instance's public base (e.g.
``ws://192.168.60.123:9011``) so the webhook answers with a URL the dialer
can actually reach.
"""

from voice_runtime.app import app  # noqa: F401  (uvicorn factory target)


def _preload_call_runtime() -> None:
    """Load heavy call-path modules before accepting telephony sockets.

    ``mod_audio_stream`` has a short connection tolerance. Importing Pipecat,
    provider factories and the transport stack lazily on the first call can
    block the event loop for several seconds and make that socket disconnect.
    """
    from pipecat.pipeline.runner import PipelineRunner  # noqa: F401
    from pipecat.transports.websocket.fastapi import (  # noqa: F401
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    from shared.knowledge.service import get_knowledge_service  # noqa: F401
    from shared.orchestration.workflow_engine import get_workflow_engine  # noqa: F401
    from voice_runtime.pipeline import build_voice_pipeline  # noqa: F401
    from voice_runtime.serializer import RawPCMSerializer  # noqa: F401
    from voice_runtime.telephony import build_media_serializer  # noqa: F401


def main() -> None:
    import uvicorn

    from shared.config import get_settings

    _preload_call_runtime()
    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.telephony_gateway_host,
        port=settings.telephony_gateway_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
