"""A bot-ended telephony call stays open until its last audio has played.

The media socket closes right after the EndFrame is written, and the session
then hangs up the leg (FreeSWITCH ``uuid_kill``). Audio is written at real
time, but the far end plays it from its own queue behind that, so closing at
the last write cut off the end of every goodbye (demo 2026-10-26: the last
word of bot-ended calls was not heard; channel killed 80-410 ms after the
last write). The far end below is modelled the same way: each packet plays
from ``max(arrival, end of the queue before it)`` in real time.
"""

import asyncio
import base64
import json
import time

import pytest
from pipecat.frames.frames import (
    EndFrame, InterruptionFrame, OutputAudioRawFrame, TTSAudioRawFrame, TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from voice_runtime import filler_transport
from voice_runtime.filler_transport import FillerWebsocketOutputTransport
from voice_runtime.serializer import RawPCMSerializer
from voice_runtime.telephony import (
    FreeSwitchAudioForkSerializer, FreeSwitchAudioStreamSerializer, VaaniFrameSerializer,
)

_SAMPLE = (9000).to_bytes(2, "little", signed=True)


class FarEnd:
    """Media socket peer that plays what it receives in real time."""

    def __init__(self):
        self.is_connected = True
        self.is_closing = False
        self.playhead = 0.0
        self.audio_bytes = 0
        self.disconnected_at: float | None = None
        self.kills = 0

    async def setup(self, frame):
        pass

    async def cleanup(self):
        pass

    async def disconnect(self):
        if self.disconnected_at is None:
            self.disconnected_at = time.monotonic()
        self.is_connected = False

    async def send(self, payload):
        now = time.monotonic()
        message = json.loads(payload) if isinstance(payload, str) else None
        if message is None:
            audio = payload
        elif message.get("type") == "playAudio":
            audio = base64.b64decode(message["data"]["audioContent"])
        elif message.get("type") == "streamAudio":
            audio = base64.b64decode(message["data"]["audioData"])
        elif message.get("event") == "media":
            audio = base64.b64decode(message["media"]["payload"])
        else:
            if message.get("type") == "killAudio" or message.get("event") == "clear":
                self.kills += 1
                self.playhead = now
            return
        self.audio_bytes += len(audio)
        self.playhead = max(now, self.playhead) + len(audio) / 16000

    def _hang_up(self):
        # The caller hung up while the goodbye was still playing.
        self.is_connected = False
        self.is_closing = True


async def _run_goodbye(serializer, client, *, audio_s: float, end_silence_secs: int = 0):
    output = FillerWebsocketOutputTransport(client, client, FastAPIWebsocketParams(
        audio_out_enabled=True, audio_out_sample_rate=8000,
        audio_out_10ms_chunks=4, audio_out_end_silence_secs=end_silence_secs,
        serializer=serializer,
    ))
    worker = PipelineWorker(
        Pipeline([output]),
        params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000),
        enable_rtvi=False, idle_timeout_secs=None,
    )
    nbytes = int(audio_s * 16000)
    nbytes -= nbytes % 2
    state = {}

    async def feed():
        deadline = time.monotonic() + 5.0
        while getattr(output, "_task_manager", None) is None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        await worker.queue_frame(TTSStartedFrame())
        # The whole goodbye arrives at once (TTS is faster than real time) and
        # ends on a sub-packet remnant: 1,000 bytes past the last 200 ms packet.
        await worker.queue_frame(TTSAudioRawFrame(
            audio=_SAMPLE * (nbytes // 2), sample_rate=8000, num_channels=1,
        ))
        await worker.queue_frame(TTSStoppedFrame())
        # The brain's EndWorkerFrame sits behind the reply: the worker ends.
        await worker.queue_frame(EndFrame())
        state["queued_at"] = time.monotonic()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    feeder = asyncio.create_task(feed())
    await asyncio.wait_for(runner.run(), timeout=30)
    await feeder
    state["ended_at"] = time.monotonic()
    return output, state, nbytes


@pytest.mark.asyncio
@pytest.mark.parametrize("make_serializer", [
    FreeSwitchAudioForkSerializer,
    lambda: FreeSwitchAudioStreamSerializer(send_kill_audio=True),
    lambda: VaaniFrameSerializer(stream_sid="sid"),
], ids=["fork", "stream", "vaani"])
async def test_socket_closes_after_the_last_audio_has_played(make_serializer):
    client = FarEnd()
    output, _, nbytes = await _run_goodbye(make_serializer(), client, audio_s=1.2625)
    # Every byte of the goodbye reached the far end, the remnant included.
    assert client.audio_bytes >= nbytes
    # The socket (and with it the call) closed after the far end finished
    # playing, plus the tail — not at the last write.
    assert client.disconnected_at is not None
    played_out = client.playhead
    tail = filler_transport.END_PLAYOUT_TAIL_S
    assert client.disconnected_at >= played_out + tail - 0.03, (
        client.disconnected_at - played_out
    )
    assert client.disconnected_at <= played_out + tail + 0.25, (
        client.disconnected_at - played_out
    )
    stats = output.end_playout
    assert stats is not None and not stats["capped"] and not stats["far_end_closed"]
    assert stats["tail_ms"] == 300
    assert stats["waited_ms"] >= 300


async def _output_with_backlog(client, seconds: float):
    """An output whose far end has ``seconds`` of audio still queued."""
    serializer = FreeSwitchAudioForkSerializer()
    output = FillerWebsocketOutputTransport(client, client, FastAPIWebsocketParams(
        audio_out_enabled=True, audio_out_sample_rate=8000, audio_out_end_silence_secs=0,
        serializer=serializer,
    ))
    for _ in range(int(seconds / 0.2)):
        await client.send(await serializer.serialize(
            OutputAudioRawFrame(audio=_SAMPLE * 1600, sample_rate=8000, num_channels=1)))
    assert serializer.playout_remaining_s() > seconds - 0.1
    return output


@pytest.mark.asyncio
async def test_the_wait_is_bounded():
    # A playout estimate far beyond the cap (6 s pushed to the far end at
    # once, as if the pacing had stalled) must not hold the call open.
    output = await _output_with_backlog(FarEnd(), 6.0)
    started = time.monotonic()
    await output._write_frame(EndFrame())
    held = time.monotonic() - started
    cap = filler_transport.END_PLAYOUT_MAX_WAIT_S
    assert cap - 0.05 <= held <= cap + 0.2
    assert output.end_playout["capped"] is True


@pytest.mark.asyncio
async def test_a_caller_hangup_during_the_wait_ends_it():
    client = FarEnd()
    output = await _output_with_backlog(client, 2.0)
    asyncio.get_running_loop().call_later(0.3, client._hang_up)
    started = time.monotonic()
    await output._write_frame(EndFrame())
    held = time.monotonic() - started
    assert output.end_playout["far_end_closed"] is True
    assert 0.25 <= held <= 0.5


@pytest.mark.asyncio
async def test_browser_sessions_are_unchanged():
    # No playout estimate (the browser client owns its own queue): no wait.
    client = FarEnd()
    output, _, _ = await _run_goodbye(RawPCMSerializer(), client, audio_s=0.5)
    assert output.end_playout is None


def test_a_kill_empties_the_playout_estimate():
    async def scenario():
        serializer = FreeSwitchAudioForkSerializer()
        await serializer.serialize(
            OutputAudioRawFrame(audio=_SAMPLE * 8000, sample_rate=8000, num_channels=1))
        assert 0.9 < serializer.playout_remaining_s() <= 1.0
        assert json.loads(await serializer.serialize(InterruptionFrame())) == {"type": "killAudio"}
        assert serializer.playout_remaining_s() == 0.0
        # mod_audio_stream without killAudio keeps playing what it has.
        stream = FreeSwitchAudioStreamSerializer(send_kill_audio=False)
        await stream.serialize(
            OutputAudioRawFrame(audio=_SAMPLE * 8000, sample_rate=8000, num_channels=1))
        assert await stream.serialize(InterruptionFrame()) is None
        assert stream.playout_remaining_s() > 0.9

    asyncio.run(scenario())
