"""No caller audio is dropped because the STT socket was not ready.

Live 2026-10-22 (cv_80982e36f4f5 / cv_baeaf87927cb): the caller's first
"haan boliye" after the greeting produced no transcript. The socket itself was
open 18 s earlier, so that loss had another cause (Sarvam's first-turn speech
requirement, see test_sarvam_first_turn_handshake.py) — but the audit of the
path showed both Sarvam adapters simply DROP audio that arrives while no
socket is live: ``run_stt`` returned without sending. With pipecat 1.13's
concurrent StartFrame, inbound audio can precede the handshake, and the
legacy adapter's bounded mid-call reconnect opens the same window.

Both adapters now hold that audio (bounded, ``_PRECONNECT_BUFFER_S``) and
send it as soon as the socket (and, on the realtime socket under manual
endpointing, the announced utterance) is ready, recording
``stt_audio_buffered_before_connect`` with the buffered duration.
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import TranscriptionFrame
from pipecat.services.sarvam.stt import SarvamSTTService

from voice_runtime import sarvam_realtime_stt as rt
from voice_runtime import sarvam_stt
from voice_runtime.sarvam_stt import EndpointedSarvamSTTService

FRAME_MS = 20
RATE = 8000
FRAME_BYTES = RATE * 2 * FRAME_MS // 1000   # 320 bytes = 20 ms at 8 kHz


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def add_event(self, kind: str, **data) -> None:
        self.events.append((kind, data))

    def of(self, kind: str) -> list[dict]:
        return [data for k, data in self.events if k == kind]


def _frames(text: str, *, count: int, fill: bytes = b"\x00") -> list[bytes]:
    """``count`` 20 ms frames whose bytes spell ``text`` (padded with
    ``fill``), so what Sarvam received can be read back as the words it was
    sent. The realtime adapter never sends exact digital silence (the gate's
    zeros hallucinate on the open utterance), so its tests pad with 0x01."""
    payload = text.encode("ascii")
    frames = []
    for i in range(count):
        chunk = payload[i * FRAME_BYTES:(i + 1) * FRAME_BYTES]
        frames.append(chunk + fill * (FRAME_BYTES - len(chunk)))
    return frames


async def _feed(service, frame: bytes) -> None:
    async for _ in service.run_stt(frame):
        pass


# ── a Sarvam SDK streaming client that "recognises" the bytes it was sent ─────

class _FakeSocket:
    def __init__(self) -> None:
        self.audio = bytearray()
        self.flushes = 0
        self.handler = None
        self.closed = asyncio.Event()

    def on(self, event, handler):
        self.handler = handler

    async def start_listening(self):
        await self.closed.wait()

    async def transcribe(self, audio: str, encoding="audio/wav", sample_rate=16000):
        self.audio.extend(base64.b64decode(audio))

    async def flush(self):
        self.flushes += 1
        words = bytes(b for b in self.audio if b).decode("ascii", "ignore")
        self.audio.clear()
        message = SimpleNamespace(
            type="data", data=SimpleNamespace(transcript=words, language_code="hi-IN"),
            dict=lambda: {"transcript": words},
        )
        self.handler(message)


class _FakeStreaming:
    def __init__(self, socket: _FakeSocket) -> None:
        self.socket = socket
        self.connect_kwargs: list[dict] = []

    def connect(self, **kwargs):
        self.connect_kwargs.append(kwargs)
        socket = self.socket

        class _Ctx:
            async def __aenter__(self_inner):
                return socket

            async def __aexit__(self_inner, *exc):
                socket.closed.set()

        return _Ctx()


def _legacy_service(recorder: _Recorder, monkeypatch) -> tuple[EndpointedSarvamSTTService, _FakeStreaming]:
    service = EndpointedSarvamSTTService(
        api_key="k", mode="transcribe", sample_rate=RATE, input_audio_codec="pcm_s16le",
        settings=SarvamSTTService.Settings(model="saaras:v3", language=None, vad_signals=False),
        keepalive_timeout=None, recorder=recorder,
    )
    service._sample_rate = RATE           # setup() would copy it from the pipeline
    fake = _FakeStreaming(_FakeSocket())
    service._sarvam_client.speech_to_text_streaming._client = fake   # behind the connect proxy
    tasks: list[asyncio.Task] = []

    def _create_task(coro, name=None):
        task = asyncio.get_running_loop().create_task(coro)
        tasks.append(task)
        return task

    async def _cancel_task(task, timeout=None):
        task.cancel()

    pushed: list = []

    async def _push(frame, direction=None):
        pushed.append(frame)

    async def _usage():
        return None

    monkeypatch.setattr(service, "create_task", _create_task)
    monkeypatch.setattr(service, "cancel_task", _cancel_task)
    monkeypatch.setattr(service, "push_frame", _push)
    monkeypatch.setattr(service, "emit_stt_usage_metrics", _usage)
    monkeypatch.setattr(service, "_create_keepalive_task", lambda: None)
    service._test_pushed = pushed
    service._test_tasks = tasks
    return service, fake


class TestLegacyAdapterHoldsAudioUntilTheSocketIsLive:
    @pytest.mark.asyncio
    async def test_audio_800ms_before_the_socket_opens_reaches_sarvam_in_full(self, monkeypatch):
        recorder = _Recorder()
        service, fake = _legacy_service(recorder, monkeypatch)
        frames = _frames("haan boliye", count=40)          # 800 ms, socket not open yet
        for frame in frames:
            await _feed(service, frame)
        assert fake.socket.audio == b""                     # nothing could be sent yet
        assert len(service._held_audio) == 40 * FRAME_BYTES

        await service._connect()                            # the handshake completes
        assert bytes(fake.socket.audio) == b"".join(frames)   # replayed, complete and in order
        [event] = recorder.of("stt_audio_buffered_before_connect")
        assert event == {"buffered_ms": 800.0, "dropped_ms": 0.0, "reconnect": False}
        assert service._held_audio == bytearray()

        # Live audio keeps flowing straight through, after the replayed part.
        for frame in _frames(" ji", count=3):
            await _feed(service, frame)
        await fake.socket.flush()                           # the VAD-stop flush
        await asyncio.sleep(0)
        [final] = [f for f in service._test_pushed if isinstance(f, TranscriptionFrame)]
        assert final.text == "haan boliye ji" and final.finalized is True

        fake.socket.closed.set()
        for task in service._test_tasks:
            task.cancel()

    @pytest.mark.asyncio
    async def test_hold_is_bounded_to_the_newest_six_seconds(self, monkeypatch):
        recorder = _Recorder()
        service, fake = _legacy_service(recorder, monkeypatch)
        frames = [bytes([1 + (i % 250)]) * FRAME_BYTES for i in range(350)]   # 7 s
        for frame in frames:
            await _feed(service, frame)
        assert len(service._held_audio) == 300 * FRAME_BYTES
        await service._connect()
        assert bytes(fake.socket.audio) == b"".join(frames[50:])     # oldest second dropped
        [event] = recorder.of("stt_audio_buffered_before_connect")
        assert event["buffered_ms"] == 6000.0 and event["dropped_ms"] == 1000.0
        fake.socket.closed.set()
        for task in service._test_tasks:
            task.cancel()

    @pytest.mark.asyncio
    async def test_dead_socket_window_before_a_reconnect_is_covered_too(self, monkeypatch):
        recorder = _Recorder()
        service, fake = _legacy_service(recorder, monkeypatch)
        await service._connect()
        assert recorder.of("stt_audio_buffered_before_connect") == []   # nothing was waiting
        # The socket died mid-call; the receive task dropped the client and
        # is about to reconnect (voice_runtime.sarvam_stt._receive_task_handler).
        service._socket_client = None
        service._reconnect_attempts = 1
        frames = _frames("abhi", count=10)
        for frame in frames:
            await _feed(service, frame)
        fake.socket.audio.clear()
        await service._connect()
        assert bytes(fake.socket.audio) == b"".join(frames)
        [event] = recorder.of("stt_audio_buffered_before_connect")
        assert event["buffered_ms"] == 200.0 and event["reconnect"] is True
        fake.socket.closed.set()
        for task in service._test_tasks:
            task.cancel()

    @pytest.mark.asyncio
    async def test_nothing_is_held_once_the_service_is_stopping(self, monkeypatch):
        service, _ = _legacy_service(_Recorder(), monkeypatch)
        service._stt_stopping = True
        await _feed(service, _frames("x", count=1)[0])
        assert service._held_audio == bytearray()


# ── realtime adapter ─────────────────────────────────────────────────────────

def _realtime_service(recorder: _Recorder, *, endpointing: str) -> rt.RealtimeSarvamSTTService:
    service = rt.RealtimeSarvamSTTService(
        api_key="k", endpointing=endpointing, sample_rate=RATE,
        settings=rt.RealtimeSarvamSTTService.Settings(model="saaras:v3-realtime", language=None),
        recorder=recorder,
    )
    service._sample_rate = RATE
    return service


class TestRealtimeAdapterHoldsAudioUntilReady:
    @pytest.mark.asyncio
    async def test_manual_endpointing_waits_for_socket_and_announced_utterance(self, monkeypatch):
        recorder = _Recorder()
        service = _realtime_service(recorder, endpointing="manual")
        try:
            sent: list[bytes] = []

            async def _send_chunk(chunk: bytes):
                sent.append(chunk)

            monkeypatch.setattr(service, "_send_audio_chunk", _send_chunk)
            monkeypatch.setattr(service, "_is_websocket_open", lambda: False)
            frames = _frames("haan boliye", count=40, fill=b"\x01")   # 800 ms before the socket opens
            for frame in frames:
                await _feed(service, frame)
            assert sent == [] and len(service._held_audio) == 40 * FRAME_BYTES

            # Socket open, but ``speech_start`` not announced yet: the server
            # would not transcribe audio sent now, so it is still held.
            monkeypatch.setattr(service, "_is_websocket_open", lambda: True)
            await _feed(service, frames[0])
            assert sent == [] and len(service._held_audio) == 41 * FRAME_BYTES

            service._utterance_open = True                   # session.begin → speech_start sent
            tail = _frames(" ji", count=5, fill=b"\x01")
            for frame in tail:
                await _feed(service, frame)
            expected = b"".join(frames) + frames[0] + b"".join(tail)
            # Upstream sends 100 ms chunks and keeps the remainder buffered.
            assert b"".join(sent) == expected[: len(b"".join(sent))]
            assert len(b"".join(sent)) >= len(expected) - RATE * 2 // 10
            [event] = recorder.of("stt_audio_buffered_before_connect")
            assert event == {"buffered_ms": 820.0, "dropped_ms": 0.0}
            assert service._held_audio == bytearray()
        finally:
            await service.cleanup()

    @pytest.mark.asyncio
    async def test_vad_endpointing_only_waits_for_the_socket(self, monkeypatch):
        recorder = _Recorder()
        service = _realtime_service(recorder, endpointing="vad")
        try:
            sent: list[bytes] = []

            async def _send_chunk(chunk: bytes):
                sent.append(chunk)

            monkeypatch.setattr(service, "_send_audio_chunk", _send_chunk)
            monkeypatch.setattr(service, "_is_websocket_open", lambda: False)
            frames = _frames("namaste", count=10, fill=b"\x01")
            for frame in frames:
                await _feed(service, frame)
            monkeypatch.setattr(service, "_is_websocket_open", lambda: True)
            for frame in _frames("ji", count=5, fill=b"\x01"):
                await _feed(service, frame)
            assert b"".join(sent).startswith(b"".join(frames))
            assert recorder.of("stt_audio_buffered_before_connect")[0]["buffered_ms"] == 200.0
        finally:
            await service.cleanup()

    @pytest.mark.asyncio
    async def test_hold_is_bounded(self, monkeypatch):
        service = _realtime_service(_Recorder(), endpointing="manual")
        try:
            monkeypatch.setattr(service, "_is_websocket_open", lambda: False)
            for i in range(350):
                await _feed(service, bytes([1 + (i % 250)]) * FRAME_BYTES)
            assert len(service._held_audio) == int(RATE * 2 * sarvam_stt._PRECONNECT_BUFFER_S)
            assert service._held_dropped_bytes == 50 * FRAME_BYTES
        finally:
            await service.cleanup()
