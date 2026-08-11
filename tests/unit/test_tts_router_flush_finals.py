"""StreamingTTSRouter mid-turn flush finals — unit level, no pipeline.

Regression for the flush-hint bug: a TTSFlushHintFrame flush made Sarvam emit
a completion "final" while the LLM was still streaming the same turn; the
router finalized the generation (TTSStoppedFrame, audio context removed) and
every subsequent audio chunk of the reply was rejected. A mid-turn final must
be ignored; only the end-of-turn final (or a failure) closes the generation.
"""

import asyncio

from pipecat.frames.frames import TTSAudioRawFrame, TTSStoppedFrame
from pipecat.processors.frame_processor import FrameDirection

from shared.providers.tts.streaming import (
    StreamingTTSProvider,
    TTSStreamEvent,
    TTSStreamSettings,
)
from voice_runtime.frames import SwitchVoiceLanguageFrame
from voice_runtime.tts_router import StreamingTTSRouter, _Generation, _Sentence

ENGINE = {"provider": "sarvam", "model": "bulbul:v3", "voice": "shubh"}
KEY = ("sarvam", "bulbul:v3", "shubh")


class FakeProvider(StreamingTTSProvider):
    name = "fake-tts"

    def __init__(self):
        super().__init__(TTSStreamSettings(
            provider="sarvam", model="bulbul:v3", voice="shubh",
            language="hi-IN", sample_rate=16000,
        ))
        self.calls: list[tuple] = []

    async def connect(self):
        self.calls.append(("connect",))

    async def configure(self, settings):
        self.calls.append(("configure",))

    async def synthesize_stream(self, text, *, generation_id):
        self._begin_generation(generation_id)
        self.calls.append(("text", generation_id, text))

    async def flush(self, generation_id):
        self.calls.append(("flush", generation_id))

    async def finish(self, generation_id):
        self.calls.append(("finish", generation_id))

    async def cancel(self, generation_id):
        self.calls.append(("cancel", generation_id))

    async def close(self):
        self.calls.append(("close",))


class FakeRecorder:
    session_id = "vs_unit"

    def __init__(self):
        self.events: list[dict] = []
        self.persisted: list[str] = []

    def add_event(self, kind, **data):
        self.events.append({"kind": kind, **data})

    def add_tts_usage(self, **kwargs):
        pass

    async def flush_event(self, kind, **data):
        self.add_event(kind, **data)
        self.persisted.append(kind)


def make_router(**kwargs):
    router = StreamingTTSRouter(
        tts_config={"provider": "sarvam", "model": "bulbul:v3", "voice": "shubh",
                    "settings": {}, "api_key_reference": "",
                    "language_map": {}, "fallback": None},
        language="hi-IN", sample_rate=16000, **kwargs,
    )
    router._sample_rate = 16000       # normally set by StartFrame
    router._task_manager = object()   # routes create_task to the stub below
    router.appended = []
    router.removed = []
    router.tasks = []

    async def append(context_id, frame):
        router.appended.append((context_id, frame))

    async def remove(context_id):
        router.removed.append(context_id)

    async def noop(*args, **kwargs):
        pass

    def create_task(coro, *args, **kwargs):
        task = asyncio.get_running_loop().create_task(coro)
        router.tasks.append(task)
        return task

    router.append_to_audio_context = append
    router.remove_audio_context = remove
    router.audio_context_available = lambda context_id: context_id not in router.removed
    router.stop_ttfb_metrics = noop
    router.create_task = create_task
    return router


def stopped_frames(router):
    return [f for _, f in router.appended if isinstance(f, TTSStoppedFrame)]


def audio_frames(router):
    return [f for _, f in router.appended if isinstance(f, TTSAudioRawFrame)]


async def test_midturn_flush_final_does_not_finalize_generation():
    router = make_router()
    state = _Generation(engine=ENGINE, provider=FakeProvider())
    router._generations["ctx1"] = state

    await router._dispatch_event(KEY, TTSStreamEvent(
        kind="audio", generation_id="ctx1", audio=b"\x01\x02" * 8,
    ))
    # Flush-hint-induced provider final while the LLM is still streaming.
    await router._dispatch_event(KEY, TTSStreamEvent(kind="final", generation_id="ctx1"))
    assert "ctx1" in router._generations          # generation survives
    assert router.removed == []                    # audio context stays open
    assert stopped_frames(router) == []
    assert state.midturn_final_seen is True

    # Synthesis continues on the same generation: audio is still accepted.
    await router._dispatch_event(KEY, TTSStreamEvent(
        kind="audio", generation_id="ctx1", audio=b"\x01\x02" * 8,
    ))
    assert len(audio_frames(router)) == 2

    # End of turn: the completion marker is set, the next final closes out.
    state.turn_complete = True
    await router._dispatch_event(KEY, TTSStreamEvent(kind="final", generation_id="ctx1"))
    assert "ctx1" not in router._generations
    assert router.removed == ["ctx1"]
    assert len(stopped_frames(router)) == 1


