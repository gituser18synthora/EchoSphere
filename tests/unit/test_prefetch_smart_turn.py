"""Phase 5 Change 2 (revised): the decision prefetch on Smart Turn calls.

A confident Smart Turn verdict dispatches the final within ~15 ms of its
arrival (demo 2026-10-27..30: verdict +105 ms, final +145 ms, dispatch
+161 ms after the VAD stop), so speculating at the verdict buys nothing.
The prefetch pays only while a settled final is HELD — Smart Turn said
incomplete and the brain's own endpoint or the pause window runs (head start
p90 600 ms), or the STT final beat the VAD stop and the verdict is still
computing. Text that is still growing is never speculated on: that was the
~40 discarded decision calls a day of Phase 4.
"""

import asyncio
import time

from pipecat.frames.frames import VADUserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from voice_runtime.brain import ConversationBrain

from tests.unit.test_agentic_orchestration import GRACE, _AgenticLLMStub, _RecorderStub

TEXT = "mujhe invoice samajh nahi aaya"


def _brain(*, smart_turn_active: bool) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Support", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Support.",
        intents=[{"name": "support", "samples": ["help"], "route": ""}],
    )
    brain = ConversationBrain(
        config=config, llm=_AgenticLLMStub([]), recorder=_RecorderStub(),
        finalize_grace=GRACE, complete_endpoint=GRACE, smart_turn_active=smart_turn_active,
    )
    brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)

    async def _push(frame, direction=None):
        pass

    brain.push_frame = _push
    brain._pending_segments = [TEXT]
    assert brain._goal_engine.enabled
    return brain


def _events(brain, kind):
    return [data for k, data in brain._recorder.events if k == kind]


def _final_in(brain) -> None:
    """This speech episode's final has landed and the VAD has stopped."""
    brain._physical_speech_active = False
    brain._last_vad_stopped_at = None
    brain._last_final_at = time.monotonic()


async def test_no_prefetch_while_the_caller_is_still_speaking():
    brain = _brain(smart_turn_active=True)
    brain._physical_speech_active = True          # mid-utterance segment buffered
    brain._last_final_at = time.monotonic()
    brain._start_decision_prefetch()
    assert brain._decision_prefetch is None
    assert _events(brain, "decision_prefetch_skipped") == [{"reason": "speech_active"}]


async def test_no_prefetch_before_this_episodes_final():
    brain = _brain(smart_turn_active=True)
    # Smart Turn closed the turn on older buffered text; the final of the
    # speech just heard is still in flight (_SMART_TURN_FINAL_WAIT_S path).
    brain._physical_speech_active = False
    brain._last_final_at = time.monotonic() - 2.0
    brain._last_vad_stopped_at = time.monotonic()
    brain._last_vad_stop_secs = 0.2
    brain._start_decision_prefetch()
    assert brain._decision_prefetch is None
    assert _events(brain, "decision_prefetch_skipped") == [{"reason": "final_pending"}]


async def test_a_held_settled_final_is_prefetched_and_kept():
    brain = _brain(smart_turn_active=True)
    _final_in(brain)
    brain._start_decision_prefetch()
    assert brain._decision_prefetch is not None
    assert brain._decision_prefetch[0] == TEXT
    task = brain._decision_prefetch[1]
    brain._start_decision_prefetch()              # endpoint re-armed: same text, same task
    assert brain._decision_prefetch[1] is task
    assert _events(brain, "decision_prefetch_skipped") == []
    brain._discard_decision_prefetch("test")


async def test_the_vad_stop_starts_the_prefetch_when_the_final_beat_it():
    brain = _brain(smart_turn_active=True)
    brain._turn_active = True
    brain._physical_speech_active = True
    brain._last_final_at = time.monotonic()        # final inside the stop window
    await brain.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.5), FrameDirection.DOWNSTREAM)
    assert brain._physical_speech_active is False
    assert brain._decision_prefetch is not None and brain._decision_prefetch[0] == TEXT
    brain._discard_decision_prefetch("test")


async def test_the_vad_stop_without_a_final_or_an_open_turn_starts_nothing():
    brain = _brain(smart_turn_active=True)
    brain._turn_active = True
    brain._physical_speech_active = True
    brain._last_final_at = None
    await brain.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert brain._decision_prefetch is None
    brain._turn_active = False                    # a held backchannel while the bot speaks
    brain._last_final_at = time.monotonic()
    await brain.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert brain._decision_prefetch is None


async def test_speech_resumed_inside_the_turn_discards_the_prefetch():
    brain = _brain(smart_turn_active=True)
    _final_in(brain)
    brain._turn_active = True
    brain._start_decision_prefetch()
    assert brain._decision_prefetch is not None
    await brain._on_physical_speech_resumed()
    assert brain._decision_prefetch is None
    assert _events(brain, "decision_prefetch_discarded") == [{"reason": "speech_resumed"}]


async def test_other_calls_keep_the_phase_4_prefetch():
    brain = _brain(smart_turn_active=False)
    brain._physical_speech_active = True          # mid-utterance speculation stays
    brain._last_final_at = None
    brain._start_decision_prefetch()
    assert brain._decision_prefetch is not None
    assert _events(brain, "decision_prefetch_skipped") == []
    brain._turn_active = True
    await brain._on_physical_speech_resumed()     # no eager discard off Smart Turn
    assert brain._decision_prefetch is not None
    brain._discard_decision_prefetch("test")


def test_the_pipeline_derives_the_flag_from_the_smart_turn_gate():
    from voice_runtime.smart_turn import MODE_OFF, MODE_ON, smart_turn_decision

    assert smart_turn_decision(MODE_ON, "hi-IN", enabled=True) == (True, "tenant_on")
    assert smart_turn_decision(MODE_OFF, "hi-IN", enabled=True)[0] is False
    assert smart_turn_decision(MODE_ON, "hi-IN", enabled=False)[0] is False
