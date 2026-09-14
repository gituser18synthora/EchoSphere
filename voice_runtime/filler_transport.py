"""Ownership-preserving output for disposable latency filler PCM.

Normal response audio retains Pipecat's output path. Filler never enters its
untyped partial-chunk buffer, and retired filler can be dropped at every queue
boundary without resetting a call's valid audio or interruption state.
"""

import asyncio

from pipecat.frames.frames import (
    InterruptionFrame,
    OutputTransportMessageFrame, OutputTransportMessageUrgentFrame,
    OutputTransportReadyFrame, StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketOutputTransport, FastAPIWebsocketTransport,
)

from shared.audio.pcm import resample_pcm
from voice_runtime.frames import FillerAudioRawFrame, FillerClearFrame


class FillerOutputTransportMixin:
    """Small extension of Pipecat's sender, also usable by local test devices."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._filler_owners = {}

    class MediaSender(BaseOutputTransport.MediaSender):
        async def handle_interruptions(self, frame):
            # Base cleanup clears partial PCM only if bot-speaking began.
            # A sub-chunk packet can exist before that event (plain audio,
            # or synthesis interrupted before its first full output chunk).
            self._audio_buffer.clear()
            await super().handle_interruptions(frame)

        async def handle_audio_frame(self, frame):
            if not isinstance(frame, FillerAudioRawFrame):
                await super().handle_audio_frame(frame)
                return
            owner = frame.owner
            if owner is None or owner.cancelled or not self._params.audio_out_enabled:
                return
            # Isolated resampling: a filler must never leave samples in the
            # real response's streaming resampler or partial PCM buffer.
            audio = frame.audio
            if frame.sample_rate != self._sample_rate:
                audio = resample_pcm(audio, frame.sample_rate, self._sample_rate)
            size = max(2, int(self._sample_rate * 0.02) * frame.num_channels * 2)
            for offset in range(0, len(audio), size):
                chunk = FillerAudioRawFrame(
                    audio=audio[offset:offset + size], sample_rate=self._sample_rate,
                    num_channels=frame.num_channels, owner=owner,
                )
                chunk.transport_destination = self._destination
                self._audio_queue.put_nowait(chunk)

        async def clear_filler(self, owner):
            # Drain/requeue without yielding, preserving order and unfinished
            # task bookkeeping for every unrelated frame, including TTS.
            kept, discarded = [], 0
            while not self._audio_queue.empty():
                frame = self._audio_queue.get_nowait()
                if isinstance(frame, FillerAudioRawFrame) and frame.owner is owner:
                    discarded += len(frame.audio)
                elif (
                    isinstance(frame, OutputTransportMessageFrame)
                    and (frame.message or {}).get("filler_owner") == owner.token
                ):
                    pass
                else:
                    kept.append(frame)
                self._audio_queue.task_done()
            for frame in kept:
                self._audio_queue.put_nowait(frame)
            return discarded

        async def _next_frame(self):
            async for frame in super()._next_frame():
                if isinstance(frame, FillerAudioRawFrame) and (
                    frame.owner is None or frame.owner.cancelled
                ):
                    continue
                yield frame

    async def set_transport_ready(self, frame: StartFrame):
        # Pipecat's base factory hardcodes BaseOutputTransport.MediaSender.
        # Use our sender with identical destinations/start ordering instead.
        self._filler_owners = {}
        for destination in self._params.audio_out_destinations:
            await self.register_audio_destination(destination)
        for destination in self._params.video_out_destinations:
            await self.register_video_destination(destination)
        destinations = [None, *set(
            self._params.audio_out_destinations + self._params.video_out_destinations
        )]
        for destination in destinations:
            sender = self.MediaSender(
                self, destination=destination, sample_rate=self.sample_rate,
                audio_chunk_size=self.audio_chunk_size, params=self._params,
            )
            self._media_senders[destination] = sender
            await sender.start(frame)
        await self.push_frame(OutputTransportReadyFrame(), FrameDirection.UPSTREAM)

    async def clear_filler(self, owner):
        # No global queue reset, bot-speaking frame or InterruptionFrame.
        owner.cancel()
        discarded = 0
        for sender in self._media_senders.values():
            discarded += await sender.clear_filler(owner)
        await self._clear_filler_playback(owner)
        getattr(self, "_filler_owners", {}).pop(owner.token, None)
        return discarded

    async def _clear_filler_playback(self, owner):
        await self.send_message(OutputTransportMessageUrgentFrame(
            message={"type": "filler_clear", "owner": owner.token},
        ))

    async def _handle_frame(self, frame):
        if isinstance(frame, FillerAudioRawFrame):
            if frame.owner is None or frame.owner.cancelled:
                return
            self._filler_owners[frame.owner.token] = frame.owner
        else:
            # An urgent clear may overtake data still in a processor queue.
            # Also finish clearing cancelled owners before accepting response
            # PCM, even if the clear SystemFrame is awaiting socket I/O.
            for owner in list(getattr(self, "_filler_owners", {}).values()):
                if owner.cancelled:
                    await self.clear_filler(owner)
        await super()._handle_frame(frame)

    async def queue_frame(self, frame, direction=FrameDirection.DOWNSTREAM, callback=None):
        if isinstance(frame, FillerAudioRawFrame) and frame.owner and not frame.owner.cancelled:
            self._filler_owners[frame.owner.token] = frame.owner
        elif isinstance(frame, InterruptionFrame):
            # Include owners whose data has not reached _handle_frame yet.
            # The shared flag also aborts an in-flight async serialization.
            for owner in self._filler_owners.values():
                owner.cancel()
        await super().queue_frame(frame, direction, callback)

    async def process_frame(self, frame, direction):
        if isinstance(frame, InterruptionFrame):
            for owner in self._filler_owners.values():
                owner.cancel()
        if isinstance(frame, FillerClearFrame):
            await self.clear_filler(frame.owner)
            await self.push_frame(frame, direction)
            return
        await super().process_frame(frame, direction)


class FillerWebsocketOutputTransport(FillerOutputTransportMixin, FastAPIWebsocketOutputTransport):
    """Retire filler pacing and send its targeted clear before response PCM."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._filler_send_lock = asyncio.Lock()
        self._cleared_fillers = set()

    async def _clear_filler_playback(self, owner):
        async with self._filler_send_lock:
            if owner.token in self._cleared_fillers:
                return
            # Serializer receives the typed clear (telephony cannot safely
            # translate this into a whole-stream killAudio/clear).
            await super()._write_frame(FillerClearFrame(owner))
            self._cleared_fillers.add(owner.token)

    async def _write_frame(self, frame):
        async with self._filler_send_lock:
            if isinstance(frame, FillerAudioRawFrame):
                if frame.owner is None or frame.owner.cancelled:
                    return
                # Tagged filler bypasses optional fixed-size PCM buffering:
                # it must never be concatenated with a real response packet.
                payload = await self._params.serializer.serialize(frame)
                if payload and not frame.owner.cancelled:
                    await self._client.send(payload)
                    return True
                return False
            # Enforce wire order even when an urgent clear and an already
            # queued response reach the sender concurrently.
            for owner in list(getattr(self, "_filler_owners", {}).values()):
                if owner.cancelled and owner.token not in self._cleared_fillers:
                    await super()._write_frame(FillerClearFrame(owner))
                    self._cleared_fillers.add(owner.token)
            await super()._write_frame(frame)

    async def write_audio_frame(self, frame):
        if not isinstance(frame, FillerAudioRawFrame):
            return await super().write_audio_frame(frame)
        owner = frame.owner
        if owner is None or owner.cancelled or self._client.is_closing or not self._client.is_connected:
            return False
        # Preserve the owner which Pipecat's websocket writer otherwise
        # strips. No filler look-ahead and no debt in response pacing.
        if not await self._write_frame(frame):
            return False
        duration = len(frame.audio) / (frame.sample_rate * frame.num_channels * 2)
        try:
            await asyncio.wait_for(owner.cancelled_event.wait(), timeout=duration)
        except TimeoutError:
            pass
        return True


class FillerWebsocketTransport(FastAPIWebsocketTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._output = FillerWebsocketOutputTransport(
            self, self._client, self._params, name=self._output_name,
        )
