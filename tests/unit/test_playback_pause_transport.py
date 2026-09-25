"""Provisional pause at the transport media sender (voice_runtime.filler_transport).

2026-09-23 runtime test: the PlaybackDuck fired its pause, but a reply that
synthesized ahead of real time already sat in the output transport's own audio
queue, so the caller kept hearing the bot through every pause (duck held 0 ms).
The pause must therefore also stop the sender from dequeuing, keep the queued
audio for a resume, and let a committed interruption drop everything.
"""

import asyncio
import base64
import json
import time

from pipecat.frames.frames import EndFrame, InterruptionFrame, TTSAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from voice_runtime.filler_transport import FillerWebsocketOutputTransport
from voice_runtime.playback_duck import PlaybackDuck
from voice_runtime.serializer import RawPCMSerializer
from voice_runtime.telephony import FreeSwitchAudioForkSerializer


async def until(predicate, timeout=4.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def tagged(value: int, ms: int = 200) -> TTSAudioRawFrame:
    return TTSAudioRawFrame(
        audio=value.to_bytes(2, "little", signed=True) * (ms * 8),
        sample_rate=8000, num_channels=1,
    )


class WireSocket:
    """Fake websocket client: records every payload with arrival time and the
    sample value it carries (each queued frame is tagged with one value)."""

    is_connected = True
    is_closing = False

    def __init__(self):
        self.wire: list[tuple[float, int | None, str | None]] = []

    async def setup(self, frame):
        pass

    async def disconnect(self):
        pass

    async def cleanup(self):
        pass

    async def send(self, payload):
        now = time.monotonic()
        if isinstance(payload, (bytes, bytearray)):
            audio = bytes(payload)
        else:
            message = json.loads(payload)
            if message.get("type") == "playAudio":
                audio = base64.b64decode(message["data"]["audioContent"])
            else:
                self.wire.append((now, None, message.get("type") or message.get("name")))
                return
        for offset in range(0, len(audio) - 1, 640):        # every 40 ms of 8 kHz audio
            value = int.from_bytes(audio[offset:offset + 2], "little", signed=True)
            if value:
                self.wire.append((now, value, None))

    def values(self):
        return [v for _, v, _ in self.wire if v is not None]

    def audio_after(self, t):
        return [(ts, v) for ts, v, _ in self.wire if v is not None and ts > t]


class Passthrough(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class Session:
    def __init__(self, kind: str):
        self.client = WireSocket()
        serializer = RawPCMSerializer() if kind == "browser" else FreeSwitchAudioForkSerializer()
        self.output = FillerWebsocketOutputTransport(self.client, self.client, FastAPIWebsocketParams(
            audio_out_enabled=True, audio_out_sample_rate=8000, audio_out_10ms_chunks=4,
            audio_out_end_silence_secs=0, serializer=serializer,
        ))
        self.worker = PipelineWorker(
            Pipeline([Passthrough(), self.output]),
            params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000),
            enable_rtvi=False, idle_timeout_secs=None,
        )
        self._runner = None

    async def __aenter__(self):
        self._runner = asyncio.create_task(WorkerRunner(handle_sigint=False).run(self.worker))
        await until(lambda: bool(self.output._media_senders))
        return self

    async def __aexit__(self, *exc):
        await self.worker.queue_frame(EndFrame())
        try:
            await asyncio.wait_for(self._runner, timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            self._runner.cancel()

    async def queue_reply(self, values, ms=200):
        for value in values:
            await self.worker.queue_frame(tagged(value, ms))


class TestSenderPause:
    async def test_pause_stops_the_wire_and_resume_continues_in_order(self):
        for kind in ("browser", "fork"):
            async with Session(kind) as s:
                # 1.2 s of reply queued at once inside the sender (fast provider).
                await s.queue_reply([1, 2, 3, 4, 5, 6])
                await until(lambda: len(s.client.values()) >= 2)
                s.output.pause_playback()
                paused_at = time.monotonic()
                assert s.output.playback_paused
                await asyncio.sleep(0.5)
                leaked = s.client.audio_after(paused_at + 0.05)
                assert leaked == [], f"{kind}: audio kept flowing during the pause: {leaked}"
                s.output.resume_playback()
                # 200 ms frames leave the sender as 40 ms chunks: wait for the whole tail.
                await until(lambda: s.client.values().count(6) >= 5)
                values = s.client.values()
                # Everything queued before the pause plays, in order, nothing lost or repeated.
                assert values == sorted(values), values
                per_value = {v: values.count(v) for v in range(1, 7)}
                assert len(set(per_value.values())) == 1, (kind, per_value)

    async def test_interruption_during_pause_drops_queued_and_parked_audio(self):
        for kind in ("browser", "fork"):
            async with Session(kind) as s:
                await s.queue_reply([1, 2, 3, 4, 5, 6])
                await until(lambda: len(s.client.values()) >= 2)
                s.output.pause_playback()
                await asyncio.sleep(0.15)
                cut_at = time.monotonic()
                await s.worker.queue_frame(InterruptionFrame())
                await until(lambda: not s.output.playback_paused)
                await asyncio.sleep(0.4)
                stale = s.client.audio_after(cut_at + 0.02)
                assert stale == [], f"{kind}: stale audio after the interruption: {stale}"
                # The sender is alive again: audio for the next reply flows.
                await s.queue_reply([9, 9])
                await until(lambda: 9 in s.client.values())
                assert all(v == 9 for _, v in s.client.audio_after(cut_at + 0.02)), "old tail replayed"

    async def test_end_frame_is_never_held_by_a_pause(self):
        s = Session("browser")
        async with s:
            await s.queue_reply([1, 2])
            s.output.pause_playback()
        # __aexit__ queued the EndFrame and the runner finished within its timeout.
        assert s._runner.done()


class _Control:
    def __init__(self):
        self.calls = []

    def pause_playback(self):
        self.calls.append("pause")

    def resume_playback(self):
        self.calls.append("resume")


def _duck(control):
    duck = PlaybackDuck(recorder=None, playback_control=control)
    duck._sample_rate = 8000

    async def _push(frame, direction=None):
        pass

    duck.push_frame = _push
    return duck


class TestDuckDrivesTheTransport:
    async def test_pause_resume_forward_to_the_transport(self):
        control = _Control()
        duck = _duck(control)
        await duck.set_provisional(True)
        await duck.set_provisional(False)
        assert control.calls == ["pause", "resume"]

    async def test_interruption_leaves_the_resume_to_the_transport_handler(self):
        control = _Control()
        duck = _duck(control)
        await duck.set_provisional(True)
        await duck.process_frame(InterruptionFrame(), None)
        # No duck-side resume: the transport's own interruption handling
        # drops its queue before opening the gate, so nothing paused plays.
        assert control.calls == ["pause"]
        assert not duck.paused

    async def test_teardown_resumes_the_transport(self):
        control = _Control()
        duck = _duck(control)
        await duck.set_provisional(True)
        await duck.process_frame(EndFrame(), None)
        assert control.calls == ["pause", "resume"]


class TestEchoReferenceFeed:
    async def test_reference_holds_each_frame_before_it_goes_on_the_wire(self):
        # Telephony self-echo evidence (voice_runtime.echo_reference): the
        # reference must have a frame no later than the socket send, or a
        # fast echo path returns audio the gate cannot compare against.
        from voice_runtime.echo_reference import EchoReference

        class Feed(EchoReference):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.fed = []

            def add_output(self, pcm, sample_rate, at=None):
                self.fed.append((time.monotonic(), len(pcm)))
                super().add_output(pcm, sample_rate, at)

        for kind in ("fork", "browser"):
            async with Session(kind) as s:
                ref = Feed(sample_rate=8000)
                s.output.attach_echo_reference(ref)
                await s.queue_reply([1, 2, 3])
                await until(lambda: len(s.client.values()) >= 3 and len(ref.fed) >= 1)
                await until(lambda: sum(n for _, n in ref.fed) >= 3 * 8000 * 2 * 0.2 - 1)
                first_wire = s.client.wire[0][0]
                assert ref.fed[0][0] <= first_wire, (kind, ref.fed[0][0] - first_wire)
                assert sum(n for _, n in ref.fed) == 3 * int(8000 * 0.2) * 2, kind
                assert ref.active()

    async def test_without_a_reference_the_write_path_is_unchanged(self):
        async with Session("fork") as s:
            assert s.output._echo_reference is None
            await s.queue_reply([1, 2])
            await until(lambda: len(s.client.values()) >= 2)
