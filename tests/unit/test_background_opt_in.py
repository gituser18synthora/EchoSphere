"""Opt-in speech protection; the legacy suite and golden baseline remain intact."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import BotStartedSpeakingFrame, Frame, TranscriptionFrame, VADUserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from tests.background_replay import baseline_table, load_clips, replay_segment
from tests.unit.test_background_speech_guard import make_brain, say, level_events
from tests.unit.test_brain_turn_taking import settle_turn, stub_turn_handler
from voice_runtime.barge_in import WordConfirmedBargeInStrategy
from voice_runtime.caller_level import CallerLevelBaseline


async def test_all_new_flags_off_reproduce_the_frozen_pre_fix_baseline():
    expected = json.loads(Path("tests/fixtures/background_replay_baseline.json").read_text())
    assert await baseline_table() == expected


@pytest.mark.parametrize("provisional", [False, True])
async def test_audio_alone_never_commits_or_pauses_when_transcript_is_required(provisional):
    strategy = WordConfirmedBargeInStrategy(
        require_transcript=True, meaningful_words=2, provisional_duck=provisional,
        vad_fallback_secs=.8, commit_secs=.8,
    )
    strategy.trigger_user_turn_started = AsyncMock()
    strategy._on_provisional = AsyncMock()
    clock = [10.0]
    with patch("voice_runtime.barge_in.time.monotonic", side_effect=lambda: clock[0]):
        await strategy.process_frame(BotStartedSpeakingFrame())
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        clock[0] += 3
        await strategy.process_frame(Frame())
        await strategy.process_frame(TranscriptionFrame(text="... ???", user_id="u", timestamp="t"))
        strategy.trigger_user_turn_started.assert_not_awaited()
        strategy._on_provisional.assert_not_awaited()
        await strategy.process_frame(TranscriptionFrame(text="एक मिनट रुकिए", user_id="u", timestamp="t2"))
        strategy.trigger_user_turn_started.assert_awaited_once()


async def test_startup_protection_holds_overlap_but_accepts_a_quiet_bot_answer():
    brain, gate, baseline = make_brain()
    baseline.start_protection = True
    handled, _ = stub_turn_handler(brain)
    await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await say(brain, gate, "बाद में बात करना", -48, during_bot=True)
    assert level_events(brain)[-1]["reason"] == "caller_reference_not_ready"
    assert not brain._pending_segments
    # No baseline was falsely learned from that background overlap.
    assert not baseline.established
    brain._bot_speaking = False
    await say(brain, gate, "हाँ मैं बोल रहा हूँ", -28.3)
    await settle_turn()
    assert handled == ["हाँ मैं बोल रहा हूँ"]


@pytest.mark.parametrize("enabled,expected", [(True, ["held", "held", "accepted"]), (False, ["held"] * 3)])
async def test_repeated_suspect_fallback_is_configurable_in_the_real_brain(enabled, expected):
    brain, gate, baseline = make_brain()
    baseline.observe_trusted(-28.3)
    baseline.quiet_failopen_enabled = enabled
    stub_turn_handler(brain)
    for text in ["बाद में", "अभी", "उधर देखो"]:
        await say(brain, gate, text, -48, segment_ms=1000)
    assert [e["action"] for e in level_events(brain)] == expected


@pytest.mark.parametrize("holds", [0, 1, 2, 5])
def test_quiet_max_holds_keeps_an_explicit_boundary(holds):
    baseline = CallerLevelBaseline(quiet_max_holds=holds)
    assert baseline.quiet_should_fail_open(holds)
    if holds:
        assert not baseline.quiet_should_fail_open(holds - 1)


async def test_confirmed_incident_needs_both_enforcement_and_a_suitable_margin():
    segment = load_clips()[0]["segments"][0]
    assert (await replay_segment(segment, enforce=True, allowance=12))["interrupted"]
    result = await replay_segment(segment, enforce=True, allowance=6, require_transcript=True)
    assert not result["interrupted"] and not result["accepted"]


@pytest.mark.parametrize("delta", [0, -8, -15, -20])
async def test_genuine_caller_attenuation_controls_are_reported_honestly(delta):
    # Controlled attenuation of a caller example is not real speakerphone evidence.
    segment = dict(load_clips()[0]["segments"][0], text="एक मिनट रुकिए", speech_dbfs=-28.3 + delta)
    result = await replay_segment(segment, enforce=True, allowance=6, require_transcript=True)
    assert result["interrupted"] is (delta > -18)


async def test_startup_conservatism_has_a_known_early_caller_tradeoff():
    segment = dict(load_clips()[0]["segments"][0], baseline_dbfs=None, speech_dbfs=-28.3, text="एक मिनट रुकिए")
    assert (await replay_segment(segment, start_protection=False))["interrupted"]
    assert not (await replay_segment(segment, start_protection=True))["interrupted"]
