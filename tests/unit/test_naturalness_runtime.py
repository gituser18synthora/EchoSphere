"""Human speech naturalness — runtime wiring.

Covers the three integration seams:
- ConversationBrain: transient prefaces mask LLM/tool latency, never enter
  conversation history; backchannels never count as the bot's reply.
- StreamingTTSRouter: per-sentence pause/rate variation in pause mode.
- CallerAudioGate: the backchannel window keeps a mid-utterance murmur from
  poisoning the caller's own live speech segment.
"""

import asyncio
import random

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.naturalness import SpeechNaturalnessPlanner, TurnSpeechPlan
from shared.providers.tts.streaming import TTSStreamEvent, TTSStreamSettings
from voice_runtime.audio_gate import CallerAudioGate
from voice_runtime.brain import ConversationBrain
from voice_runtime.frames import TTSFlushHintFrame
from voice_runtime.tts_router import _Generation, _Sentence

from tests.unit.test_tts_router_flush_finals import (
    ENGINE,
    KEY,
    FakeProvider,
    audio_frames,
    make_router,
)

GRACE = 0.05


def planner(overrides=None, seed=7):
    base = {"enabled": True}
    base.update(overrides or {})
    return SpeechNaturalnessPlanner(base, rng=random.Random(seed))


# ── brain harness (mirrors test_brain_call_session) ─────────────────────


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-nat"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0}
        self.turns = []
        self.language = "hi-IN"

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        self.turns.append(turn)

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))

    def flush_event_soon(self, kind, **data):
        self.events.append((kind, data))

    def event_kinds(self):
        return [kind for kind, _ in self.events]


class _StreamingLLMStub:
    def __init__(self, tokens):
        self._tokens = tokens
        self.calls = []
        self.last_stream_usage = None

    def stream(self, history, *, system, temperature, max_tokens):
        self.calls.append({"history": [dict(m) for m in history], "system": system})

        async def _gen():
            for token in self._tokens:
                yield token

        return _gen()