async def test_completion_marker_set_before_end_of_turn_flush():
    """The end-of-turn flush (run by super().on_turn_context_completed) must
    always find turn_complete already set — otherwise the final it produces
    would race the marker and could be dropped as a mid-turn final."""
    router = make_router()
    provider = FakeProvider()
    state = _Generation(engine=ENGINE, provider=provider)
    state.got_audio = True
    router._generations["ctx2"] = state
    router._turn_context_id = "ctx2"

    marker_at_flush = []
    original_flush = provider.flush

    async def observing_flush(generation_id):
        marker_at_flush.append(state.turn_complete)
        await original_flush(generation_id)

    provider.flush = observing_flush
    await router.on_turn_context_completed()
    assert marker_at_flush == [True]


async def test_ignored_final_with_no_more_text_closes_at_end_of_turn():
    """A flush-hint final followed by NO further text: the provider owes no
    second final, so end-of-turn must close the generation directly instead
    of waiting forever."""
    router = make_router()
    state = _Generation(engine=ENGINE, provider=FakeProvider())
    state.got_audio = True
    router._generations["ctx3"] = state

    await router._dispatch_event(KEY, TTSStreamEvent(kind="final", generation_id="ctx3"))
    assert "ctx3" in router._generations

    router._turn_context_id = "ctx3"
    await router.on_turn_context_completed()
    assert "ctx3" not in router._generations
    assert router.removed == ["ctx3"]
    assert len(stopped_frames(router)) == 1


async def test_pause_mode_sentence_finals_still_drive_serialization():
    """Pause mode relies on per-sentence finals for sub-generations — they
    must keep releasing the next sentence mid-turn and must NOT be gated."""
    router = make_router(pause_ms=200)
    provider = FakeProvider()
    state = _Generation(engine=ENGINE, provider=provider)
    router._generations["ctx4"] = state
    state.pending.append(_Sentence(text="One."))
    state.pending.append(_Sentence(text="Two."))

    await router._dispatch_next_sentence("ctx4", state)
    assert state.active == "ctx4~1"

    # Mid-turn per-sentence final releases the next sentence — no close-out.
    await router._dispatch_event(KEY, TTSStreamEvent(kind="final", generation_id="ctx4~1"))
    assert state.active == "ctx4~2"
    assert "ctx4" in router._generations

    state.turn_complete = True
    await router._dispatch_event(KEY, TTSStreamEvent(kind="final", generation_id="ctx4~2"))
    assert "ctx4" not in router._generations
    assert len(stopped_frames(router)) == 1


async def test_finalize_backgrounds_the_recorder_write():
    """The tts_provider_used persistence runs off the event pump — finalize
    schedules it as a task instead of awaiting the Mongo round-trip."""
    recorder = FakeRecorder()
    router = make_router(recorder=recorder)
    state = _Generation(engine=ENGINE, provider=FakeProvider())
    router._generations["ctx5"] = state

    await router._finalize_generation("ctx5", failed=False)
    assert len(router.tasks) == 1
    await asyncio.gather(*router.tasks)
    assert recorder.persisted == ["tts_provider_used"]


async def test_language_switch_prewarms_unconnected_engine():
    router = make_router()
    router._language_map = {
        "en-US": {"provider": "elevenlabs", "model": "eleven_flash_v2_5",
                  "voice": "v1", "params": {}, "api_key_reference": "env:X"},
    }
    warmed = []
    provider = FakeProvider()

    async def fake_get_provider(engine, locale):
        warmed.append((engine.get("provider"), locale))
        return provider

    router._get_provider = fake_get_provider
    await router.process_frame(
        SwitchVoiceLanguageFrame(language="en-US"), FrameDirection.DOWNSTREAM
    )
    await asyncio.gather(*router.tasks)
    assert warmed == [("elevenlabs", "en-US")]
    assert ("connect",) in provider.calls
