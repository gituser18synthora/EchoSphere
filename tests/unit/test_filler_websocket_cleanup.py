"""Actual websocket output/serializer handoff, with a local playback model.

The browser model supports targeted clear; telephony can only drain packets
already sent on the wire. Neither model claims remote hardware measurements.
"""

import asyncio
import base64
import json
import time

import pytest
from pipecat.frames.frames import EndFrame, StartFrame, TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from voice_runtime.filler_transport import FillerWebsocketOutputTransport
from voice_runtime.frames import FillerAudioOwner, FillerAudioRawFrame, FillerClearFrame
from voice_runtime.latency_filler import LatencyFillerProcessor
from voice_runtime.serializer import RawPCMSerializer
from voice_runtime.telephony import FreeSwitchAudioForkSerializer, VaaniFrameSerializer
from tests.unit.test_latency_filler import _AcknowledgementCueStub, _ShortLibrary
from tests.unit.test_latency_filler_readiness import AudioProbe, TimedRecorder


class SocketPlayback:
    is_connected = True
    is_closing = False

    def __init__(self, browser=False):
        self.browser = browser
        self.packets = []
        self.clears = []
        self.playhead = 0.0

    async def setup(self, frame):
        pass

    async def disconnect(self):
        pass

    async def cleanup(self):
        pass

    async def send(self, payload):
        now = time.monotonic()
        owner = None
        if isinstance(payload, bytes):
            audio = payload
        else:
            message = json.loads(payload)
            if message.get("type") == "filler_clear":
                self.clears.append((now, message["owner"]))
                for packet in self.packets:
                    if packet["owner"] == message["owner"] and packet["end"] > now:
                        boundary = min(packet["end"], now + 0.002) if packet["play"] <= now else now
                        packet["discarded_ms"] = (packet["end"] - max(boundary, packet["play"])) * 1000
                        packet["end"] = min(packet["end"], boundary)
                self.playhead = max([now, *(p["end"] for p in self.packets)])
                return
            if message.get("type") == "filler_audio":
                owner = message["owner"]
                audio = base64.b64decode(message["audio"])
            elif message.get("type") == "playAudio":
                audio = base64.b64decode(message["data"]["audioContent"])
            elif message.get("event") == "media":
                audio = base64.b64decode(message["media"]["payload"])
            else:
                return
        values = list(memoryview(audio).cast("h"))
        reply = 12000 in values
        first_reply_offset = values.index(12000) / 8000 if reply else 0
        filler_samples = [i for i, value in enumerate(values) if value != 12000]
        play = max(now, self.playhead)
        duration = len(audio) / 16000  # native8k, mono16-bit
        self.playhead = play + duration
        self.packets.append({
            "sent": now, "play": play, "end": self.playhead,
            "duration_ms": duration * 1000, "owner": owner,
            "reply": reply, "mixed": reply and any(v != 12000 for v in values),
            "first_reply_offset": first_reply_offset,
            "reply_duration_ms": values.count(12000) / 8,
            "last_filler_end": play + (filler_samples[-1] + 1) / 8000 if filler_samples else None,
            "discarded_ms": 0.0,
        })


class ObservedOutput(FillerWebsocketOutputTransport):
    class MediaSender(FillerWebsocketOutputTransport.MediaSender):
        async def handle_audio_frame(self, frame):
            await super().handle_audio_frame(frame)
            if isinstance(frame, FillerAudioRawFrame):
                self._transport.queued.append((time.monotonic(), frame.owner.token, len(frame.audio)))

        async def clear_filler(self, owner):
            discarded = await super().clear_filler(owner)
            self._transport.discards.append((time.monotonic(), owner.token, discarded))
            return discarded

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queued = []
        self.discards = []


