"""Sarvam realtime STT: what the always-open utterance is fed, which
transcripts may leave the adapter, and the handshake language policy.

Live call cv_a3f4eee0d0d2 (2026-10-22, SARVAM_REALTIME_STT=true, bot with
hi-IN + en-IN, auto-detect) showed three failures; replaying its recorded
audio into the realtime socket on 2026-10-23 reproduced each:

1. the caller's "haan boliye" came back as English ("Yes, speak." live,
   "Hello, speak." on replay) under ``language_code=auto``; pinned to hi-IN
   the same audio transcribes as Devanagari;
2. the bot's 19 s confirmation reply was cut after 16 s by a 2-word final
   ("சரிங்க சார்.", Tamil) that Sarvam produced from the gate's digital silence
   — no VAD, no speech_end from us; on replay 18 s of zeros yielded partials
   from 4 s on and a server-initiated final at ~18 s;
3. that silence text ("However, everyone should try to learn...") also
   landed in the caller's turns.

The adapter now (a) never sends exact-zero frames and pings instead,
(b) while the bot speaks (+0.6 s tail) sends audio only with Silero speech,
keeping a 0.4 s onset ring, (c) lets a transcript through only with VAD
evidence (caller speaking now, or a speech_end awaiting its final) and
(d) drops, in the echo window, text that is mostly the bot's own recent
words. ``build_stt_service`` pins single-language bots and forces
``mode=transcribe`` on this socket.
"""

from __future__ import annotations

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transcriptions.language import Language

import voice_runtime.barge_in as barge_in_module
from voice_runtime import sarvam_realtime_stt as rt
from voice_runtime.barge_in import WordConfirmedBargeInStrategy
from voice_runtime.spoken_text import SpokenTextReference, normalize_words

RATE = 8000
FRAME = RATE * 2 * 20 // 1000        # 20 ms


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def add_event(self, kind: str, **data) -> None:
        self.events.append((kind, data))

    def of(self, kind: str) -> list[dict]:
        return [d for k, d in self.events if k == kind]


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(rt.time, "monotonic", c)
    monkeypatch.setattr(barge_in_module.time, "monotonic", c)
    return c


def _frame(i: int) -> bytes:
    return bytes([1 + (i % 250)]) * FRAME


class Harness:
    """A realtime adapter with an open socket and announced utterance, whose
    sends and pushes are captured."""

    def __init__(self, monkeypatch, *, recorder=None, spoken=None, endpointing="manual"):
        self.recorder = recorder or _Recorder()
        self.service = rt.RealtimeSarvamSTTService(
            api_key="k", endpointing=endpointing, sample_rate=RATE,
            settings=rt.RealtimeSarvamSTTService.Settings(model="saaras:v3-realtime", language=Language.HI_IN),
            recorder=self.recorder, spoken_text=spoken,
        )
        self.service._sample_rate = RATE
        self.service._utterance_open = True
        self.sent: list[bytes] = []
        self.jsons: list[dict] = []
        self.pushed: list = []
        svc = self.service

        async def _send_chunk(chunk: bytes):
            self.sent.append(chunk)

        async def _send_json(payload: dict) -> bool:
            self.jsons.append(payload)
            return True

        async def _push(self_inner, frame, direction=FrameDirection.DOWNSTREAM):
            # Only what the adapter lets out as a transcript matters here; the
            # processor forwards every other frame (bot/VAD state) unchanged.
            if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)):
                self.pushed.append(frame)

        async def _usage():
            return None

        monkeypatch.setattr(svc, "_is_websocket_open", lambda: True)
        monkeypatch.setattr(svc, "_send_audio_chunk", _send_chunk)
        monkeypatch.setattr(svc, "_send_json", _send_json)
        monkeypatch.setattr(svc, "emit_stt_usage_metrics", _usage)
        monkeypatch.setattr(rt.SarvamRealtimeSTTService, "push_frame", _push)

    async def audio(self, frame: bytes) -> None:
        async for _ in self.service.run_stt(frame):
            pass

    async def frame(self, frame) -> None:
        await self.service.process_frame(frame, FrameDirection.DOWNSTREAM)

    async def partial(self, text: str, language: str = "hi-IN") -> None:
        await self.service._handle_message({"event": "transcript.partial", "text": text, "language": language})

    async def final(self, text: str, language: str = "hi-IN") -> None:
        await self.service._handle_message({"event": "transcript.final", "text": text, "language": language})

    @property
    def finals(self) -> list[str]:
        return [f.text for f in self.pushed if isinstance(f, TranscriptionFrame)]

    @property
    def interims(self) -> list[str]:
        return [f.text for f in self.pushed if isinstance(f, InterimTranscriptionFrame)]

    @property
    def stats(self) -> dict:
        return self.service._guard_stats

    async def cleanup(self):
        await self.service.cleanup()


