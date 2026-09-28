"""Ownership-preserving output for disposable latency filler PCM.

Normal response audio retains Pipecat's output path. Filler never enters its
untyped partial-chunk buffer, and retired filler can be dropped at every queue
boundary without resetting a call's valid audio or interruption state.

Optional background ambience (voice_runtime.ambience) is mixed in here, at the
last point before the serializer — see ``attach_ambience``.
"""

import asyncio
import time

import logging

from pipecat.frames.frames import (
    CancelFrame, EndFrame, InterruptionFrame, OutputAudioRawFrame,
    OutputTransportMessageFrame, OutputTransportMessageUrgentFrame,
    OutputTransportReadyFrame, StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_output import BOT_VAD_STOP_FALLBACK_SECS, BaseOutputTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketOutputTransport, FastAPIWebsocketTransport,
)

from shared.audio.pcm import resample_pcm
from voice_runtime.frames import (
    AUDIO_FLUSH_MESSAGE_TYPE, AmbienceAudioRawFrame, FillerAudioRawFrame, FillerClearFrame,
)

# Timer slack when deciding that an ambience chunk is due.
_AMBIENCE_DUE_TOLERANCE_S = 0.001


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
            ambience = self._room_ambience()
            if ambience is not None:
                async for frame in self._next_frame_with_ambience(ambience):
                    yield frame
                return
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

        def _room_ambience(self):
            """The call's background ambience (default destination only)."""
            if self._destination is not None:
                return None
            return getattr(self._transport, "_ambience", None)

        async def _next_frame_with_ambience(self, ambience):
            """``_next_frame`` while background ambience is on.

            Queued frames, their order, and every pause / interruption /
            teardown rule are exactly the plain path's. In addition, while no
            bot audio is playing — between turns, or during a provisional
            pause — an ambience-only frame is yielded each time the playout
            horizon drains to the ambience lead, so the room stays audible.
            Queued frames always win: the ambience deadline only bounds the
            wait for them, it never replaces it with a sleep.

            No room audio is inserted while the bot is speaking: a reply is
            one continuous paced stream (it already carries the ambience),
            and interleaving would stretch it.
            """
            ambience.begin()
            last_frame_time = time.time()   # the plain path's 3 s bot-stopped fallback
            last_audio_at = 0.0             # monotonic: last bot audio written
            held = None                     # dequeued just as a pause began
            while True:
                if self._playback_paused:
                    # Nothing queued may reach the wire; the room carries on.
                    wait = ambience.idle_wait(time.monotonic())
                    if wait is not None and wait <= _AMBIENCE_DUE_TOLERANCE_S:
                        yield ambience.idle_frame(flush_pending=False)
                        continue
                    try:
                        await asyncio.wait_for(self._playback_gate.wait(), timeout=wait)
                    except TimeoutError:
                        pass
                    continue
                if held is not None:
                    frame, held = held, None
                else:
                    if not self._audio_queue.empty():
                        # Queued frames always win, taken directly: wait_for()
                        # with a zero timeout cancels its get() before it runs,
                        # so it never returns an item even when one is waiting
                        # (an overdue room chunk + a queued reply livelocked).
                        frame = self._audio_queue.get_nowait()
                    else:
                        wait = None
                        if not self._bot_speaking:
                            wait = ambience.idle_wait(time.monotonic(), last_audio_at=last_audio_at)
                        if wait is not None and wait <= _AMBIENCE_DUE_TOLERANCE_S:
                            yield ambience.idle_frame(flush_pending=ambience.audio_since_idle)
                            continue
                        fallback = last_frame_time + BOT_VAD_STOP_FALLBACK_SECS - time.time()
                        timeout = fallback if wait is None else min(wait, fallback)
                        try:
                            if timeout <= 0:
                                raise TimeoutError
                            frame = await asyncio.wait_for(self._audio_queue.get(), timeout=timeout)
                        except TimeoutError:
                            if time.time() - last_frame_time >= BOT_VAD_STOP_FALLBACK_SECS:
                                # As the plain path: no frame at all for 3 s.
                                await self._bot_stopped_speaking()
                                last_frame_time = time.time()
                            continue
                    last_frame_time = time.time()
                    if isinstance(frame, FillerAudioRawFrame) and (
                        frame.owner is None or frame.owner.cancelled
                    ):
                        self._audio_queue.task_done()
                        continue
                    if isinstance(frame, EndFrame):
                        # The call is ending: the room stops with the last
                        # queued audio (end-of-call silence stays silent).
                        ambience.stop("end")
                    elif self._playback_paused:
                        held = frame   # plays when the pause ends
                        continue
                yield frame
                self._audio_queue.task_done()
                if isinstance(frame, OutputAudioRawFrame):
                    last_audio_at = time.monotonic()
                elif (
                    isinstance(frame, OutputTransportMessageFrame)
                    and (frame.message or {}).get("type") == AUDIO_FLUSH_MESSAGE_TYPE
                ):
                    # A latency-filler clip completed (telephony marker):
                    # none of its audio is still coming, so the room resumes
                    # at once instead of after the confirmation window.
                    last_audio_at = 0.0

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
        self._ambience = None

    def attach_ambience(self, ambience) -> None:
        """Background ambience (voice_runtime.ambience.AmbienceMixer) for this
        call. Attach before the pipeline starts; without it (the default)
        every code path below is exactly the plain one.

        - Bot audio is mixed with the room sound in ``_write_frame`` AFTER
          the echo reference has been fed the clean frame and BEFORE the
          serializer encodes it. The frame the media sender pushes on to
          the recorder is the clean original.
        - Ambience-only frames (between turns) are written by the media
          sender on the playout-horizon clock; they are never fed to the
          echo reference and never pushed downstream.
        """
        self._ambience = ambience

    @staticmethod
    def _audio_seconds(frame) -> float:
        rate = frame.sample_rate * max(1, frame.num_channels) * 2
        return len(frame.audio) / rate if rate else 0.0

    def _packet_backlog(self) -> int | None:
        """Bytes a packetizing serializer (FreeSWITCH, Vaani) still holds."""
        pending = getattr(self._params.serializer, "_pending_audio", None)
        return None if pending is None else len(pending)

    def _with_ambience(self, frame):
        """``frame`` with the room sound mixed in (a new frame), or ``frame``."""
        ambience = self._ambience
        if (
            ambience is None or not ambience.running
            or not isinstance(frame, OutputAudioRawFrame) or not frame.audio
            or frame.sample_rate != ambience.sample_rate or frame.num_channels != 1
        ):
            return frame
        audio = ambience.mix(frame.audio)
        ambience.audio_since_idle = True
        if isinstance(frame, FillerAudioRawFrame):
            return FillerAudioRawFrame(
                audio=audio, sample_rate=frame.sample_rate, num_channels=1, owner=frame.owner,
            )
        return OutputAudioRawFrame(audio=audio, sample_rate=frame.sample_rate, num_channels=1)

    async def _send_ambience_payload(self, payload) -> bool:
        if self._client.is_closing or not self._client.is_connected:
            return False
        try:
            await self._client.send(payload)
        except Exception as exc:  # noqa: BLE001 — the room must never break a call
            logger.debug("ambience send failed: %s", exc)
            return False
        return True

    async def _write_ambience(self, frame, ambience) -> bool:
        if not ambience.running:
            return False
        if self._client.is_closing or not self._client.is_connected:
            ambience.stop("disconnected")
            return False
        serializer = self._params.serializer
        pending = getattr(serializer, "_pending_audio", None)
        if frame.flush_pending and pending:
            # A reply just ended with its last partial packet still in the
            # serializer's buffer. It belongs BEFORE the room audio.
            seconds = len(pending) / (ambience.sample_rate * 2)
            payload = await serializer.serialize(
                OutputTransportMessageFrame(message={"type": AUDIO_FLUSH_MESSAGE_TYPE})
            )
            if payload and await self._send_ambience_payload(payload):
                ambience.note_sent(seconds)
                ambience.note_remnant_flushed()
        payload = await serializer.serialize(frame)
        if not payload or not await self._send_ambience_payload(payload):
            return False
        ambience.note_room_sent(self._audio_seconds(frame))
        ambience.idle_since_audio = True
        ambience.audio_since_idle = False
        return True

    async def _ambience_handoff(self, ambience) -> None:
        """Bot audio follows room audio: let the client drop the room audio it
        still has queued (the browser client does; telephony serializers
        return nothing — their queue plays out, at most one chunk + lead)."""
        payload = await self._params.serializer.serialize(FillerClearFrame(ambience.owner))
        cleared = bool(payload) and await self._send_ambience_payload(payload)
        ambience.note_handoff(cleared)

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
            ambience = self._ambience
            if ambience is not None:
                if isinstance(frame, AmbienceAudioRawFrame):
                    return await self._write_ambience(frame, ambience)
                ambience.arm()
            if isinstance(frame, FillerAudioRawFrame):
                if frame.owner is None or frame.owner.cancelled:
                    return
                if ambience is not None and ambience.idle_since_audio and frame.audio:
                    await self._ambience_handoff(ambience)
                # Tagged filler bypasses optional fixed-size PCM buffering:
                # it must never be concatenated with a real response packet.
                payload = await self._params.serializer.serialize(self._with_ambience(frame))
                if payload and not frame.owner.cancelled:
                    self._note_wire_audio(frame)
                    await self._client.send(payload)
                    if ambience is not None:
                        ambience.note_sent(self._audio_seconds(frame))
                    return True
                return False
            # Enforce wire order even when an urgent clear and an already
            # queued response reach the sender concurrently.
            for owner in list(getattr(self, "_filler_owners", {}).values()):
                if owner.cancelled and owner.token not in self._cleared_fillers:
                    await super()._write_frame(FillerClearFrame(owner))
                    self._cleared_fillers.add(owner.token)
            self._note_wire_audio(frame)
            if ambience is None:
                await super()._write_frame(frame)
                return
            await self._write_with_ambience(frame, ambience)

    async def _write_with_ambience(self, frame, ambience) -> None:
        serializer = self._params.serializer
        if ambience.idle_since_audio and isinstance(frame, OutputAudioRawFrame) and frame.audio:
            await self._ambience_handoff(ambience)
        backlog = self._packet_backlog()
        dropped = getattr(serializer, "stale_audio_dropped", 0)
        await super()._write_frame(self._with_ambience(frame))
        if isinstance(frame, OutputAudioRawFrame) and frame.audio:
            seconds = self._audio_seconds(frame)
            if backlog is not None:
                # Only what actually left the packet buffer reaches the far
                # end now; a stale remnant it discarded never does.
                if getattr(serializer, "stale_audio_dropped", 0) != dropped:
                    backlog = 0
                sent = backlog + len(frame.audio) - (self._packet_backlog() or 0)
                seconds = max(0, sent) / (frame.sample_rate * 2)
            ambience.note_sent(seconds)
        elif isinstance(frame, InterruptionFrame):
            ambience.note_interruption()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            ambience.stop("end" if isinstance(frame, EndFrame) else "cancel")

    async def write_audio_frame(self, frame):
        if isinstance(frame, AmbienceAudioRawFrame):
            # Paced by the media sender's playout clock, not by a sleep here:
            # a reply frame arriving meanwhile goes out at once. Never pushed
            # downstream — room audio is not recorded.
            await self._write_frame(frame)
            return False
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