async def run_websocket_cleanup(transport_kind, acknowledgements):
    client = SocketPlayback(browser=transport_kind == "browser")
    serializer = {
        "browser": RawPCMSerializer,
        "fork": FreeSwitchAudioForkSerializer,
        "vaani": lambda: VaaniFrameSerializer(stream_sid="local-call"),
    }[transport_kind]()
    output = ObservedOutput(client, client, FastAPIWebsocketParams(
        audio_out_enabled=True, audio_out_sample_rate=8000,
        audio_out_10ms_chunks=4, audio_out_end_silence_secs=0,
        serializer=serializer,
    ))
    recorder = TimedRecorder()
    class ObservedFiller(LatencyFillerProcessor):
        async def queue_frame(self, frame, direction, callback=None):
            if isinstance(frame, TTSAudioRawFrame) and frame.num_frames > 0:
                recorder.add_event("playable_audio_ready")
            await super().queue_frame(frame, direction, callback)

    filler = ObservedFiller(
        delay_ms=100, sample_rate=8000, library=_ShortLibrary(clip_ms=600),
        cue_library=_AcknowledgementCueStub(), recorder=recorder,
        emit_flush_marker=transport_kind != "browser",
    )
    incoming = AudioProbe()
    worker = PipelineWorker(
        Pipeline([incoming, filler, output]),
        params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000),
        enable_rtvi=False, idle_timeout_secs=None,
    )
    rows = []
    scenarios = [
        ("immediately_after_start", 0.01, 0.125),
        ("middle", 0.01, 0.3), ("near_end", 0.01, 0.67),
        ("producer_finished", 0.01, 0.705), ("fast", 0.01, 0.04),
        ("slow_llm", 0.3, 0.33),
        *[(f"slow_tts_{i + 1}", 0.01, 0.33) for i in range(6)],
    ]

    async def feed():
        await asyncio.sleep(0.1)
        for index, (name, started_after, ready_after) in enumerate(scenarios):
            stopped = time.monotonic()
            await filler.arm(
                turn_id=index + 1, gender="female", speech_stopped_at=stopped,
                acknowledgement={"text": "जी…"} if acknowledgements else None,
            )
            await asyncio.sleep(started_after)
            await worker.queue_frame(TTSStartedFrame())
            await asyncio.sleep(max(0, stopped + ready_after - time.monotonic()))
            await worker.queue_frame(TTSAudioRawFrame(
                # 280 ms exactly fills the existing telephony reply ramp
                # (40+80+160 ms). Final speech-packet flush is separate from
                # filler cleanup and is intentionally not changed here.
                audio=(12000).to_bytes(2, "little") * 2240,
                sample_rate=8000, num_channels=1,
            ))
            await worker.queue_frame(TTSStoppedFrame())
            await asyncio.sleep(0.8)
            events = [(at, kind, data) for at, kind, data in recorder.timed_events if at >= stopped]
            ready = next(at for at, kind, _ in events if kind == "playable_audio_ready")
            start = next((at for at, kind, _ in events if kind == "latency_filler_played"), None)
            cancel = next((at for at, kind, _ in events if kind == "latency_filler_cut"), None)
            packets = [p for p in client.packets if p["sent"] >= stopped]
            reply = next(p for p in packets if p["reply"])
            fillers = [p for p in packets if p["last_filler_end"] is not None]
            reply_play = reply["play"] + reply["first_reply_offset"]
            relative = lambda at: round((at - stopped) * 1000, 3) if at is not None else None
            rows.append({
                "transport": transport_kind, "ack": acknowledgements, "scenario": name,
                "speech_end_ms": 0.0, "speech_end_monotonic": stopped,
                "filler_start_ms": relative(start), "ready_ms": relative(ready),
                "cancel_ms": relative(cancel),
                "clear_ms": [relative(at) for at, _ in client.clears if at >= stopped],
                "queued": [{"ms": relative(at), "owner": owner, "bytes": size}
                           for at, owner, size in output.queued if at >= stopped],
                "discarded_bytes": sum(size for at, _, size in output.discards if at >= stopped),
                "last_filler_sent_ms": relative(fillers[-1]["sent"] if fillers else None),
                "last_filler_played_until_ms": relative(max((min(p["end"], p["last_filler_end"]) for p in fillers), default=None)),
                "first_response_sent_ms": relative(reply["sent"]),
                "first_response_play_ms": relative(reply_play),
                "ready_to_sent_ms": (reply["sent"] - ready) * 1000,
                "ready_to_play_ms": (reply_play - ready) * 1000,
                "filler_packets_sent_after_ready": sum(p["sent"] >= ready for p in fillers),
                "filler_playback_tail_ms": max(0, max((min(p["end"], p["last_filler_end"]) for p in fillers), default=ready) - ready) * 1000,
                "remote_discarded_ms": sum(p["discarded_ms"] for p in fillers),
                "reply_audio_ms": sum(p["reply_duration_ms"] for p in packets),
                "mixed_packets": sum(p["mixed"] for p in packets),
            })
        await worker.queue_frame(EndFrame())

    feeder = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(WorkerRunner(handle_sigint=False).run(worker), timeout=30)
        await feeder
    finally:
        if not feeder.done():
            feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
    return rows


