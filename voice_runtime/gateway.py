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


def main() -> None:
    import uvicorn

    from shared.config import get_settings

    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.telephony_gateway_host,
        port=settings.telephony_gateway_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