# ── audio towards the socket ────────────────────────────────────────────────

class TestAudioToTheSocket:
    @pytest.mark.asyncio
    async def test_gate_silence_is_never_sent_and_the_socket_is_pinged(self, clock, monkeypatch):
        h = Harness(monkeypatch)
        try:
            for _ in range(10):
                await h.audio(b"\x00" * FRAME)
            assert h.sent == [] and h.stats["silence_skipped_ms"] == 200.0
            assert h.jsons == []
            clock.t += 8.1
            await h.audio(b"\x00" * FRAME)
            assert h.jsons == [{"event": "ping"}] and h.stats["pings"] == 1
            clock.t += 1.0
            await h.audio(b"\x00" * FRAME)
            assert len(h.jsons) == 1                               # not before the next 8 s
        finally:
            await h.cleanup()

    @pytest.mark.asyncio
    async def test_bot_speech_audio_goes_out_only_with_vad_and_keeps_the_onset(self, clock, monkeypatch):
        h = Harness(monkeypatch)
        try:
            await h.frame(BotStartedSpeakingFrame())
            frames = [_frame(i) for i in range(30)]                # 600 ms of the bot's echo
            for f in frames:
                await h.audio(f)
            assert h.sent == [] and h.stats["guarded_skipped_ms"] == 600.0
            assert len(h.service._pre_vad_ring) == 20 * FRAME       # the newest 400 ms
            await h.frame(VADUserStartedSpeakingFrame())            # the caller really starts
            onset = _frame(99)
            await h.audio(onset)
            expected = b"".join(frames[10:]) + onset               # ring first, then the frame
            assert b"".join(h.sent) == expected[: len(b"".join(h.sent))]
            assert len(b"".join(h.sent)) >= len(expected) - RATE * 2 // 10   # upstream's 100 ms chunking
            assert h.service._pre_vad_ring == bytearray()
        finally:
            await h.cleanup()

    @pytest.mark.asyncio
    async def test_echo_tail_after_the_bot_stops(self, clock, monkeypatch):
        h = Harness(monkeypatch)
        try:
            await h.frame(BotStartedSpeakingFrame())
            await h.frame(BotStoppedSpeakingFrame())
            clock.t += 0.3
            await h.audio(_frame(1))
            assert h.sent == []                                      # still the bot's echo
            clock.t += 0.4
            await h.audio(_frame(2))
            assert b"".join(h.sent).endswith(_frame(2)) or h.service._audio_buffer   # past the tail: sent
        finally:
            await h.cleanup()

    @pytest.mark.asyncio
    async def test_speech_end_is_owed_a_final_and_the_utterance_reopens(self, clock, monkeypatch):
        h = Harness(monkeypatch)
        try:
            await h.frame(VADUserStartedSpeakingFrame())
            await h.audio(_frame(1))
            await h.frame(VADUserStoppedSpeakingFrame())
            assert [j["event"] for j in h.jsons][-2:] == ["speech_end", "speech_start"]
            assert len(h.service._pending_finals) == 1 and h.service._utterance_open
        finally:
            await h.cleanup()


# ── transcripts out of the adapter ──────────────────────────────────────────

