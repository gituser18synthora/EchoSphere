"""A pickup word is not the answer to a question the greeting never asked.

DEMO 2026-11-25: on 12 of 77 calls the caller said "हाँ" / "जी" / "हाँ जी
बोलिए" as the line connected, the greeting was cut in its first second and
the bot took that word as "yes, I am <name>" (identity_rule: explicit_asked
false) and read out a 17-second explanation to someone who did not know who
was calling.
"""
import pytest

from tests.unit.test_pickup_greeting_recovery import (  # noqa: F401 — fixtures
    GREETING, cut, greet, hello_turn, spoken,
)
from tests.unit.test_pickup_greeting_recovery import brain as plain_brain  # noqa: F401 — fixture
from voice_runtime.brain import ConversationBrain

HEARD_AFTER = ConversationBrain._greeting_question_heard_after(GREETING)


@pytest.fixture
def brain(plain_brain):  # noqa: F811
    """A call whose flow uses the answer contract (it declares answer options
    / closingFollowups): the brain learns that in the call's first second."""
    plain_brain._flow_answer_contract = True
    return plain_brain


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


@pytest.mark.parametrize("text", ["haan", "हाँ जी", "हाँ जी बोलिए", "जी बताइए"])
async def test_a_flow_without_the_answer_contract_keeps_its_opening(plain_brain, text):  # noqa: F811
    """Opt-in: for every other bot a pickup "haan" over the greeting is read
    exactly as before (only a bare "hello" restarts the greeting there)."""
    await greet(plain_brain)
    await cut(plain_brain, played=1.0)
    assert not plain_brain._flow_answer_contract
    assert not plain_brain._pickup_response_before_question(text)
    assert not plain_brain._pickup_hello_pending(text)
    assert plain_brain._pickup_hello_pending("हेलो")


async def test_the_contract_is_learnt_from_the_flow_before_its_first_turn():
    """The opening has no workflow result to learn from: the engine is asked
    once, in the background, when the call starts."""
    from unittest.mock import AsyncMock
    from tests.unit.test_brain_workflow_routing import _WorkflowStub, consumed_result, make_brain
    import asyncio

    workflow = _WorkflowStub(consumed_result("ok"))
    workflow.flow_declarations = AsyncMock(return_value={"doNotCallNode": "m_stop", "answerContract": True})
    learner = make_brain(workflow_engine=workflow)
    try:
        assert learner._flow_answer_contract is False
        await learner._learn_do_not_call_target("wf_entry")
        assert learner._flow_answer_contract is True
        assert learner._flow_dnc_targets == {"wf_entry": "m_stop"}
        await learner._learn_do_not_call_target("wf_entry")
        assert workflow.flow_declarations.await_count == 1
        plain = _WorkflowStub(consumed_result("ok"))
        plain.flow_declarations = AsyncMock(return_value={"doNotCallNode": None, "answerContract": False})
        other = make_brain(workflow_engine=plain)
        await other._learn_do_not_call_target("wf_entry")
        assert other._flow_answer_contract is False and other._flow_dnc_targets == {"wf_entry": None}
        await other.cleanup()
    finally:
        await asyncio.sleep(0)
        await learner.cleanup()


@pytest.mark.parametrize("text", ["हाँ जी, बोलिए। के?", "ए हाँ जी", "हाँ। का", "जी बोलिए अ"])
async def test_a_stray_recognition_fragment_does_not_turn_a_pickup_phrase_into_an_answer(brain, text):
    """LOCAL voice run 2026-11-26: speech-to-text wrote "हाँ जी, बोलिए। के?"
    for a pickup phrase spoken over the greeting; the stray "के?" made it
    "content", identity was confirmed and the purpose line was read out."""
    await greet(brain)
    await cut(brain, played=1.2)
    assert brain._pickup_response_before_question(text)


@pytest.mark.parametrize("text", ["हाँ जी, रोहन बोल रहा हूँ", "नहीं जी", "हाँ जी बताइए क्या काम है"])
async def test_real_words_are_never_dropped_as_fragments(brain, text):
    await greet(brain)
    await cut(brain, played=1.2)
    assert not brain._pickup_response_before_question(text)
