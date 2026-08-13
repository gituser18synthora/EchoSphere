"""Call-lifecycle stability regressions.

Covers the non-router halves of the 2026-08-13 "call drops / stalls after a
couple of turns" incident:

- ``to_thread_abandonable``: a barge-in's cancel must not wait out a tool's
  HTTP timeout (observed live as ``_handle_turn: timed out waiting for task
  to cancel``);
- serializer ``last_media_at``: the session host distinguishes a live call
  from an abandoned socket when pipecat's ABSOLUTE session timer fires;
- Sarvam STT mid-call reconnect: the upstream service has no reconnect, so a
  server-closed socket silently deafened the bot for the rest of the call;
- backchannel latches: a hangup mid-backchannel must not leave
  ``_backchannel_active`` (and the audio gate's echo shield) stuck on.
"""

import asyncio
import threading
import time

import pytest

from shared.orchestration.async_tools import to_thread_abandonable


class TestToThreadAbandonable:
    async def test_result_and_exception_passthrough(self):
        assert await to_thread_abandonable(lambda: 42) == 42
        with pytest.raises(ValueError):
            await to_thread_abandonable(self._raise)

    @staticmethod
    def _raise():
        raise ValueError("boom")

    async def test_cancellation_does_not_wait_for_the_thread(self):
        started = threading.Event()
        release = threading.Event()

        def blocking():
            started.set()
            release.wait(5.0)
            return "late"

        task = asyncio.create_task(to_thread_abandonable(blocking))
        await asyncio.to_thread(started.wait, 2.0)
        cancel_at = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The old plain to_thread await stalled here until the thread ended
        # (up to the tool's full HTTP timeout).
        assert time.monotonic() - cancel_at < 0.5
        release.set()


class TestSerializerMediaActivity:
    async def test_fork_serializer_stamps_inbound_media(self):
        from voice_runtime.telephony import FreeSwitchAudioForkSerializer

        serializer = FreeSwitchAudioForkSerializer()
        assert serializer.last_media_at == 0.0
        frame = await serializer.deserialize(b"\x01\x02" * 160)
        assert frame is not None
        assert serializer.last_media_at > 0.0

    async def test_fork_serializer_ignores_text_for_activity(self):
        from voice_runtime.telephony import FreeSwitchAudioForkSerializer

        serializer = FreeSwitchAudioForkSerializer()
        await serializer.deserialize('{"type": "metadata"}')
        assert serializer.last_media_at == 0.0

    async def test_browser_serializer_stamps_inbound_media(self):
        from voice_runtime.serializer import RawPCMSerializer

        serializer = RawPCMSerializer()
        assert serializer.last_media_at == 0.0
        await serializer.deserialize(b"\x01\x02" * 160)
        assert serializer.last_media_at > 0.0


class _ReconnectRecorder:
    def __init__(self):
        self.events = []

    def add_event(self, kind, **data):
        self.events.append({"kind": kind, **data})


class TestSarvamSttReconnect:
    def _service(self):
        from voice_runtime.sarvam_stt import EndpointedSarvamSTTService

        svc = EndpointedSarvamSTTService.__new__(EndpointedSarvamSTTService)
        svc._stt_stopping = False
        svc._reconnect_attempts = 0
        svc._recorder = _ReconnectRecorder()
        svc._receive_task = None
        svc.calls = []

        class _DeadClient:
            async def start_listening(self):
                return None  # the server closed; listening just ends

        class _Context:
            def __init__(self, owner):
                self._owner = owner

            async def __aexit__(self, *exc):
                self._owner.calls.append("context_exit")

        svc._socket_client = _DeadClient()
        svc._websocket_context = _Context(svc)

        async def _cancel_keepalive():
            svc.calls.append("keepalive_cancelled")

        async def _connect():
            svc.calls.append("connect")

        async def _push_error(error_msg=None, **kwargs):
            svc.calls.append(("push_error", error_msg))

        svc._cancel_keepalive_task = _cancel_keepalive
        svc._connect = _connect
        svc.push_error = _push_error
        return svc

    async def test_dead_socket_triggers_one_reconnect(self):
        svc = self._service()
        await svc._receive_task_handler()
        assert "context_exit" in svc.calls
        assert "keepalive_cancelled" in svc.calls
        assert svc.calls[-1] == "connect"
        assert svc._socket_client is None  # run_stt stops writing immediately
        assert svc._reconnect_attempts == 1
        assert [e["kind"] for e in svc._recorder.events] == ["stt_reconnecting"]

    async def test_gives_up_after_max_attempts(self):
        svc = self._service()
        svc._reconnect_attempts = 3  # already exhausted
        await svc._receive_task_handler()
        assert "connect" not in svc.calls
        assert any(
            isinstance(c, tuple) and c[0] == "push_error" for c in svc.calls
        )
        assert [e["kind"] for e in svc._recorder.events] == ["stt_reconnect_gave_up"]

    async def test_no_reconnect_during_teardown(self):
        svc = self._service()
        svc._stt_stopping = True
        await svc._receive_task_handler()
        assert svc.calls == []

    async def test_transcript_resets_the_attempt_counter(self):
        from pipecat.frames.frames import TranscriptionFrame

        svc = self._service()
        svc._reconnect_attempts = 2

        pushed = []

        async def _super_push(frame, direction=None):
            pushed.append(frame)

        # Bypass the pipecat FrameProcessor plumbing.
        import voice_runtime.sarvam_stt as sarvam_stt_module

        original = sarvam_stt_module.SarvamSTTService.push_frame

        async def _stub(self, frame, direction=None):
            await _super_push(frame, direction)

        sarvam_stt_module.SarvamSTTService.push_frame = _stub
        try:
            frame = TranscriptionFrame(
                text="हाँ", user_id="", timestamp="", result=None
            )
            await svc.push_frame(frame)
        finally:
            sarvam_stt_module.SarvamSTTService.push_frame = original
        assert svc._reconnect_attempts == 0
        assert pushed and pushed[0].finalized is True


class TestBackchannelLatch:
    def _brain(self):
        from voice_runtime.brain import ConversationBrain

        brain = ConversationBrain.__new__(ConversationBrain)
        brain._closing = True
        brain._backchannel_active = False
        brain._audio_gate = None
        return brain

    async def test_play_backchannel_noops_while_closing(self):
        brain = self._brain()
        await brain._play_backchannel("hmm")
        # The old code flipped the flag first and _speak_transient no-opped,
        # latching the backchannel window open for the rest of the call.
        assert brain._backchannel_active is False