def make_brain(*, llm=None, naturalness=None, tts=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, tts=tts or {}, system_prompt="You are Test.",
    )
    brain = ConversationBrain(
        config=config, llm=llm, recorder=_RecorderStub(),
        finalize_grace=GRACE, naturalness=naturalness,
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify

    def _create_task(coro, name=None):
        return asyncio.get_event_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.create_task = _create_task
    brain.cancel_task = _cancel_task
    return brain


def transcript(text, language="hi-IN"):
    return TranscriptionFrame(text=text, user_id="u", timestamp="t", language=language)


async def settle_turn():
    await asyncio.sleep(GRACE * 2)
    for _ in range(5):
        await asyncio.sleep(0)


def assistant_history(brain):
    return [m["content"] for m in brain._history if m["role"] == "assistant"]


# ── brain: streamed-reply preface ────────────────────────────────────────


async def test_streamed_reply_preface_is_spoken_but_not_history():
    llm = _StreamingLLMStub(["Aapka sawaal ", "samajh gaya, batata hoon."])
    brain = make_brain(
        llm=llm,
        naturalness=planner({"thinking_filler_probability": 1.0,
                             "acknowledgement_probability": 1.0}),
    )
    await brain.process_frame(
        transcript("mujhe apne account ke baare mein jaanna hai"),
        FrameDirection.DOWNSTREAM,
    )
    await settle_turn()

    texts = [f.text for f in brain._pushed if isinstance(f, TextFrame)]
    # First spoken text is the delivery preface, flushed immediately.
    assert texts[0].endswith("... ") or texts[0].endswith("… "), texts
    assert any(isinstance(f, TTSFlushHintFrame) for f in brain._pushed)
    # The semantic reply follows and is what history keeps — no preface.
    reply = assistant_history(brain)[-1]
    assert reply == "Aapka sawaal samajh gaya, batata hoon."
    assert not reply.startswith(texts[0].strip())
    # Telemetry rode the orchestration_turn event.
    event = dict(brain._recorder.events)["orchestration_turn"]
    assert event["human_speech_enabled"] is True
    assert event["naturalness"]["filler_used"] is True
    assert event["naturalness"]["preface_spoken"] is True


async def test_disabled_naturalness_changes_nothing():
    llm = _StreamingLLMStub(["Theek hai."])
    brain = make_brain(llm=llm, naturalness=planner({"enabled": False}))
    await brain.process_frame(
        transcript("mujhe jaankari chahiye"), FrameDirection.DOWNSTREAM
    )
    await settle_turn()
    texts = [f.text for f in brain._pushed if isinstance(f, TextFrame)]
    assert texts == ["Theek hai."]
    event = dict(brain._recorder.events)["orchestration_turn"]
    assert event["human_speech_enabled"] is False
    assert event["naturalness"]["filler_used"] is False


async def test_direct_say_preface_keeps_history_semantic():
    brain = make_brain(llm=_StreamingLLMStub([]), naturalness=planner())
    brain._turn_speech_plan = TurnSpeechPlan(
        preface="Achha...", preface_kind="acknowledgement"
    )
    record = await brain._say("Aapki request note kar li hai.", authored=False)
    assert record is not None and record.text == "Aapki request note kar li hai."
    assert assistant_history(brain) == ["Aapki request note kar li hai."]
    texts = [f.text for f in brain._pushed if isinstance(f, TextFrame)]
    assert texts == ["Achha... ", "Aapki request note kar li hai."]


async def test_authored_say_is_never_decorated():
    brain = make_brain(llm=_StreamingLLMStub([]), naturalness=planner())
    brain._turn_speech_plan = TurnSpeechPlan(
        preface="Achha...", preface_kind="acknowledgement"
    )
    await brain._say("Namaste, main Test bol raha hoon.")  # authored=True
    texts = [f.text for f in brain._pushed if isinstance(f, TextFrame)]
    assert texts == ["Namaste, main Test bol raha hoon."]
    # The plan's preface was not consumed by the authored phrase.
    assert brain._turn_speech_plan.has_preface


# ── brain: backchannels ──────────────────────────────────────────────────


async def test_backchannel_never_becomes_a_turn_or_reply():
    brain = make_brain(llm=_StreamingLLMStub([]), naturalness=planner())
    await brain._play_backchannel("hmm...")

    # Spoken, but no history / turn record / client bot_text. Trailing "..."
    # is normalized to a single ellipsis so the aggregator cannot split it.
    texts = [f.text for f in brain._pushed if isinstance(f, TextFrame)]
    assert texts == ["hmm…"]
    assert brain._history == []
    assert brain._recorder.turns == []
    assert all(p.get("type") != "bot_text" for p in brain._notified)
    assert "backchannel_played" in brain._recorder.event_kinds()
    assert brain._backchannel_active is True

    # Its audio must not count as the reply speaking.
    await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    assert brain._reply_audio_started is False
    assert brain._latency.bot_started_at is None
    assert brain._bot_speaking is True

    await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
    assert brain._backchannel_active is False
    assert brain._bot_speaking is False


async def test_reply_audio_bookkeeping_intact_without_backchannel():
    brain = make_brain(llm=_StreamingLLMStub([]), naturalness=planner())
    # A dispatched turn is in flight (the tracker rejects unowned bot audio).
    brain._latency.mark_final()
    brain._latency.mark_dispatched()
    await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    assert brain._reply_audio_started is True
    assert brain._latency.bot_started_at is not None


async def test_monitor_not_started_when_backchannels_disabled():
    brain = make_brain(
        llm=_StreamingLLMStub([]),
        naturalness=planner({"backchannels": False}),
    )
    brain._pipeline_started = True
    brain._start_backchannel_monitor()
    assert brain._backchannel_task is None


# ── TTS router: per-sentence delivery ────────────────────────────────────


class _ForcedPlanner(SpeechNaturalnessPlanner):
    """Deterministic segment plans for router assertions."""

    def __init__(self, pause_after_ms=320, speed_scale=0.95):
        super().__init__({}, rng=random.Random(1))
        self._forced = (pause_after_ms, speed_scale)

    def plan_segment(self, text, *, base_pause_ms, language=""):
        seg = super().plan_segment(text, base_pause_ms=base_pause_ms,
                                   language=language)
        seg.pause_after_ms, seg.speed_scale = self._forced
        return seg


async def test_pause_mode_uses_planned_per_sentence_gap():
    router = make_router(pause_ms=150, naturalness=_ForcedPlanner(pause_after_ms=320))
    provider = FakeProvider()
    state = _Generation(engine=ENGINE, provider=provider)
    router._generations["ctx"] = state

    async for _ in router.run_tts("Pehla vaakya.", "ctx"):
        pass
    assert state.active == "ctx~1"
    assert state.active_pause_after_ms == 320

    # Sentence one produced audio and finished → the gap is owed at 320ms.
    await router._dispatch_event(KEY, TTSStreamEvent(
        kind="audio", generation_id="ctx~1", audio=b"\x01\x02" * 8,
    ))
    async for _ in router.run_tts("Doosra vaakya.", "ctx"):
        pass
    await router._dispatch_event(KEY, TTSStreamEvent(kind="final", generation_id="ctx~1"))
    assert state.active == "ctx~2"

    silence = audio_frames(router)[-1]
    expected_bytes = int(16000 * 320 / 1000) * 2
    assert len(silence.audio) == expected_bytes


async def test_pause_mode_without_planner_keeps_router_default_gap():
    router = make_router(pause_ms=150)
    provider = FakeProvider()
    state = _Generation(engine=ENGINE, provider=provider)
    router._generations["ctx"] = state

    async for _ in router.run_tts("Pehla vaakya.", "ctx"):
        pass
    await router._dispatch_event(KEY, TTSStreamEvent(
        kind="audio", generation_id="ctx~1", audio=b"\x01\x02" * 8,
    ))
    async for _ in router.run_tts("Doosra vaakya.", "ctx"):
        pass
    await router._dispatch_event(KEY, TTSStreamEvent(kind="final", generation_id="ctx~1"))

    silence = audio_frames(router)[-1]
    assert len(silence.audio) == int(16000 * 150 / 1000) * 2


async def test_per_sentence_speed_reconfigures_elevenlabs_only():
    engine = {"provider": "elevenlabs", "model": "eleven_flash_v2_5",
              "voice": "v1", "params": {}, "api_key_reference": "env:X"}
    router = make_router(pause_ms=150, naturalness=_ForcedPlanner(speed_scale=0.95))
    provider = FakeProvider()
    state = _Generation(engine=engine, provider=provider)
    router._generations["ctx"] = state

    seen = []

    def fake_settings(eng, locale, *, speed=None):
        seen.append(speed)
        return TTSStreamSettings(provider="elevenlabs", model="eleven_flash_v2_5",
                                 voice="v1", language=locale, sample_rate=16000)

    router._stream_settings = fake_settings
    state.pending.append(_Sentence(text="Ek.", speed_scale=0.95))
    await router._dispatch_next_sentence("ctx", state)
    assert seen == [1.0 * 0.95]
    assert ("configure",) in provider.calls

    # Sarvam engines are never reconfigured mid-turn (config resend flushes).
    provider2 = FakeProvider()
    state2 = _Generation(engine=ENGINE, provider=provider2)
    router._generations["ctx2"] = state2
    state2.pending.append(_Sentence(text="Do.", speed_scale=0.95))
    await router._dispatch_next_sentence("ctx2", state2)
    assert ("configure",) not in provider2.calls


# ── caller audio gate: backchannel window ────────────────────────────────


def open_gate() -> CallerAudioGate:
    gate = CallerAudioGate()
    gate._open = True
    gate._floor_dbfs = -60.0
    return gate


def test_backchannel_window_shields_the_open_segment():
    gate = open_gate()
    gate._begin_segment()
    gate._bot_speaking = True          # backchannel audio is playing
    gate.begin_backchannel_window()

    assert gate._echo_guarded() is False            # threshold not raised
    gate._accumulate_segment(-30.0, 100.0)
    assert gate._segment_during_bot_audio is False  # segment not poisoned
    assert gate.live_speech_ms == 100.0

    # The shield outlives the window by the echo tail, so the caller's
    # continuing speech is not latched by trailing echo either.
    gate._bot_speaking = False
    gate.end_backchannel_window()
    gate._bot_stopped_at = __import__("time").monotonic()
    gate._accumulate_segment(-30.0, 100.0)
    assert gate._segment_during_bot_audio is False


def test_closed_gate_keeps_full_echo_protection_during_backchannel():
    gate = CallerAudioGate()
    gate._bot_speaking = True
    gate.begin_backchannel_window()
    # Gate closed: the backchannel's own echo must NOT find a lowered bar.
    assert gate._echo_guarded() is True
    assert gate.live_speech_ms == 0.0


def test_normal_echo_guard_unaffected_outside_window():
    gate = open_gate()
    gate._begin_segment()
    gate._bot_speaking = True
    assert gate._echo_guarded() is True
    gate._accumulate_segment(-30.0, 50.0)
    assert gate._segment_during_bot_audio is True
