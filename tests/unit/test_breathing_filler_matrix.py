"""Breathing × filler words through the real brain, TTS router, latency filler
and Pipecat output path.

Only the LLM and TTS providers and the audio device are local fakes; the
brain's dispatch/arm/cancel logic, the planner's context decisions, the
processor's schedule and the ownership-preserving output are the real code.
Each combination of the two family switches runs the same conversation:
an explanation, a question with a fast reply, a problem report, a short
answer, a long reply arriving mid-filler, a barge-in, and follow-up turns —
and the written audio timeline proves which family sounded, that the other
one never did, that the reply was never held back and that no stale filler
audio survives reply audio or an interruption.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict

import pytest
from pipecat.frames.frames import (
    EndFrame, InterruptionFrame, StartFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.workers.runner import WorkerRunner

import voice_runtime.latency_filler as latency_filler_module
from shared.bot_config import ResolvedBotConfig
from shared.orchestration.naturalness import _POOLS, SpeechNaturalnessPlanner, normalize_spoken_variant
from shared.providers.tts.streaming import TTSStreamEvent, TTSStreamSettings
from voice_runtime.brain import ConversationBrain
from voice_runtime.filler_transport import FillerOutputTransportMixin
from voice_runtime.frames import FillerAudioRawFrame
from voice_runtime.pipeline import build_latency_filler
from voice_runtime.tts_router import StreamingTTSRouter
from voice_runtime.turn_metrics import TurnLatencyTracker
from tests.unit.test_latency_filler import _AcknowledgementCueStub, _ShortLibrary
from tests.unit.test_naturalness_runtime import _RecorderStub
from tests.unit.test_tts_router_flush_finals import FakeProvider
from tests.unit.test_turn_endpointing import transcript

# Sample values that label each sound family on the written timeline.
REPLY, ACK, HMM, WAIT, BREATH = 12000, 7000, 4000, 5000, 2000   # breath = _ShortLibrary female
LABELS = {REPLY: "reply", ACK: "ack", HMM: "hmm", WAIT: "wait", BREATH: "breath"}
DELAY_S, HMM_S, WAIT_S = 0.5, 0.9, 1.3
SLOW = 0.95


class Recorder(_RecorderStub):
    def __init__(self):
        super().__init__()
        self.usage = defaultdict(int)

    def add_tts_usage(self, **kwargs):
        pass

    def data(self, kind):
        return [d for k, d in self.events if k == kind]


class LLM:
    last_stream_usage = None

    def __init__(self, state):
        self.state = state
        self.started = []

    async def stream(self, *args, **kwargs):
        self.started.append(time.monotonic())
        await asyncio.sleep(self.state["llm"])
        for piece in self.state["reply"]:
            yield piece


class Provider(FakeProvider):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.tasks = {}
        self.requested = []

    async def flush(self, generation_id):
        if generation_id not in self.tasks:
            self.requested.append(time.monotonic())
            self.tasks[generation_id] = asyncio.create_task(self.render(generation_id))

    async def finish(self, generation_id):
        await self.flush(generation_id)

    async def render(self, generation_id):
        await asyncio.sleep(self.state["tts"])
        await self._emit(TTSStreamEvent(
            kind="audio", generation_id=generation_id,
            audio=REPLY.to_bytes(2, "little") * 3840,      # 240 ms at 16 kHz
        ))
        await self._emit(TTSStreamEvent(kind="final", generation_id=generation_id))

    async def cancel(self, generation_id):
        self._end_generation(generation_id)
        if generation_id in self.tasks:
            self.tasks[generation_id].cancel()

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


class Router(StreamingTTSRouter):
    def _stream_settings(self, engine, locale, *, speed=None):
        return TTSStreamSettings(
            provider="sarvam", model="bulbul:v3", voice="anushka",
            language=locale, sample_rate=16000,
        )


class CueStub(_AcknowledgementCueStub):
    """Voiced cues for the ladder, honouring the planner's per-turn selection."""

    def __init__(self):
        super().__init__(clip_ms=240)
        self.last_cue_id = None
        self.voiced = []

    def warm(self, engine, language, selection=None):
        self.warmed.append((engine, language, selection))

    def clip(self, engine, language, kind, sample_rate, selection=None):
        if kind == "hmm":
            self.last_cue_id = (selection or {}).get("primary") or "hmm"
            self.voiced.append(self.last_cue_id)
        return super().clip(engine, language, kind, sample_rate)


