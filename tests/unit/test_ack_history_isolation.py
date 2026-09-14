"""Per-call acknowledgement history reflects heard, current-turn selections."""

import random

import pytest

from shared.orchestration.naturalness import (
    SpeechNaturalnessPlanner,
    normalize_spoken_variant,
)
from shared.orchestration.voice_identity import VoiceIdentity
from tests.unit.test_naturalness_runtime import make_brain


IDENTITY = VoiceIdentity(gender="neutral")


def planner(seed=7):
    return SpeechNaturalnessPlanner(
        {"acknowledgement_probability": 1.0}, rng=random.Random(seed),
    )


def plan_ack(call, turn, *, context="answer", commit=True):
    return call.plan_early_ack(
        language="en-IN", identity=IDENTITY, context=context,
        turn_index=turn, commit=commit,
    )


@pytest.mark.parametrize("call_count", [1, 2, 3])
def test_ten_allowed_ack_turns_are_independent_when_calls_interleave(call_count):
    baseline = planner()
    expected = [plan_ack(baseline, turn) for turn in range(1, 20, 2)]
    calls = [planner() for _ in range(call_count)]
    sequences = [[] for _ in calls]
    for turn in range(1, 20, 2):
        # Alternate order so both same-time and reordered call scheduling are covered.
        order = range(call_count) if turn % 4 == 1 else reversed(range(call_count))
        for index in order:
            call = calls[index]
            sequences[index].append(plan_ack(call, turn))
            assert plan_ack(call, turn + 1) == ""  # Existing consecutive-turn guard.
    for index, sequence in enumerate(sequences):
        print(f"ACK {call_count} calls / Call {chr(65 + index)}: {', '.join(sequence)}")
        assert sequence == expected
        assert all(sequence)
        normalized = list(map(normalize_spoken_variant, sequence))
        assert all(left != right for left, right in zip(normalized, normalized[1:]))


@pytest.mark.parametrize("context", ["answer", "question", "lookup", "neutral"])
def test_small_valid_pool_avoids_immediate_repeat_after_recent_window_fills(context):
    class FirstChoice:
        def random(self):
            return 0.0

        def choice(self, values):
            return values[0]

    call = planner()
    call._rng = FirstChoice()
    selected = [plan_ack(call, turn, context=context) for turn in range(1, 22, 2)]
    assert all(selected)
    assert all(left != right for left, right in zip(selected, selected[1:]))


def test_single_context_valid_variant_may_repeat(monkeypatch):
    from shared.orchestration import naturalness

    monkeypatch.setitem(naturalness._POOLS["en"], "ack_question", ("Hmm…",))
    call = planner()
    assert [plan_ack(call, turn, context="question") for turn in range(1, 8, 2)] == ["Hmm…"] * 4


def test_speculative_ack_does_not_consume_spoken_history():
    call = planner()
    assert plan_ack(call, 1, commit=False)
    assert not call._recent and not call._recent_spoken
    assert call._last_early_ack_turn is None
    assert call.note_early_ack_played(1) is True
    assert len(call._recent_spoken) == 1
    assert call.note_early_ack_played(1) is False  # A repeated callback is idempotent.
    assert len(call._recent_spoken) == 1


def test_obsolete_interrupted_and_replanned_ack_cannot_commit_history():
    call = planner()
    assert plan_ack(call, 1, commit=False)
    assert plan_ack(call, 2, commit=False)
    assert call.note_early_ack_played(1) is False
    assert not call._recent_spoken
    call.discard_early_ack()
    assert call.note_early_ack_played(2) is False
    assert not call._recent_spoken
    assert plan_ack(call, 3, commit=False)
    assert call.note_early_ack_played(3) is True
    assert plan_ack(call, 4) == ""
    assert plan_ack(call, 5)


async def test_brain_cancellation_drops_pending_ack_and_cleanup_releases_call_history():
    call = planner()
    brain = make_brain(naturalness=call)
    assert plan_ack(call, 1, commit=False)
    await brain._cancel_latency_filler("barge_in")
    brain._on_latency_ack_played(1)
    assert brain._early_ack_spoken_turn is None
    assert not call._recent_spoken
    assert plan_ack(call, 2, commit=False)
    brain._on_latency_ack_played(1)
    assert brain._early_ack_spoken_turn is None
    brain._on_latency_ack_played(2)
    assert brain._early_ack_spoken_turn == 2
    assert call._recent_spoken
    assert plan_ack(call, 4, commit=False)
    await brain.cleanup()
    assert not call._recent and not call._recent_spoken
    assert call._last_early_ack_turn is None
    assert call._pending_early_ack is None
    assert call.note_early_ack_played(4) is False
    # A new session starts without any completed-call history.
    fresh = planner()
    assert plan_ack(fresh, 1) == plan_ack(planner(), 1)


def test_brain_withheld_replan_invalidates_old_pending_ack():
    call = planner()
    brain = make_brain(naturalness=call)
    assert plan_ack(call, 1, commit=False)
    brain._closing = True
    assert brain._plan_early_ack("yes") is None
    brain._on_latency_ack_played(1)
    assert brain._early_ack_spoken_turn is None
    assert not call._recent_spoken
    assert call.note_early_ack_played(1) is False
