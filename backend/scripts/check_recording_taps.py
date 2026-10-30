"""Offline regressions for the recording taps (no services/providers needed).

Run: python backend/scripts/check_recording_taps.py

Compile the production recording classes from their ASTs and use lightweight
frame/task adapters, avoiding model downloads and database configuration.
The production stereo aligner and tap/queue/finalization logic run unmodified.
This does not replace a Pipecat integration or live-call validation.
"""

import ast
import asyncio
import logging
import struct
import time
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
CLOCK = SimpleNamespace(monotonic=time.monotonic)


class Frame:
    pass


class InputAudioRawFrame(Frame):
    def __init__(self, audio, sample_rate=8000):
        self.audio = audio
        self.sample_rate = sample_rate


class OutputAudioRawFrame(Frame):
    def __init__(self, audio, sample_rate=8000):
        self.audio = audio
        self.sample_rate = sample_rate


class EndFrame(Frame):
    pass


class CancelFrame(Frame):
    pass


class FrameDirection(Enum):
    DOWNSTREAM = 1
    UPSTREAM = 2


class FrameProcessor:
    def __init__(self):
        self.forwarded = []

    async def process_frame(self, frame, direction):
        pass

    async def push_frame(self, frame, direction):
        self.forwarded.append((frame, direction))

    def create_task(self, coroutine):
        return asyncio.create_task(coroutine)

    async def cancel_task(self, task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class Resampler:
    async def resample(self, audio, source_rate, target_rate):
        assert source_rate == target_rate == 8000
        return audio


class Writer:
    def __init__(self):
        self.chunks = []

    async def append(self, chunk, sample_rate, channels):
        assert sample_rate == 8000 and channels == 2
        self.chunks.append(chunk)

    def samples(self, channel):
        data = b"".join(self.chunks)
        return b"".join(data[i:i + 2] for i in range(channel * 2, len(data), 4))


def load_recording_classes():
    namespace = {
        "asyncio": asyncio, "time": CLOCK,
        "logger": logging.getLogger(__name__),
        "Frame": Frame, "FrameDirection": FrameDirection,
        "FrameProcessor": FrameProcessor,
        "InputAudioRawFrame": InputAudioRawFrame,
        "OutputAudioRawFrame": OutputAudioRawFrame,
        "EndFrame": EndFrame, "CancelFrame": CancelFrame,
        "CallRecordingWriter": Writer, "create_stream_resampler": Resampler,
    }
    for relative, names in (
        ("voice_runtime/recording.py", {"AlignedStereoRecorder"}),
        ("voice_runtime/pipeline.py", {"AlignedRecordingProcessor", "CallerRecordingTap"}),
    ):
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names]
        assert {node.name for node in nodes} == names
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["AlignedRecordingProcessor"], namespace["CallerRecordingTap"]


Recording, Tap = load_recording_classes()
DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


def pcm(value, samples=160):
    return struct.pack("<h", value) * samples


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.writer = Writer()
        self.recording = Recording(writer=self.writer, sample_rate=8000)
        self.tap = Tap(self.recording)

    async def asyncTearDown(self):
        await self.recording.stop_recording()

    async def test_repeated_gate_preroll_does_not_extend_or_duplicate_recording(self):
        expected = bytearray()
        with patch.object(CLOCK, "monotonic", return_value=100.0):
            for opening in range(18):
                preroll = []
                # Gate closes, forwards silence, then replays 220 ms on open.
                for _ in range(11):
                    original = pcm(8)
                    frame = InputAudioRawFrame(original)
                    await self.tap.process_frame(frame, DOWN)
                    expected.extend(original)
                    self.assertIs(self.tap.forwarded[-1][0], frame)
                    self.assertEqual(frame.audio, original)
                    preroll.append(original)
                    frame.audio = bytes(len(original))
                    await self.recording.process_frame(frame, DOWN)
                for buffered in preroll:
                    await self.recording.process_frame(InputAudioRawFrame(buffered), DOWN)
                for _ in range(5):
                    frame = InputAudioRawFrame(pcm(1000 + opening))
                    expected.extend(frame.audio)
                    await self.tap.process_frame(frame, DOWN)
                    await self.recording.process_frame(frame, DOWN)
            await self.recording.stop_recording()
        self.assertEqual(self.writer.samples(0), bytes(expected))
        self.assertEqual(len(self.writer.samples(0)), 18 * 16 * 320)
        self.assertEqual(self.writer.samples(1), bytes(len(expected)))

    async def test_bot_audio_recorded_once_after_output(self):
        bot = OutputAudioRawFrame(pcm(2000, 320))
        await self.recording.process_frame(bot, DOWN)
        caller = InputAudioRawFrame(pcm(1000, 320))
        await self.tap.process_frame(caller, DOWN)
        await self.recording.process_frame(caller, DOWN)
        await self.recording.stop_recording()
        self.assertEqual(self.writer.samples(0), caller.audio)
        self.assertEqual(self.writer.samples(1), bot.audio)
        self.assertIs(self.recording.forwarded[0][0], bot)

    async def test_upstream_audio_is_not_recorded(self):
        await self.tap.process_frame(InputAudioRawFrame(pcm(1)), UP)
        await self.recording.process_frame(OutputAudioRawFrame(pcm(2)), UP)
        await self.recording.stop_recording()
        self.assertEqual(self.writer.chunks, [])

    async def test_ingress_end_waits_for_output_and_finalization_is_idempotent(self):
        await self.tap.process_frame(InputAudioRawFrame(pcm(1000)), DOWN)
        end = EndFrame()
        await self.tap.process_frame(end, DOWN)
        self.assertFalse(self.recording._stopped)
        await self.recording.process_frame(OutputAudioRawFrame(pcm(2000)), DOWN)
        await self.recording.process_frame(end, DOWN)
        original = b"".join(self.writer.chunks)
        await self.recording.process_frame(CancelFrame(), DOWN)
        await self.recording.stop_recording()
        await self.tap.process_frame(InputAudioRawFrame(pcm(3000)), DOWN)
        self.assertEqual(b"".join(self.writer.chunks), original)
        # Bot audio arrived after the first caller frame; preserve its position.
        self.assertEqual(self.writer.samples(0), pcm(1000) + bytes(320))
        self.assertEqual(self.writer.samples(1), bytes(320) + pcm(2000))

    async def test_writer_queue_drains_across_chunk_boundary(self):
        with patch.object(CLOCK, "monotonic", return_value=100.0):
            for _ in range(550):
                await self.tap.process_frame(InputAudioRawFrame(pcm(1234)), DOWN)
            await self.recording.stop_recording()
        self.assertEqual(self.writer.samples(0), pcm(1234) * 550)
        self.assertGreaterEqual(len(self.writer.chunks), 2)
        self.assertIsNone(self.recording._write_task)


if __name__ == "__main__":
    unittest.main()
