"""Call-level policies in the voice worker app.

WebSocket shutdown must be idempotent: the pipecat transport usually sends
the close frame itself (client disconnect, worker cancellation, normal end),
and Starlette raises ``RuntimeError: Cannot call "send" once a close message
has been sent`` on a second close. Every shutdown path in the app goes
through ``_close_websocket``, which must close at most once and never raise.
"""

from fastapi.websockets import WebSocketState

from voice_runtime.app import _close_websocket


class _FakeWebSocket:
    """Mimics Starlette's close-state machine."""

    def __init__(self, application_state=WebSocketState.CONNECTED):
        self.application_state = application_state
        self.close_calls: list[tuple[int, str]] = []

    async def close(self, code=1000, reason=""):
        if self.application_state == WebSocketState.DISCONNECTED:
            raise RuntimeError('Cannot call "send" once a close message has been sent.')
        self.application_state = WebSocketState.DISCONNECTED
        self.close_calls.append((code, reason))


class _ExplodingWebSocket(_FakeWebSocket):
    """Transport died mid-close (client vanished): close always raises."""

    async def close(self, code=1000, reason=""):
        raise RuntimeError('Cannot call "send" once a close message has been sent.')


class TestIdempotentClose:
    async def test_closes_once_with_code_and_reason(self):
        ws = _FakeWebSocket()
        await _close_websocket(ws, code=4401, reason="unknown or expired session")
        assert ws.close_calls == [(4401, "unknown or expired session")]
        assert ws.application_state == WebSocketState.DISCONNECTED

    async def test_second_close_is_a_noop(self):
        # The disconnect handler, the pipeline teardown and the _run_call
        # finally block may all reach close — only the first may send.
        ws = _FakeWebSocket()
        await _close_websocket(ws)
        await _close_websocket(ws)
        await _close_websocket(ws, code=4500, reason="late error path")
        assert len(ws.close_calls) == 1

    async def test_skips_when_transport_already_closed(self):
        # Pipecat's FastAPI transport closed the socket during the call.
        ws = _FakeWebSocket(application_state=WebSocketState.DISCONNECTED)
        await _close_websocket(ws)
        assert ws.close_calls == []

    async def test_close_race_never_raises(self):
        # State says CONNECTED but the close frame is already on the wire
        # (the exact race behind the production RuntimeError).
        ws = _ExplodingWebSocket()
        await _close_websocket(ws)  # must not raise

    async def test_pre_accept_denial_still_closes(self):
        # Session rejection happens before accept(); the handshake denial
        # close must still go out.
        ws = _FakeWebSocket(application_state=WebSocketState.CONNECTING)
        await _close_websocket(ws, code=4404, reason="unknown provider")
        assert ws.close_calls == [(4404, "unknown provider")]
