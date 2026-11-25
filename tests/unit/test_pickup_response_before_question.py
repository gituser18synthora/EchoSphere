"""A pickup word is not the answer to a question the greeting never asked.

DEMO 2026-11-25: on 12 of 77 calls the caller said "हाँ" / "जी" / "हाँ जी
बोलिए" as the line connected, the greeting was cut in its first second and
the bot took that word as "yes, I am <name>" (identity_rule: explicit_asked
false) and read out a 17-second explanation to someone who did not know who
was calling.
"""
import pytest

from tests.unit.test_pickup_greeting_recovery import (  # noqa: F401 — fixtures
    GREETING, brain, cut, greet, hello_turn, spoken,
)
from voice_runtime.brain import ConversationBrain

HEARD_AFTER = ConversationBrain._greeting_question_heard_after(GREETING)


def test_the_question_counts_as_heard_only_once_its_name_has_played():
    intro, question = GREETING.split(". ", 1)
    assert len(intro) / 14 < HEARD_AFTER < len(GREETING) / 14
    assert ConversationBrain._greeting_question_heard_after("") == 0.0


@pytest.mark.parametrize("text", [
    "haan", "हाँ", "जी", "हाँ जी", "जी हाँ", "हाँ जी बोलिए", "जी बताइए", "hello haan ji", "हेलो हाँ", "हम्म।",
    "नमस्कार जी हाँ जी बोलिए", "yes", "बोलिए",
])
@pytest.mark.parametrize("played", [0.0, 1.0, 2.5])
async def test_a_pickup_word_over_the_first_words_restarts_the_greeting(brain, text, played):
    await greet(brain)
    await cut(brain, played=played)
    await hello_turn(brain, text)
    assert spoken(brain) == [GREETING, GREETING]
    assert not brain._identity.confirmed and not brain._identity.denied
    assert brain._active_workflow is None and not brain._workflow_ever_routed
    brain._take_decision.assert_not_called()
    assert brain._opening_greeting["recovered"]


@pytest.mark.parametrize("text", ["haan", "जी", "हाँ जी बोलिए"])
async def test_the_same_word_after_the_question_was_played_is_the_answer(brain, text):
    await greet(brain)
    await cut(brain, played=HEARD_AFTER + 0.5)
    assert not brain._pickup_hello_pending(text)


@pytest.mark.parametrize("text", ["haan", "जी", "हाँ जी बोलिए"])
async def test_an_uninterrupted_greeting_is_answered_as_before(brain, text):
    await greet(brain)
    assert not brain._pickup_hello_pending(text)


@pytest.mark.parametrize("text", [
    "नहीं", "wrong number", "कौन बोल रहा है?", "haan main Vaibhav bol raha hoon", "अभी busy हूँ", "हाँ बताइए क्या काम है",
])
async def test_a_reply_with_content_is_never_treated_as_a_pickup_word(brain, text):
    await greet(brain)
    await cut(brain, played=1.0)
    assert not brain._pickup_response_before_question(text)


async def test_the_greeting_is_restarted_once_only(brain):
    await greet(brain)
    await cut(brain, played=1.0)
    await hello_turn(brain, "हाँ")
    assert spoken(brain) == [GREETING, GREETING]
    await cut(brain, played=1.0)
    assert not brain._pickup_response_before_question("हाँ")