class Output(FillerOutputTransportMixin, BaseOutputTransport):
    def __init__(self):
        super().__init__(TransportParams(
            audio_out_enabled=True, audio_out_sample_rate=16000,
            audio_out_10ms_chunks=2, audio_out_end_silence_secs=0,
        ))
        self.writes = []          # (monotonic, label, owner, cancelled_at_write, mixed)
        self.interrupts = 0

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            self.interrupts += 1

    async def write_audio_frame(self, frame):
        values = set(memoryview(frame.audio).cast("h"))
        present = [LABELS[v] for v in LABELS if v in values]
        label = present[0] if present else "silence"
        owner = frame.owner if isinstance(frame, FillerAudioRawFrame) else None
        self.writes.append((
            time.monotonic(), label, owner, owner.cancelled if owner else None,
            "reply" in present and len(present) > 1,
        ))
        await asyncio.sleep(len(frame.audio) / 32000)
        return True


async def until(predicate, timeout=4.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.002)


async def run_conversation(*, breathing: bool, words: bool, monkeypatch):
    monkeypatch.setattr(latency_filler_module, "_MIN_RUNG_GAP_S", 0.05)
    monkeypatch.setattr(latency_filler_module, "_AFTER_ACK_GAP_S", 0.05)
    monkeypatch.setattr(latency_filler_module, "_RESUME_GAP_S", 0.05)
    state = {"llm": 0.01, "tts": 0.03, "reply": ["ठीक है, समझ गया।"]}
    recorder = Recorder()
    tracker = TurnLatencyTracker(session_id=recorder.session_id)
    planner = SpeechNaturalnessPlanner({
        "enabled": True, "breathing": breathing, "filler_words": words,
        "acknowledgement_probability": 1.0, "latency_cue_probability": 1.0,
        "backchannels": False, "sentence_breaths": False,
        "latency_filler_delay_ms": int(DELAY_S * 1000),
    }, rng=random.Random(11))
    cues = CueStub()
    library = _ShortLibrary(clip_ms=400)      # breath: 0.5–0.9 s after the caller stops
    filler = build_latency_filler(
        planner, sample_rate=16000, recorder=recorder, library=library, cue_library=cues,
    )
    arms = []
    if filler is not None:
        # Fast ladder for the test clock (the bounds-clamped config would
        # need 2–3 s per rung).
        if filler.ladder_enabled:
            filler._rung_delays_s = {"breath": DELAY_S, "hmm": HMM_S, "wait": WAIT_S}
        real_arm = filler.arm

        async def recording_arm(**kwargs):
            arms.append(dict(kwargs))
            await real_arm(**kwargs)

        filler.arm = recording_arm
    config = ResolvedBotConfig(
        tenant_id="matrix", bot_id="matrix", bot_name="Matrix", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="Answer briefly.",
        tts={"provider": "sarvam", "model": "bulbul:v3", "voice": "anushka", "voice_gender": "female"},
    )
    llm, provider, output = LLM(state), Provider(state), Output()
    brain = ConversationBrain(
        config=config, llm=llm, recorder=recorder, latency=tracker,
        naturalness=planner, latency_filler=filler,
        finalize_grace=0.02, complete_endpoint=0.02, short_reply_endpoint=0.02,
    )
    tts = Router(
        tts_config=config.tts, language="hi-IN", sample_rate=16000,
        provider_factory=lambda settings: provider, recorder=recorder, latency=tracker,
    )
    processors = [brain, tts] + ([filler] if filler is not None else []) + [output]
    worker = PipelineWorker(
        Pipeline(processors),
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000),
        enable_rtvi=False, idle_timeout_secs=None,
    )
    turns = []

    async def caller_says(text, *, llm_delay, tts_delay, reply=None, barge_in_after=None):
        state.update(llm=llm_delay, tts=tts_delay, reply=reply or ["ठीक है, समझ गया।"])
        tracker.mark_speech_started()
        await worker.queue_frame(UserStartedSpeakingFrame())
        await asyncio.sleep(0.03)
        tracker.mark_speech_stopped()
        stopped = time.monotonic()
        # A plausible speaking rate for the transcript quality gate (the
        # helper's default 1 s would make a 17-word utterance "impossible").
        await worker.queue_frame(transcript(
            text, metrics={"audio_duration": max(1.0, len(text.split()) / 3.0)},
        ))
        await worker.queue_frame(UserStoppedSpeakingFrame())
        record = {"text": text, "stopped": stopped, "interrupted_at": None}
        if barge_in_after is not None:
            await asyncio.sleep(barge_in_after)
            record["interrupted_at"] = time.monotonic()
            await worker.queue_frame(InterruptionFrame())
            tracker.mark_speech_started()
            await worker.queue_frame(UserStartedSpeakingFrame())
            await until(lambda: output.interrupts == sum(1 for t in turns if t["interrupted_at"]) + 1)
            # The caller finishes a new thought right away.
            await asyncio.sleep(0.05)
            record["end"] = time.monotonic()
            turns.append(record)
            return record
        # This turn's window closes once ITS reply has been written and had
        # time to play out, so slow turns never leak into the next window.
        await until(
            lambda: any(at > stopped and label == "reply" for at, label, *_ in output.writes),
            timeout=8.0,
        )
        await asyncio.sleep(0.5)
        record["end"] = time.monotonic()
        turns.append(record)
        return record

    async def feed():
        await until(lambda: bool(output._media_senders))
        # 1. The caller explains something at length; the answer is slow.
        await caller_says(
            "मैंने कस्टमर को कॉल किया था, उन्होंने फ़ोन नहीं उठाया, फिर मैं घर पर गया और सामान दे दिया",
            llm_delay=SLOW, tts_delay=0.03,
        )
        # 2. A question with a fast reply: nothing may play in any combination.
        await caller_says("मेरा पेमेंट कब आएगा", llm_delay=0.01, tts_delay=0.03)
        # 3. The caller reports a problem; the answer is slow.
        await caller_says("मेरा पैसा कट गया और ऑर्डर नहीं मिला", llm_delay=SLOW, tts_delay=0.03)
        # 4. A short answer, slow reply (no acknowledgement two turns in a
        #    row → the ladder's cue gets its chance).
        await caller_says("हाँ", llm_delay=SLOW, tts_delay=0.03)
        # 5. A long reply whose first audio lands mid-filler (~0.67 s, inside
        #    the 0.5–0.9 s breath / 0.5–0.74 s acknowledgement): the filler is
        #    cut before that audio, never after it.
        await caller_says(
            "ठीक है बताइए", llm_delay=0.01, tts_delay=0.6,
            reply=["आपकी बात समझ ली है। ", "मैं इसे अभी नोट कर रहा हूँ। ", "एक मिनट में पूरी जानकारी देता हूँ।"],
        )
        # 6. Barge-in while a filler is running (breath ends ~0.8 s, the cue
        #    starts at 0.9 s): the interruption lands at 1.0 s in every
        #    combination that has something playing.
        await caller_says("अच्छा एक और बात", llm_delay=0.01, tts_delay=SLOW + 0.9, barge_in_after=1.0)
        # 7. The new thought after the interruption is answered normally.
        await caller_says("मुझे कल का स्लॉट चाहिए", llm_delay=SLOW, tts_delay=0.03)
        await asyncio.sleep(0.2)
        await worker.queue_frame(EndFrame())

    feeder = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(WorkerRunner(handle_sigint=False).run(worker), timeout=40)
        await feeder
    finally:
        if not feeder.done():
            feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
    return {
        "turns": turns, "writes": output.writes, "recorder": recorder, "arms": arms,
        "filler": filler, "brain": brain, "cues": cues, "library": library, "planner": planner,
    }


