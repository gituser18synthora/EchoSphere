"""Sentence-level TTS playback with gapless output and barge-in interruption.

Barge-in check points
=====================
1. DURING playback of each sentence — polls barge_in_event every 20 ms
   and calls sd.stop() if it fires.
2. BETWEEN sentences — checks before synthesising the next one, so we
   don't waste a TTS API call that will never be played.

NOT checked BEFORE the first sentence — this was the bug that caused
every response to be skipped when barge_in_event was stale from the
previous turn. ContinuousAudio.play() now clears the event at entry so
it is always clean by the time stream_sentences() is called.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd

from voicebot.audio.pcm_utils import join_pcm_chunks, prepare_pcm_for_playback
from voicebot.audio.tts_text import sanitize_for_tts

if TYPE_CHECKING:
    from voicebot.adapters.base import TTSAdapter
    from voicebot.audio.continuous_io import ContinuousAudio

logger = logging.getLogger(__name__)

CROSSFADE_MS = 10


class TTSStreamPlayer:
    def __init__(
        self,
        tts_adapter: TTSAdapter,
        suppression_flag: asyncio.Event,
        sample_rate: int = 8000,
        playback_chunk_samples: int = 1600,
        barge_in_event: asyncio.Event | None = None,
        continuous_audio: "ContinuousAudio | None" = None,
    ) -> None:
        self._tts_adapter = tts_adapter
        self._suppression_flag = suppression_flag
        self._sample_rate = sample_rate
        self._playback_chunk_samples = playback_chunk_samples
        self.played_to_speaker: bool = False
        self.characters_synthesized: int = 0
        self._play_lock = asyncio.Lock()
        self._barge_in_event = barge_in_event
        self._continuous_audio = continuous_audio

    def _barge_in_active(self) -> bool:
        return (
            self._barge_in_event is not None
            and self._barge_in_event.is_set()
        )

    async def stream_sentences(
        self,
        sentences: list[str],
        *,
        voice_id: str,
        speed: float = 1.0,
        pitch: float = 1.0,
    ) -> bytes:
        raw_valid = [s for s in sentences if s and s.strip()]
        if not raw_valid:
            return b""

        valid = [
            sanitize_for_tts(
                s,
                ensure_terminal_punct=(i == len(raw_valid) - 1),
            )
            for i, s in enumerate(raw_valid)
        ]
        valid = [s for s in valid if s]
        if not valid:
            return b""

        if self._continuous_audio is not None:
            self._continuous_audio.reset_barge_in_state()

        self._suppression_flag.set()
        self.played_to_speaker = False
        self.characters_synthesized = 0
        all_pcm: list[bytes] = []
        loop = asyncio.get_running_loop()
        collect_task: asyncio.Task[bytes] | None = None
        barge_in_occurred = False

        try:
            for i, sentence in enumerate(valid):

                # Collect TTS audio for this sentence.
                if collect_task is None:
                    pcm = await self._collect_sentence(
                        sentence, voice_id, speed, pitch,
                    )
                else:
                    try:
                        pcm = await collect_task
                    except asyncio.CancelledError:
                        logger.info("[Barge-in] Pre-fetch task cancelled")
                        break
                collect_task = None

                # ── BETWEEN-SENTENCE barge-in check ─────────────────────
                # Only skip synthesising/playing if barge-in fired DURING
                # the previous sentence's playback. Never checked before
                # sentence 0 — the event is guaranteed clean at this point
                # because ContinuousAudio.play() cleared it before calling us.
                if i > 0 and self._barge_in_active():
                    barge_in_occurred = True
                    logger.info(
                        "[Barge-in] Skipping sentence %d/%d — customer speaking",
                        i + 1, len(valid),
                    )
                    break

                # Pre-fetch next sentence while we play this one.
                if i + 1 < len(valid):
                    collect_task = asyncio.create_task(
                        self._collect_sentence(
                            valid[i + 1], voice_id, speed, pitch,
                        ),
                    )

                if not pcm:
                    continue

                is_first = i == 0
                is_last = i == len(valid) - 1
                pcm_play = prepare_pcm_for_playback(
                    pcm,
                    sample_rate=self._sample_rate,
                    crossfade_ms=CROSSFADE_MS,
                    fade_in=not is_first,
                    fade_out=not is_last,
                )
                all_pcm.append(pcm_play)
                self.played_to_speaker = True

                async with self._play_lock:
                    stopped = await self._play_with_barge_in(
                        loop, pcm_play,
                    )

                if stopped:
                    barge_in_occurred = True
                    logger.info(
                        "[Barge-in] Stopped mid-sentence %d/%d",
                        i + 1, len(valid),
                    )
                    if collect_task is not None:
                        collect_task.cancel()
                    break

        finally:
            self._suppression_flag.clear()
            if barge_in_occurred and self._continuous_audio is not None:
                self._continuous_audio.handoff_barge_in_captured()
            if self._continuous_audio is not None:
                self._continuous_audio.reset_barge_in_state()

        return b"".join(all_pcm)

    async def _play_with_barge_in(
        self,
        loop: asyncio.AbstractEventLoop,
        pcm: bytes,
    ) -> bool:
        """
        Play one sentence. Polls barge_in_event every 20 ms.
        Returns True if interrupted, False if completed normally.
        """
        if not pcm or len(pcm) < 2:
            return False

        arr = np.frombuffer(pcm, dtype=np.int16)
        if arr.size == 0:
            return False

        if self._barge_in_event is None:
            # No barge-in configured — simple blocking play.
            await loop.run_in_executor(None, self._blocking_play, pcm)
            return False

        sd.play(arr, samplerate=self._sample_rate)
        interrupted = False

        while sd.get_stream().active:
            if self._barge_in_active():
                sd.stop()
                interrupted = True
                break
            await asyncio.sleep(0.02)

        if not interrupted:
            await loop.run_in_executor(None, sd.wait)

        return interrupted

    async def _collect_sentence(
        self,
        sentence: str,
        voice_id: str,
        speed: float,
        pitch: float,
    ) -> bytes:
        self.characters_synthesized += len(sentence)
        parts: list[bytes] = []
        try:
            async for chunk in self._tts_adapter.synthesize_stream(
                text=sentence,
                voice_id=voice_id,
                speed=speed,
                pitch=pitch,
            ):
                if chunk:
                    if len(chunk) % 2 == 1:
                        chunk = chunk[:-1]
                    parts.append(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "TTS collect failed for sentence %r: %s",
                sentence[:80], e,
            )
        return join_pcm_chunks(
            parts,
            sample_rate=self._sample_rate,
            crossfade_ms=CROSSFADE_MS,
        )

    def _blocking_play(self, pcm: bytes) -> None:
        if not pcm or len(pcm) < 2:
            return
        arr = np.frombuffer(pcm, dtype=np.int16)
        if arr.size == 0:
            return
        sd.play(arr, samplerate=self._sample_rate)
        sd.wait()