class TestTranscriptsNeedVadEvidence:
    @pytest.mark.asyncio
    async def test_server_text_without_vad_never_leaves(self, clock, monkeypatch):
        h = Harness(monkeypatch)
        try:
            await h.partial("However,", "en-IN")
            await h.partial("However, whatever happens, happens.", "en-IN")
            await h.final("However, whatever happens, happens. However, whatever happens,", "kn-IN")
            assert h.pushed == []
            assert h.stats["partials_dropped"] == 2 and h.stats["finals_dropped"] == 1
            [event] = h.recorder.of("stt_realtime_transcript_dropped")
            assert event["reason"] == "no_vad_speech" and event["final"] is True
            assert event["words"] == 7 and event["language"] == "kn-IN"
        finally:
            await h.cleanup()

    @pytest.mark.asyncio
    async def test_the_final_owed_to_our_speech_end_passes_once(self, clock, monkeypatch):
        h = Harness(monkeypatch)
        try:
            await h.frame(VADUserStartedSpeakingFrame())
            await h.partial("तो")
            assert h.interims == ["तो"]
            assert getattr(h.pushed[0], "_echosphere_stability_reference") is False
            await h.frame(VADUserStoppedSpeakingFrame())
            clock.t += 0.2
            await h.final("हाँ बोलिए।")
            assert h.finals == ["हाँ बोलिए।"]
            final = h.pushed[-1]
            assert final.finalized is True and getattr(final, "_echosphere_mid_utterance") is False
            await h.final("whatever", "en-IN")                       # nothing is owed any more
            assert h.finals == ["हाँ बोलिए।"] and h.stats["finals_dropped"] == 1
        finally:
            await h.cleanup()

    @pytest.mark.asyncio
    async def test_an_owed_final_is_forgotten_after_the_wait(self, clock, monkeypatch):
        h = Harness(monkeypatch)
        try:
            await h.frame(VADUserStartedSpeakingFrame())
            await h.frame(VADUserStoppedSpeakingFrame())
            clock.t += rt._PENDING_FINAL_S + 0.5
            await h.final("However, whatever", "en-IN")
            assert h.finals == []
        finally:
            await h.cleanup()

    @pytest.mark.asyncio
    async def test_a_final_while_the_caller_still_speaks_is_mid_utterance(self, clock, monkeypatch):
        h = Harness(monkeypatch)
        try:
            await h.frame(VADUserStartedSpeakingFrame())
            await h.final("हाँ मुझे बोला गया था")
            assert h.finals == ["हाँ मुझे बोला गया था"]
            assert getattr(h.pushed[-1], "_echosphere_mid_utterance") is True
        finally:
            await h.cleanup()


class TestEchoText:
    REPLY = ("आपने जो बताया, उसे मैं एक बार confirm कर लेता हूँ। Onboarding के time आपको इस "
             "deduction के बारे में बताया गया था, और आपको 500 रुपये बताए गए थे")

    @pytest.mark.asyncio
    async def test_the_bots_own_words_in_the_echo_window_are_dropped(self, clock, monkeypatch):
        spoken = SpokenTextReference()
        spoken.add(self.REPLY)
        h = Harness(monkeypatch, spoken=spoken)
        try:
            await h.frame(BotStartedSpeakingFrame())
            await h.frame(VADUserStartedSpeakingFrame())             # echo loud enough for Silero
            await h.partial("आपने जो")
            await h.final("आपने जो बताया उसे मैं")
            assert h.pushed == [] and h.stats["echo_dropped"] == 2
            events = h.recorder.of("stt_realtime_transcript_dropped")
            assert [e["reason"] for e in events] == ["echo_text", "echo_text"]
            assert events[-1]["bot_speaking"] is True and events[-1]["vad_open"] is True
            await h.final("haan boliye")                             # the caller's own words pass
            assert h.finals == ["haan boliye"]
        finally:
            await h.cleanup()

    @pytest.mark.asyncio
    async def test_outside_the_echo_window_the_same_words_pass(self, clock, monkeypatch):
        spoken = SpokenTextReference()
        spoken.add(self.REPLY)
        h = Harness(monkeypatch, spoken=spoken)
        try:
            await h.frame(BotStartedSpeakingFrame())
            await h.frame(BotStoppedSpeakingFrame())
            clock.t += 1.0
            await h.frame(VADUserStartedSpeakingFrame())
            await h.final("आपने जो बताया उसे मैं")
            assert h.finals == ["आपने जो बताया उसे मैं"]
        finally:
            await h.cleanup()


# ── the live call's pattern, end to end through the barge-in word gate ──────

def _barge_in(min_words=2):
    confirmed = []
    strategy = WordConfirmedBargeInStrategy(
        min_words=min_words, vad_fallback_secs=0.0,
        on_confirmed=lambda why, sustained: confirmed.append(why),
    )

    async def _fire():
        pass

    strategy.trigger_user_turn_started = _fire
    return strategy, confirmed


