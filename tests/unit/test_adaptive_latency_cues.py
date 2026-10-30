"""Adaptive first-cue timing with real cancellation and PCM playback paths."""
import time

import pytest
from pipecat.frames.frames import UserStartedSpeakingFrame

from shared.orchestration.naturalness import SpeechNaturalnessPlanner
from tests.unit.test_latency_filler import (
    DOWN, _AcknowledgementCueStub, _CueStub, _FillerStub, _ShortLibrary,
    filler_audio, make_brain, make_filler, tts_audio, wait,
)


def adaptive_filler(cues=None):
    return make_filler(
        delay_ms=1500, library=_ShortLibrary(clip_ms=80),
        cue_library=cues or _CueStub(clip_ms=80),
        hmm_after_ms=3500, spoken_after_ms=5000,
    )


@pytest.mark.parametrize('context,deadline', [
    ('lookup', 1500), ('thinking', 1800), ('information', 2000),
    ('concern', 2000), ('neutral', 2200), ('confirm', 2500),
    ('affirm', 2500), ('polite', 2500),
])
def test_deadline_depends_on_turn_context(context, deadline):
    planner = SpeechNaturalnessPlanner({'adaptive_latency_cues': True, 'latency_cue_probability': 1})
    plan = planner.plan_latency_cue(language='hi-IN', context=context)
    assert plan.delay_ms == deadline


def test_prefetched_decision_does_not_mean_response_audio_is_ready():
    planner = SpeechNaturalnessPlanner({'adaptive_latency_cues': True, 'latency_cue_probability': 1})
    plan = planner.plan_latency_cue(language='en-IN', context='lookup', expected_fast=True)
    assert plan.verbal and plan.delay_ms == 2500
    legacy = SpeechNaturalnessPlanner({'latency_cue_probability': 1})
    plan = legacy.plan_latency_cue(language='en-IN', expected_fast=True)
    assert not plan.verbal and plan.delay_ms is None


@pytest.mark.parametrize('kwargs,reason', [
    ({'critical': True}, 'critical_content'),
    ({'early_ack_spoken': True}, 'ack_already_spoken'),
])
def test_existing_content_and_ack_gates_remain(kwargs, reason):
    planner = SpeechNaturalnessPlanner({'adaptive_latency_cues': True, 'latency_cue_probability': 1})
    plan = planner.plan_latency_cue(language='hi-IN', **kwargs)
    assert not plan.verbal and plan.reason == reason


async def test_context_and_timing_reach_processor_without_extra_model_call():
    filler = _FillerStub()
    brain = make_brain(filler)
    brain._naturalness = SpeechNaturalnessPlanner({
        'adaptive_latency_cues': True, 'latency_cue_probability': 1,
        'acknowledgement_probability': 0,
    })
    for text, context, first, delay in [
        ('यह कितने दिन में हो जाएगा', 'thinking', 'hmm', 1800),
        ('धन्यवाद जी', 'polite', 'ji', 2500),
        ('मैंने order guard को दे दिया था और customer ने बोला था रख दो', 'information', 'achha', 2000),
    ]:
        plan = brain._plan_latency_cue(text)
        assert (plan.context, plan.cue_ids[0], plan.delay_ms) == (context, first, delay)
        await brain._arm_latency_filler(text)
        assert filler.arms[-1]['cue_after_ms'] == delay
        assert filler.arms[-1]['cue_selection']['primary'] == first
    await brain.cleanup()


async def test_cue_starts_from_caller_stop_without_preceding_breath():
    filler = adaptive_filler()
    # Simulate dispatch taking 1.46 s; deadline remains anchored to speech end.
    origin = time.monotonic() - 1.46
    await filler.arm(turn_id=1, gender='male', speech_stopped_at=origin, cue_after_ms=1500)
    await wait(.02)
    assert not filler_audio(filler)
    await wait(.15)
    played = filler._recorder.data('latency_filler_played')
    assert [p['rung'] for p in played] == ['hmm']
    assert 1500 <= played[0]['waited_ms'] < 1800
    assert filler._library.requests == []
    await filler.cancel()


async def test_fast_reply_cancels_before_any_filler_and_is_forwarded():
    filler = adaptive_filler()
    await filler.arm(turn_id=1, gender='male', speech_stopped_at=time.monotonic()-1.3, cue_after_ms=1500)
    reply = tts_audio()
    await filler.process_frame(reply, DOWN)
    await wait(.25)
    assert not filler_audio(filler)
    assert any(f is reply for f, _ in filler.pushed)
    assert not filler.armed


