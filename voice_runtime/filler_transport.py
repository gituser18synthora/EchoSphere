"""Ownership-preserving output for disposable latency filler PCM.

Normal response audio retains Pipecat's output path. Filler never enters its
untyped partial-chunk buffer, and retired filler can be dropped at every queue
boundary without resetting a call's valid audio or interruption state.
"""

import asyncio

import logging

from pipecat.frames.frames import (
    EndFrame, InterruptionFrame, OutputAudioRawFrame,
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
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # Provisional barge-in pause (voice_runtime.playback_duck). The
            # duck holds frames UPSTREAM of the transport, but a reply that
            # synthesizes faster than real time already sits in this sender's
            # audio queue when the pause is requested, so the caller kept
            # hearing the bot for the whole pause window (2026-09-23 runtime
            # test: 0 ms held by the duck, audio flowing through every pause).
            # While paused the audio task parks at the head of the queue:
            # nothing reaches the wire, nothing already queued is lost, and a
            # resume continues exactly where playback stopped.
            self._playback_paused = False
            self._playback_gate = asyncio.Event()
            self._playback_gate.set()

        @property
        def playback_paused(self) -> bool:
            return self._playback_paused

        def pause_playback(self) -> None:
            if self._playback_paused:
                return
            self._playback_paused = True
            self._playback_gate.clear()

        def resume_playback(self) -> None:
            if not self._playback_paused:
                return
            self._playback_paused = False
            self._playback_gate.set()

        async def stop(self, frame):
            # Teardown drains the queue up to the EndFrame; a pause must
            # never stand between it and the audio task that waits for it.
            self.resume_playback()
            await super().stop(frame)

        async def handle_interruptions(self, frame):
            # Base cleanup clears partial PCM only if bot-speaking began.
            # A sub-chunk packet can exist before that event (plain audio,
            # or synthesis interrupted before its first full output chunk).
            self._audio_buffer.clear()
            if self._playback_paused:
                # A committed barge-in ends the pause: the frame parked on the
                # gate belongs to the interrupted reply, so the audio task is
                # cancelled BEFORE the gate opens (opening it first would let
                # the parked frame reach the wire ahead of the cancel). The
                # base restarts the task over an empty queue below.
                await self._cancel_audio_task()
                self._playback_paused = False
                self._playback_gate.set()
            await super().handle_interruptions(frame)
            if not self._audio_task:
                self._create_audio_task()

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
                if self._playback_paused and not isinstance(frame, EndFrame):
                    # Park at the head of the queue until the pause ends. An
                    # interruption cancels this task (see handle_interruptions),
                    # so a parked frame never plays after a commit; teardown
                    # (EndFrame) is never held back.
                    await self._playback_gate.wait()
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

    # ── provisional playback pause (voice_runtime.playback_duck) ─────────
    def pause_playback(self) -> None:
        """Stop dequeuing bot audio to the wire; queued audio is preserved."""
        for sender in self._media_senders.values():
            sender.pause_playback()

    def resume_playback(self) -> None:
        """Continue playback where the pause stopped it."""
        for sender in self._media_senders.values():
            sender.resume_playback()

    @property
    def playback_paused(self) -> bool:
        return any(sender.playback_paused for sender in self._media_senders.values())

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


logger = logging.getLogger(__name__)


class FillerWebsocketOutputTransport(FillerOutputTransportMixin, FastAPIWebsocketOutputTransport):
    """Retire filler pacing and send its targeted clear before response PCM."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._filler_send_lock = asyncio.Lock()
        self._cleared_fillers = set()
        self._echo_reference = None

    def attach_echo_reference(self, reference) -> None:
        """Feed every audio frame to ``reference`` (voice_runtime.echo_reference)
        at the moment it goes on the wire — inside ``_write_frame``, BEFORE the
        socket send. A tap after the transport only sees the frame once the
        pacing wait has passed (20–40 ms later), and on a fast echo path the
        caller leg returns the frame before that: the gate then has nothing
        to compare it against (2026-09-25: an 850 ms acknowledgement cue
        echoed at −15 dB opened the gate that way)."""
        self._echo_reference = reference

    def _note_wire_audio(self, frame) -> None:
        if self._echo_reference is None or not isinstance(frame, OutputAudioRawFrame) or not frame.audio:
            return
        try:
            self._echo_reference.add_output(frame.audio, frame.sample_rate)
        except Exception:  # noqa: BLE001 — evidence must never break audio
            logger.debug("echo reference feed failed", exc_info=True)

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
                    self._note_wire_audio(frame)
                    await self._client.send(payload)
                    return True
                return False
            # Enforce wire order even when an urgent clear and an already
            # queued response reach the sender concurrently.
            for owner in list(getattr(self, "_filler_owners", {}).values()):
                if owner.cancelled and owner.token not in self._cleared_fillers:
                    await super()._write_frame(FillerClearFrame(owner))
                    self._cleared_fillers.add(owner.token)
            self._note_wire_audio(frame)
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
