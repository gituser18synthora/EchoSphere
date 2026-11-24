"""Pickup hello preserves the opening without accepting identity or consent."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, InterruptionFrame,
    TextFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from tests.unit.test_brain_turn_taking import make_brain, settle_turn, transcript
from tests.unit.test_workflow_closing import cleanup
from tests.unit.test_workflow_identity_coherence import (
    engine, brain_with_engine, QUESTION as AUTHORED_IDENTITY, CONTACT,
)
from tests.unit.test_brain_collection_policy import turn
from tests.unit.test_brain_workflow_routing import _LLMStub
from shared.orchestration.decision_schema import ConversationDecision
from voice_runtime.frames import (
    GreetingPlaybackFrame, GreetingPlayedFrame,
    ReplyMarkerFrame, ReplyTTSCompleteFrame, ReplyTTSInterruptedFrame,
    SwitchVoiceLanguageFrame,
)

DOWN = FrameDirection.DOWNSTREAM
INTRO = "Namaste! Main Haier Customer Care se Riya bol rahi hoon."
QUESTION = "Kya meri baat Vaibhav ji se ho rahi hai?"
GREETING = f"{INTRO} {QUESTION}"


@pytest.fixture
async def brain():
    instance = make_brain()
    instance._config.greeting = GREETING
    instance._take_decision = AsyncMock(return_value=None)
    yield instance
    await cleanup(instance)


def spoken(brain):
    return [frame.text for frame in brain._pushed if isinstance(frame, TextFrame)]


async def greet(brain):
    await brain._open_session()
    return brain._opening_greeting["marker"]


async def cut(brain, *, played=0.8, before_audio=False):
    if not before_audio:
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        brain._reply_audio_started_at = time.monotonic() - played
    await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
    await brain.process_frame(InterruptionFrame(), DOWN)
    await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)


async def hello_turn(brain, text="hello"):
    previous_generation = brain._generation
    final = transcript(text)
    final.timestamp = str(time.monotonic())
    await brain.process_frame(final, DOWN)
    await brain.process_frame(UserStoppedSpeakingFrame(), DOWN)
    for _ in range(100):
        await asyncio.sleep(.01)
        if not brain._pending_segments and not brain._finalize_pending():
            break
    if brain._generation is not None and (
            brain._generation is not previous_generation or brain._generation.done()):
        await brain._generation


@pytest.mark.parametrize("text", ["hello", "Hello!", "हेलो।", "हैलो?", "hello ji", "जी हेलो", "hello hello"])
@pytest.mark.parametrize("before_audio", [False, True])
async def test_pickup_collision_recovers_rendered_intro_once_without_model_or_identity(brain, text, before_audio):
    await greet(brain)
    await cut(brain, before_audio=before_audio)
    await hello_turn(brain, text)
    assert spoken(brain) == [GREETING, GREETING]
    assert not brain._identity.confirmed and not brain._identity.denied
    assert brain._active_workflow is None and not brain._workflow_ever_routed
    assert brain._heard_nodes == [] and brain._reply_nodes == []
    brain._take_decision.assert_not_called()
    users = [turn for turn in brain._recorder.turns if turn.role == "user"]
    assert [turn.text for turn in users] == [text]
    assert users[0].route == "pickup_greeting"
    assert brain._opening_greeting["recovered"]


async def test_greeting_marker_and_fence_survive_turn_tracking_reset(brain):
    marker = await greet(brain)
    assert [frame.marker for frame in brain._pushed if isinstance(frame, ReplyMarkerFrame)] == [marker]
    assert [frame.marker for frame in brain._pushed if isinstance(frame, GreetingPlaybackFrame)] == [marker]
    opening = brain._opening_greeting
    brain._begin_reply_tracking()
    assert brain._opening_greeting is opening
    assert brain._heard_nodes == []


@pytest.mark.parametrize("completion_first", [False, True])
async def test_synthesis_complete_and_bot_stop_gaps_do_not_prove_playout(brain, completion_first):
    marker = await greet(brain)
    if completion_first:
        await brain.process_frame(ReplyTTSCompleteFrame(marker=marker), DOWN)
    await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
    await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
    if not completion_first:
        await brain.process_frame(ReplyTTSCompleteFrame(marker=marker), DOWN)
    assert not brain._opening_greeting["heard"]
    await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
    brain._reply_audio_started_at = time.monotonic() - .8
    await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
    await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
    await hello_turn(brain)
    assert spoken(brain) == [GREETING, GREETING]


async def test_fully_played_opening_hello_repeats_only_pending_question(brain):
    marker = await greet(brain)
    await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
    await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
    await brain.process_frame(GreetingPlayedFrame(marker=marker), DOWN)
    assert brain._opening_greeting["heard"]
    await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
    await hello_turn(brain)
    assert spoken(brain) == [GREETING, QUESTION]
    assert not brain._identity.confirmed
    brain._take_decision.assert_not_called()


@pytest.mark.parametrize("feedback_first", [False, True])
async def test_approximate_sentence_feedback_never_discards_opening_intro(brain, feedback_first):
    marker = await greet(brain)
    report = ReplyTTSInterruptedFrame(marker=marker,
        sentences=[(len(INTRO), 2), (len(QUESTION), 3)], texts=[INTRO, QUESTION])
    if feedback_first:
        await brain.process_frame(report, DOWN)
    await cut(brain, played=2.2)
    if not feedback_first:
        await brain.process_frame(report, DOWN)
    await hello_turn(brain)
    assert spoken(brain) == [GREETING, GREETING]


async def test_zero_audio_sentence_cannot_be_counted_as_delivered(brain):
    marker = await greet(brain)
    await cut(brain)
    await brain.process_frame(ReplyTTSInterruptedFrame(marker=marker,
        sentences=[(len(INTRO), 0), (len(QUESTION), 0)], texts=[INTRO, QUESTION]), DOWN)
    await hello_turn(brain)
    assert spoken(brain) == [GREETING, GREETING]


async def test_partial_streaming_shares_smaller_than_played_time_do_not_claim_intro_heard(brain):
    marker = await greet(brain)
    await cut(brain)
    await brain.process_frame(ReplyTTSInterruptedFrame(marker=marker,
        sentences=[(8, .1), (len(INTRO), .2), (len(QUESTION), .3)],
        texts=["Namaste!", INTRO, QUESTION]), DOWN)
    await hello_turn(brain)
    assert spoken(brain) == [GREETING, GREETING]


async def test_second_interruption_and_stale_frames_cannot_loop_intro(brain):
    first = await greet(brain)
    await cut(brain)
    await hello_turn(brain)
    second = brain._opening_greeting["marker"]
    assert second != first
    await brain.process_frame(GreetingPlayedFrame(marker=first), DOWN)
    await brain.process_frame(ReplyTTSCompleteFrame(marker=first, failed=True), DOWN)
    assert not brain._opening_greeting["heard"] and not brain._opening_greeting["failed"]
    await cut(brain)
    await brain.process_frame(GreetingPlayedFrame(marker=second), DOWN)
    assert not brain._opening_greeting["heard"]
    await hello_turn(brain)
    assert spoken(brain)[:2] == [GREETING, GREETING]
    assert len(spoken(brain)) == 3 and "Haier" not in spoken(brain)[-1]
    assert brain._identity.is_identity_ask(spoken(brain)[-1])
    assert brain._opening_greeting is None and not brain._identity.confirmed


@pytest.mark.parametrize("failed", [False, True])
async def test_no_audio_or_failed_opening_fence_never_counts_heard(brain, failed):
    marker = await greet(brain)
    if failed:
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(ReplyTTSCompleteFrame(marker=marker, failed=True), DOWN)
    await brain.process_frame(GreetingPlayedFrame(marker=marker), DOWN)
    assert not brain._opening_greeting["heard"]


@pytest.mark.parametrize("text", [
    "haan", "जी", "हम्म।", "hello haan", "hello wrong number", "hello nahi chahiye",
    "हेलो अभी busy हूं", "hello stop", "hello wait", "hello who are you?",
    "हेलो आप कहां से बोल रही हैं?", "hello I am Vaibhav", "hello mujhe service chahiye",
])
async def test_meaningful_or_affirmative_reply_never_uses_pickup_fast_path(brain, text):
    await greet(brain)
    assert not brain._pickup_hello_pending(text)


@pytest.mark.parametrize("boundary", ["workflow", "ever_routed", "confirmed", "hearing_probe", "closing"])
async def test_pickup_rule_cannot_override_other_owner(brain, boundary):
    await greet(brain)
    if boundary == "workflow":
        brain._active_workflow = "service"
    elif boundary == "ever_routed":
        brain._workflow_ever_routed = True
    elif boundary == "confirmed":
        brain._identity.confirmed = True
    elif boundary == "hearing_probe":
        brain._hearing_probe_pending = {"question": QUESTION, "nodes": []}
    else:
        brain._closing = True
    assert not brain._pickup_hello_pending("hello")


async def test_no_configured_greeting_preserves_existing_routing(brain):
    brain._config.greeting = ""
    await brain._open_session()
    assert brain._opening_greeting is None
    assert not brain._pickup_hello_pending("hello")


async def test_recovery_uses_actual_placeholder_resolved_greeting(brain, monkeypatch):
    brain._config.greeting = "Main {company} se {agent} bol rahi hoon. " + QUESTION
    monkeypatch.setattr(brain, "_placeholder_values", lambda: {"company": "Haier", "agent": "Riya"})
    await greet(brain)
    rendered = brain._opening_greeting["text"]
    assert "Haier" in rendered and "Riya" in rendered and "{" not in rendered
    await cut(brain)
    await hello_turn(brain)
    assert spoken(brain) == [rendered, rendered]


async def test_personalized_memory_opening_recovers_actual_text_and_speech_mode(brain):
    personalized = "Namaste, main Riya Haier se hoon. Aapne pehle request ki thi. " + QUESTION
    await brain._say_opening_greeting(personalized, authored=False)
    await cut(brain)
    await hello_turn(brain)
    assert spoken(brain) == [personalized, personalized]
    assert not brain._opening_greeting["authored"]


@pytest.mark.parametrize("gap", [False, True])
async def test_uninterrupted_soft_hello_keeps_queued_greeting_without_second_question(brain, gap):
    marker = await greet(brain)
    if gap:
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
    # No UserStarted/Interruption: the original opening is still queued.
    await hello_turn(brain)
    assert spoken(brain) == [GREETING]
    assert brain._opening_greeting["marker"] == marker
    assert not brain._opening_greeting["interrupted"]
    assert not brain._identity.confirmed
    brain._take_decision.assert_not_called()


async def test_soft_hello_consumes_only_its_language_and_smart_turn_flags(brain):
    await greet(brain)
    brain._pending_language = "en-IN"
    brain._smart_turn_closed = True
    brain._pending_segments = ["hello"]
    await brain._consume_pending_turn()
    assert brain._pending_language is None and not brain._smart_turn_closed
    assert spoken(brain) == [GREETING]


@pytest.mark.parametrize("notification", ["session_config", "language", "bot_text"])
@pytest.mark.parametrize("confirmed_interrupt", [False, True])
async def test_startup_notification_race_never_loses_or_duplicates_intro(brain, notification, confirmed_interrupt):
    reached = asyncio.Event()
    release = asyncio.Event()
    original_notify = brain._notify_client

    async def deferred_notify(payload):
        if payload.get("type") == notification and not reached.is_set():
            reached.set()
            await release.wait()
        await original_notify(payload)

    brain._client_info = {"sample_rate": 8000}
    brain._conversation_language = "en-IN"
    brain._notify_client = deferred_notify
    original = brain.create_task(brain._open_session())
    brain._generation = original
    await asyncio.wait_for(reached.wait(), 1)
    assert brain._opening_greeting is not None
    assert brain._opening_greeting["marker"] is None
    if confirmed_interrupt:
        await brain.process_frame(UserStartedSpeakingFrame(), DOWN)
        assert original.cancelled() and brain._opening_greeting["interrupted"]
        brain._notify_client = original_notify
        await hello_turn(brain)
    else:
        # A quiet hello alone must preserve the task blocked before TTS.
        await hello_turn(brain)
        assert not original.cancelled() and not original.done()
        release.set()
        await original
    assert spoken(brain) == [GREETING]
    assert not brain._identity.confirmed
    brain._take_decision.assert_not_called()
    assert [record.text for record in brain._recorder.turns if record.role == "user"] == ["hello"]
    notifications = [payload["type"] for payload in brain._notified]
    assert notifications.count("session_config") == 1
    assert notifications.index("session_config") < notifications.index("bot_text")
    assert notifications.count("language") == 1
    assert notifications.index("language") < notifications.index("bot_text")
    first_text = next(i for i, frame in enumerate(brain._pushed) if isinstance(frame, TextFrame))
    switches = [i for i, frame in enumerate(brain._pushed) if isinstance(frame, SwitchVoiceLanguageFrame)]
    assert switches and max(switches) < first_text


async def test_prefetch_is_skipped_for_pickup_hello(brain):
    await greet(brain)
    brain._goal_engine._enabled = True
    brain._decide_turn = AsyncMock()
    brain._start_decision_prefetch(text_override="hello")
    await asyncio.sleep(0)
    brain._decide_turn.assert_not_called()
    assert brain._decision_prefetch is None


async def test_recovery_rollback_restores_opening_for_a_merged_meaningful_turn(brain):
    await greet(brain)
    await cut(brain)
    await hello_turn(brain)
    assert brain._opening_greeting["recovered"]
    claimed = brain._claim_open_turn()
    await brain._rollback_open_turn(claimed)
    assert brain._opening_greeting["interrupted"]
    assert not brain._opening_greeting["recovered"]
    assert brain._pending_segments == ["hello"]


@pytest.mark.parametrize("text", [
    "hello wrong number", "hello abhi busy hoon", "hello kaun bol raha hai?",
    "hello nahi chahiye", "hello wait",
])
async def test_substantive_opening_reply_runs_actual_normal_handler(brain, text):
    await greet(brain)
    await cut(brain)
    await hello_turn(brain, text)
    assert brain._opening_greeting is None
    assert [record.text for record in brain._recorder.turns if record.role == "user"] == [text]
    assert "opening_greeting_recovered" not in brain._recorder.event_kinds()
    assert "pickup_hello_answered" not in brain._recorder.event_kinds()
    assert not brain._identity.confirmed


async def test_late_substantive_tail_rewinds_pickup_and_reaches_normal_denial(brain):
    await greet(brain)
    await cut(brain)
    await hello_turn(brain)
    brain._take_decision.return_value = ConversationDecision(
        decision="denied", intent="identity_confirmation", next_action="end_call",
        signal="wrong_person", confidence=1,
    )
    brain._llm = _LLMStub(reply="Wrong person, understood.")
    final = transcript("main Vaibhav nahi hoon")
    final.timestamp = str(time.monotonic())
    await brain.process_frame(final, DOWN)
    await settle_turn()
    if brain._generation is not None:
        await brain._generation
    users = [record.text for record in brain._recorder.turns if record.role == "user"]
    assert users == ["hello main Vaibhav nahi hoon"]
    assert brain._opening_greeting is None and not brain._identity.confirmed
    assert "turn_merged_late_final" in brain._recorder.event_kinds()
    assert brain._take_decision.call_args.args == (users[0],)


@pytest.mark.parametrize("answer", ["हाँ बोलो।", "हम्म।"])
async def test_recovered_opening_next_identity_answer_still_enters_authored_gate(engine, answer):
    instance, _ = engine
    brain, _ = brain_with_engine(instance)
    brain._config.greeting = f"{INTRO} {AUTHORED_IDENTITY}"
    try:
        await greet(brain)
        await cut(brain)
        await hello_turn(brain)
        assert not brain._identity.confirmed
        recovered_marker = brain._opening_greeting["marker"]
        await brain.process_frame(BotStartedSpeakingFrame(), DOWN)
        await brain.process_frame(BotStoppedSpeakingFrame(), DOWN)
        await brain.process_frame(GreetingPlayedFrame(marker=recovered_marker), DOWN)
        await turn(brain, answer)
        assert brain._identity.confirmed
        assert brain._recorder.workflow_slots["identity_answer"] == "yes"
        assert "contact_answer" not in brain._recorder.workflow_slots
        assert spoken(brain)[-1].endswith(CONTACT)
    finally:
        await cleanup(brain)