def timeline(run, turn):
    """(offset_s, label, owner_cancelled_at_write) for every SOUND written in
    this turn (a filler clip's zero padding to the chunk size is not one)."""
    start, end = turn["stopped"], turn["end"]
    return [
        (at - start, label, cancelled)
        for at, label, _owner, cancelled, _mixed in run["writes"]
        if start <= at < end and label != "silence"
    ]


def first_by_label(rows):
    first = {}
    for offset, label, _ in rows:
        first.setdefault(label, offset)
    return first


COMBOS = [(True, True), (True, False), (False, True), (False, False)]


@pytest.mark.parametrize("breathing,words", COMBOS, ids=lambda v: str(v))
async def test_each_family_sounds_only_when_its_own_switch_is_on(breathing, words, monkeypatch):
    run = await run_conversation(breathing=breathing, words=words, monkeypatch=monkeypatch)
    turns, writes = run["turns"], run["writes"]
    assert len(turns) == 7
    labels = {label for _, label, *_ in writes if label != "silence"}   # clip zero padding is not a sound
    assert "reply" in labels                                   # every combination answers
    assert not any(mixed for *_, mixed in writes)              # never filler and reply in one frame
    assert not any(cancelled for *_, cancelled, _ in writes)   # retired filler never reaches the wire

    if not breathing and not words:
        assert run["filler"] is None and run["brain"]._latency_filler is None
        assert labels == {"reply"}
        return

    breath_sounds = {"breath"} if breathing else set()
    word_sounds = {"ack", "hmm", "wait"} if words else set()
    assert labels - {"reply"} <= breath_sounds | word_sounds, labels
    if breathing:
        assert "breath" in labels
    if words:
        assert "ack" in labels and "hmm" in labels

    # Turn 1 — explanation, slow answer: the first sound is at the deadline
    # and belongs to the right family; the reply is never held back.
    first = first_by_label(timeline(run, turns[0]))
    expected_first = "ack" if words else "breath"
    assert expected_first in first and first[expected_first] >= DELAY_S - 0.02, first
    assert first["reply"] < SLOW + 0.6
    if words:
        assert "breath" not in first          # a ready acknowledgement replaces the breath
        assert "hmm" not in first             # one voice per wait, not two
    # Turn 2 — fast reply: nothing but the reply in any combination.
    assert set(first_by_label(timeline(run, turns[1]))) == {"reply"}
    # Turn 3 — problem report, slow: the family sounds again; the
    # acknowledgement is a neutral listening token, nothing bright.
    third = first_by_label(timeline(run, turns[2]))
    assert expected_first in third
    # Turn 4 — short answer, slow: no acknowledgement two turns in a row, so
    # the words family falls through to the voiced cue; the breath family
    # simply breathes again.
    fourth = first_by_label(timeline(run, turns[3]))
    if words:
        assert "ack" not in fourth and "hmm" in fourth and fourth["hmm"] >= HMM_S - 0.05, fourth
    if breathing and not words:
        assert set(fourth) == {"breath", "reply"}
    if breathing and words:
        assert "breath" in fourth and fourth["breath"] < fourth["hmm"]
    # Turn 5 — reply audio lands mid-filler: nothing of the filler is
    # written after the first reply frame.
    fifth = timeline(run, turns[4])
    reply_at = next(offset for offset, label, _ in fifth if label == "reply")
    assert 0.5 < reply_at < 0.9, fifth                       # landed while the filler was running
    assert all(label == "reply" for offset, label, _ in fifth if offset >= reply_at), fifth
    cuts = [c for c in run["recorder"].data("latency_filler_cut") if c["reason"] == "tts_audio"]
    assert cuts, "reply audio never cut a running filler"
    # Turn 6 — barge-in: nothing of the filler is written after the
    # interruption, and the armed turn is gone.
    sixth = turns[5]
    after = [label for at, label, *_ in writes
             if at >= sixth["interrupted_at"] and at < sixth["end"] and label != "silence"]
    assert "reply" not in after and not any(label in ("breath", "ack", "hmm", "wait") for label in after), after
    before = [label for at, label, *_ in writes
              if sixth["stopped"] <= at < sixth["interrupted_at"] and label != "silence"]
    assert before, "the filler never started before the barge-in"
    assert run["filler"]._armed is None or run["filler"]._armed.turn_id != 6
    # Turn 7 — the conversation continues normally after the interruption.
    seventh = first_by_label(timeline(run, turns[6]))
    assert "reply" in seventh and seventh["reply"] < SLOW + 0.6

    # Consecutive-turn repetition: no two consecutive acknowledgement turns,
    # and no voiced cue word right after the same word as an acknowledgement.
    recorder = run["recorder"]
    ack_turns = [e["turn"] for e in recorder.data("early_ack_played")]
    assert all(b - a >= 2 for a, b in zip(ack_turns, ack_turns[1:])), ack_turns
    if words:
        acks = {arm["turn_id"]: arm["acknowledgement"]["text"] for arm in run["arms"]
                if arm.get("acknowledgement")}
        assert acks, run["arms"]
        neutral = {normalize_spoken_variant(t) for t in _POOLS["hi"]["ack_neutral"]}
        information = {normalize_spoken_variant(t) for t in _POOLS["hi"]["ack_information"]}
        assert normalize_spoken_variant(acks[1]) in information, acks      # explanation → listening
        assert normalize_spoken_variant(acks[3]) in neutral, acks          # problem → neutral only
        theek = normalize_spoken_variant("ठीक है…")
        assert theek not in {normalize_spoken_variant(acks[1]), normalize_spoken_variant(acks[3])}
        # The cue that played on turn 4 is not the word the turn-3 ack used.
        played_cues = [e.get("cue") for e in recorder.data("latency_filler_played") if e["rung"] == "hmm"]
        assert played_cues, recorder.data("latency_filler_played")
        turn3_word = normalize_spoken_variant(acks[3])
        from shared.orchestration.naturalness import ladder_cue_text
        assert normalize_spoken_variant(ladder_cue_text("hi-IN", "hmm", played_cues[0])) != turn3_word
    if not breathing:
        assert run["library"].requests == []       # the breath library was never asked
        skipped = recorder.data("latency_filler_skipped")
        assert any(s["reason"] == "breathing_off" for s in skipped), skipped
    if not words:
        assert run["cues"].requests == [] and run["cues"].ack_requests == []
        assert not recorder.data("early_ack_played")
        assert all(e["rung"] == "breath" for e in recorder.data("latency_filler_played"))