@pytest.mark.parametrize("kind", ["browser", "fork", "vaani"])
@pytest.mark.parametrize("ack", [False, True])
async def test_websocket_reply_priority_without_filler_tail_in_server_queues(kind, ack):
    for row in await run_websocket_cleanup(kind, ack):
        assert row["mixed_packets"] == 0, row
        assert row["filler_packets_sent_after_ready"] == 0, row
        assert row["ready_to_sent_ms"] < 30, row
        assert row["ready_to_play_ms"] < 40, row
        assert row["reply_audio_ms"] == pytest.approx(280), row
        if row["scenario"] == "fast":
            assert row["filler_start_ms"] is None and row["queued"] == [], row
        else:
            assert 95 <= row["filler_start_ms"] < 130, row
            assert row["queued"], row


async def test_browser_serializer_keeps_real_pcm_and_clears_only_tagged_owner():
    serializer = RawPCMSerializer()
    owner = FillerAudioOwner(turn_id=1)
    frame = FillerAudioRawFrame(audio=b"\x00\x10" * 160, sample_rate=8000, num_channels=1, owner=owner)
    payload = json.loads(await serializer.serialize(frame))
    assert payload["owner"] == owner.token
    assert base64.b64decode(payload["audio"]) == frame.audio
    owner.cancel()
    assert await serializer.serialize(frame) is None
    assert json.loads(await serializer.serialize(FillerClearFrame(owner))) == {
        "type": "filler_clear", "owner": owner.token,
    }
    reply = TTSAudioRawFrame(audio=b"\x00\x20" * 160, sample_rate=8000, num_channels=1)
    assert await serializer.serialize(reply) == reply.audio


async def test_cancel_during_serialization_drops_packet_before_socket_send():
    release, entered = asyncio.Event(), asyncio.Event()

    class SlowSerializer(RawPCMSerializer):
        async def serialize(self, frame):
            if isinstance(frame, FillerAudioRawFrame):
                payload = await super().serialize(frame)
                entered.set()
                await release.wait()
                return payload
            return await super().serialize(frame)

    client = SocketPlayback(browser=True)
    output = ObservedOutput(client, client, FastAPIWebsocketParams(serializer=SlowSerializer()))
    owner = FillerAudioOwner(turn_id=1)
    frame = FillerAudioRawFrame(audio=b"\x00\x10" * 160, sample_rate=8000, num_channels=1, owner=owner)
    sending = asyncio.create_task(output.write_audio_frame(frame))
    await entered.wait()
    owner.cancel()
    release.set()
    assert await sending is False
    assert client.packets == []


async def test_filler_never_enters_optional_fixed_packet_buffer():
    client = SocketPlayback(browser=True)
    output = ObservedOutput(client, client, FastAPIWebsocketParams(
        serializer=RawPCMSerializer(), fixed_audio_packet_size=640,
    ))
    # Valid speech already waiting for a full packet must survive untouched.
    output._audio_send_buffer.extend(b"\x00\x20" * 160)
    owner = FillerAudioOwner(turn_id=1)
    await output._write_frame(FillerAudioRawFrame(
        audio=b"\x00\x10" * 160, sample_rate=8000, num_channels=1, owner=owner,
    ))
    owner.cancel()
    await output.clear_filler(owner)
    assert output._audio_send_buffer == b"\x00\x20" * 160
    await output._write_frame(TTSAudioRawFrame(
        audio=b"\x00\x20" * 160, sample_rate=8000, num_channels=1,
    ))
    assert output._audio_send_buffer == b""


def test_session_transport_installs_owner_preserving_output():
    from voice_runtime.filler_transport import FillerWebsocketTransport
    transport = FillerWebsocketTransport(
        websocket=object(), params=FastAPIWebsocketParams(allowed_origins=[]),
    )
    assert isinstance(transport.output(), FillerWebsocketOutputTransport)