@pytest.mark.parametrize('interrupt', [False, True])
async def test_reply_or_caller_interrupts_playing_cue_immediately(interrupt):
    filler = adaptive_filler(_CueStub(clip_ms=600))
    await filler.arm(turn_id=1, gender='male', speech_stopped_at=time.monotonic()-1.48, cue_after_ms=1500)
    await wait(.08)
    assert filler_audio(filler)
    owner = filler._armed.owner
    event = UserStartedSpeakingFrame() if interrupt else tts_audio()
    await filler.process_frame(event, DOWN)
    count = len(filler_audio(filler))
    await wait(.08)
    assert len(filler_audio(filler)) == count
    assert owner.cancelled and not filler.armed
    assert any(f is event for f, _ in filler.pushed)


@pytest.mark.parametrize('withheld', [False, True])
async def test_missing_or_withheld_cue_gets_one_breath_without_later_hmm(withheld):
    cues = _CueStub(missing=('hmm',) if not withheld else ())
    filler = adaptive_filler(cues)
    await filler.arm(
        turn_id=1, gender='male', speech_stopped_at=time.monotonic()-1.48,
        cue_after_ms=1500, allow_voiced=not withheld,
    )
    await wait(.15)
    cues.missing.clear()  # Late render must not introduce another thinking cue.
    await wait(.1)
    assert [p['rung'] for p in filler._recorder.data('latency_filler_played')] == ['breath']
    assert len(filler._recorder.data('adaptive_cue_fallback')) == 1
    await filler.cancel()


async def test_ready_ack_replaces_cue_and_breath():
    filler = adaptive_filler(_AcknowledgementCueStub(clip_ms=80))
    await filler.arm(
        turn_id=1, gender='female', speech_stopped_at=time.monotonic()-1.48,
        cue_after_ms=1500, acknowledgement={'text': 'जी…', 'context': 'answer'},
    )
    await wait(.2)
    played = filler._recorder.data('latency_filler_played')
    assert len(played) == 1 and played[0]['sound'] == 'acknowledgement'
    assert len(filler._recorder.data('early_ack_played')) == 1
    assert filler._library.requests == []
    await filler.cancel()


async def test_turns_without_vad_use_dispatch_and_ladder_off_ignores_adaptive():
    filler = adaptive_filler()
    dispatched = time.monotonic()
    await filler.arm(turn_id=1, gender='male', dispatched_at=dispatched, cue_after_ms=2200)
    assert filler._armed.fire_at == pytest.approx(dispatched+2.2)
    await filler.cancel()
    filler = make_filler(delay_ms=50)
    await filler.arm(turn_id=1, gender='male', cue_after_ms=1500)
    assert filler._armed.cue_after_s is None
    await wait(.16)
    assert filler._recorder.data('latency_filler_played')[0]['rung'] == 'breath'
    await filler.cancel()


async def test_adaptive_deadline_through_brain_tts_and_paced_output(tmp_path):
    import json
    from tests.unit.test_latency_filler_readiness import run_readiness_scenarios
    from voice_runtime.voiced_cues import VoicedCueLibrary

    async def render(engine, language, text):
        return (4000).to_bytes(2, 'little') * 9600, 16000

    rows = await run_readiness_scenarios(
        False, threshold_ms=2000, adaptive=True,
        cue_library=VoicedCueLibrary(renderer=render),
    )
    (tmp_path / 'adaptive-cue-timings.json').write_text(json.dumps(rows, indent=2))
    for row in rows:
        ready, forwarded, playback = (row[key] for key in (
            'first_playable_tts_audio_ms', 'response_forwarded_ms', 'response_playback_ms',
        ))
        assert ready <= forwarded <= playback, row
        if row['filler_start_ms'] is None:
            assert forwarded - ready < 50, row
        else:
            # A started voiced cue completes, including its 300 ms silence;
            # a reply arriving later does not get a second gap.
            complete = row['filler_complete_ms']
            assert complete is not None, row
            assert 0 <= forwarded - max(ready, complete) < 50, row
        assert row['filler_frames_after_reply_forwarded'] == 0, row
        assert row['filler_writes_after_reply_started'] == 0, row
        if row['scenario'] in {'fast_llm_fast_tts', 'audio_before_threshold'}:
            assert row['filler_start_ms'] is None, row
        else:
            assert 1990 <= row['filler_start_ms'] < 2150, row
            played = [e for e in row['filler_events'] if e['kind'] == 'latency_filler_played']
            assert len(played) == 1 and played[0]['rung'] == 'hmm', row