class TestLiveCallPattern:
    GREETING = ("नमस्ते Ravi Kumar जी! मैं Ashutosh, Zepto Support से बोल रहा हूँ। आपने अपने payout से "
                "onboarding fee के deduction के बारे में जो ticket raise किया था")

    @pytest.mark.asyncio
    async def test_silence_text_during_the_greeting_then_haan_boliye(self, clock, monkeypatch):
        spoken = SpokenTextReference()
        spoken.add(self.GREETING)
        h = Harness(monkeypatch, spoken=spoken)
        gate, confirmed = _barge_in()
        try:
            await h.frame(BotStartedSpeakingFrame())
            await gate.process_frame(BotStartedSpeakingFrame())
            for t, text in [(4.1, "However"), (8.2, "However, everyone should try"),
                            (12.3, "However, everyone should try to learn.")]:
                clock.t = 1000.0 + t
                await h.partial(text, "en-IN")
                for f in h.pushed:
                    await gate.process_frame(f)
            clock.t = 1018.3
            await h.final("However, everyone should try to learn. It shouldn't matter.", "en-IN")
            for f in h.pushed:
                await gate.process_frame(f)
            assert h.pushed == [] and confirmed == []                # the greeting plays to the end
            await h.frame(BotStoppedSpeakingFrame())
            await gate.process_frame(BotStoppedSpeakingFrame())
            clock.t += 1.7
            await h.frame(VADUserStartedSpeakingFrame())             # the caller: "haan boliye"
            await h.partial("तो")
            clock.t += 0.6
            await h.frame(VADUserStoppedSpeakingFrame())
            clock.t += 0.25
            await h.final("हाँ बोलिए।")
            assert h.finals == ["हाँ बोलिए।"] and h.interims == ["तो"]
            assert confirmed == []
            assert h.stats["partials_dropped"] == 3 and h.stats["finals_dropped"] == 1
        finally:
            await h.cleanup()

    @pytest.mark.asyncio
    async def test_a_silence_final_during_the_long_reply_cannot_cut_it(self, clock, monkeypatch):
        spoken = SpokenTextReference()
        spoken.add(TestEchoText.REPLY)
        h = Harness(monkeypatch, spoken=spoken)
        gate, confirmed = _barge_in()
        try:
            await h.frame(BotStartedSpeakingFrame())
            await gate.process_frame(BotStartedSpeakingFrame())
            clock.t += 16.2
            await h.final("சரிங்க சார்.", "ta-IN")                     # live: cut the reply at 16.23 s
            for f in h.pushed:
                await gate.process_frame(f)
            assert h.pushed == [] and confirmed == []
            [event] = h.recorder.of("stt_realtime_transcript_dropped")
            assert event["reason"] == "no_vad_speech" and event["bot_speaking"] is True
        finally:
            await h.cleanup()


# ── the spoken-text reference ───────────────────────────────────────────────

class TestSpokenTextReference:
    def test_keys_bridge_scripts_and_spelling(self):
        assert normalize_words("हाँ बोलिए।") == ["han", "bolie"]
        assert normalize_words("haan, Boliye!") == ["han", "boliye"]
        assert normalize_words("Onboarding fee — deduction") == ["onboarding", "fe", "deduction"]
        assert normalize_words("।।") == []

    def test_echo_needs_two_words_a_spoken_first_word_and_the_overlap(self):
        ref = SpokenTextReference()
        ref.add("ठीक है — आपके payout से कितना amount deduct हुआ है?")
        assert ref.is_echo("payout se kitna amount") is True
        assert ref.is_echo("ठीक है") is True
        assert ref.is_echo("haan") is False                              # one word
        assert ref.is_echo("haan, payout se kitna amount") is False       # starts with the caller's word
        assert ref.is_echo("mera naam Ravi hai") is False                 # nothing spoken
        assert ref.overlap("payout se kitna amount") == 1.0

    def test_window_keeps_the_newest_words(self):
        ref = SpokenTextReference(window_words=3)
        ref.add("one two three four")
        assert ref.words == ["two", "thre", "four"] and ref.added == 4
        ref.clear()
        assert ref.is_echo("two three") is False
