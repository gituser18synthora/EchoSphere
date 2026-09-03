"""Platform no-response policy and recording-announcement handling in the brain.

The ladder: after ``prompt_seconds`` of quiet the bot asks whether the caller
can hear it, retries ``max_prompts`` times with varied wording, then closes
the call through the normal call-control path (goodbye + EndWorkerFrame).
Accepted caller speech resets the ladder; the bot's own speech never trips
it; rejected noise and recording announcements neither reset it nor keep the
call alive. A recording notice that interrupted the bot restores the
interrupted reply.
"""

import asyncio

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndWorkerFrame,
    InterruptionFrame,
    TextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from voice_runtime.brain import ConversationBrain
from voice_runtime.silence_policy import SilencePolicy

DOWN = FrameDirection.DOWNSTREAM
GRACE = 0.02
POLICY = SilencePolicy(prompt_seconds=0.08, retry_seconds=0.08, max_prompts=2,
                       hold_grace_seconds=0.4)


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-test"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0}
        self.turns = []
        self.language = "hi-IN"
        self.disposition = None

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


def make_brain(policy=POLICY) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(), finalize_grace=GRACE,
        silence_policy=policy,
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


def stub_turn_handler(brain):
    handled = []

    async def _handle(text):
        handled.append(text)

    brain._handle_turn = _handle
    return handled


def transcript(text):
    return TranscriptionFrame(text=text, user_id="u", timestamp="t", language="hi-IN")


def spoken(brain):
    return [f.text for f in brain._pushed if isinstance(f, TextFrame)]


def prompts(brain):
    return [d for k, d in brain._recorder.events if k == "silence_prompt"]


async def bot_speaks_and_stops(brain):
    await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
    await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)


async def wait(seconds):
    await asyncio.sleep(seconds)
    for _ in range(3):
        await asyncio.sleep(0)


class TestSilenceLadder:
    async def test_first_no_response_prompt(self):
        brain = make_brain()
        await bot_speaks_and_stops(brain)          # greeting done → timer armed
        assert brain._silence_task is not None
        await wait(0.12)
        assert [p["attempt"] for p in prompts(brain)] == [1]
        assert any("सुन पा रहे" in t for t in spoken(brain))
        assert brain._closing is False

    async def test_retry_counter_and_varied_wording(self):
        brain = make_brain()
        await bot_speaks_and_stops(brain)
        await wait(0.12)                            # prompt 1
        await bot_speaks_and_stops(brain)           # prompt 1 audio played
        await wait(0.12)                            # prompt 2
        assert [p["attempt"] for p in prompts(brain)] == [1, 2]
        texts = spoken(brain)
        assert len(texts) == 2 and texts[0] != texts[1]

    async def test_final_retry_ends_the_call(self):
        brain = make_brain()
        await bot_speaks_and_stops(brain)
        await wait(0.12)
        await bot_speaks_and_stops(brain)
        await wait(0.12)
        await bot_speaks_and_stops(brain)
        await wait(0.12)                            # third expiry → close
        assert brain._closing is True
        assert any("बाद में connect" in t for t in spoken(brain))
        ends = [f for f in brain._pushed if isinstance(f, EndWorkerFrame)]
        assert len(ends) == 1 and ends[0].reason == "no_response"
        control = [d for k, d in brain._recorder.events if k == "call_control"]
        assert control and control[-1]["reason"] == "no_response"
        assert brain._recorder.disposition == "no_response"
        assert brain._silence_task is None
        # Nothing further fires after the close.
        await wait(0.12)
        assert len(prompts(brain)) == 2

    async def test_caller_response_resets_timer_and_counter(self):
        brain = make_brain()
        handled = stub_turn_handler(brain)
        await bot_speaks_and_stops(brain)
        await wait(0.12)
        assert len(prompts(brain)) == 1
        await bot_speaks_and_stops(brain)           # prompt audio → re-armed
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        assert brain._silence_task is None          # paused while caller speaks
        await brain.process_frame(transcript("हाँ बोल रहा हूँ"), DOWN)
        assert brain._silence_prompts == 0          # meaningful input resets
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        await wait(GRACE * 3)
        assert handled == ["हाँ बोल रहा हूँ"]
        # The bot answers; after its reply the ladder starts from the top.
        await bot_speaks_and_stops(brain)
        await wait(0.12)
        assert [p["attempt"] for p in prompts(brain)] == [1, 1]

    async def test_bot_speech_does_not_trigger_the_prompt(self):
        brain = make_brain()
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await wait(0.2)                             # longer than prompt_seconds
        assert prompts(brain) == []
        assert brain._silence_task is None
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        assert brain._silence_task is not None      # measured from the stop

    async def test_ignored_announcement_does_not_reset_the_ladder(self):
        brain = make_brain()
        handled = stub_turn_handler(brain)
        await bot_speaks_and_stops(brain)
        await wait(0.12)                            # prompt 1
        await bot_speaks_and_stops(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        await brain.process_frame(transcript("Call is now being recorded."), DOWN)
        await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
        assert "recording_announcement_ignored" in brain._recorder.event_kinds()
        assert brain._silence_prompts == 1          # not meaningful input
        assert brain._silence_task is not None      # re-armed, not reset
        await wait(0.12)
        assert [p["attempt"] for p in prompts(brain)] == [1, 2]
        await wait(GRACE * 3)
        assert handled == []                        # never became a turn

    async def test_hold_request_buys_extra_quiet(self):
        brain = make_brain()
        brain._hold_requested_at = asyncio.get_event_loop().time()
        import time
        brain._hold_requested_at = time.monotonic()
        await bot_speaks_and_stops(brain)
        await wait(0.2)                             # past prompt_seconds
        assert prompts(brain) == []                 # still inside the hold grace
        await wait(0.35)
        assert len(prompts(brain)) == 1


class TestRecordingAnnouncementInBrain:
    async def test_announcement_that_cut_the_bot_restores_the_reply(self):
        brain = make_brain()
        handled = stub_turn_handler(brain)
        brain._last_bot_reply = "नमस्ते! मैं Shubh, Zepto support से बोल रहा हूँ।"
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        # The notice's audio confirmed a barge-in and cut the greeting.
        await brain.process_frame(InterruptionFrame(), DOWN)
        assert "barge_in" in brain._recorder.event_kinds()
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(transcript("Call is now being recorded."), DOWN)
        await wait(GRACE * 3)
        assert "recording_announcement_ignored" in brain._recorder.event_kinds()
        assert "bot_reply_resumed_after_announcement" in brain._recorder.event_kinds()
        assert spoken(brain) == [brain._last_bot_reply]   # greeting re-spoken
        assert handled == []                                # no LLM/workflow turn
        assert brain._recorder.turns == []                  # no transcript entry

    async def test_announcement_fused_with_speech_keeps_only_the_speech(self):
        brain = make_brain()
        handled = stub_turn_handler(brain)
        await brain.process_frame(transcript("Call is now being recorded. बताइए।"), DOWN)
        await wait(GRACE * 6)
        assert handled == ["बताइए"]
        assert "recording_announcement_stripped" in brain._recorder.event_kinds()

    async def test_real_speech_after_interruption_is_not_resumed_over(self):
        brain = make_brain()
        handled = stub_turn_handler(brain)
        brain._last_bot_reply = "greeting"
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(InterruptionFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(transcript("ek minute ruko"), DOWN)
        await wait(GRACE * 6)
        assert handled == ["ek minute ruko"]
        assert "bot_reply_resumed_after_announcement" not in brain._recorder.event_kinds()
