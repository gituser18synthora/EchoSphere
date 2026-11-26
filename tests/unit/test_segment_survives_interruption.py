"""A caller's short word must not vanish when its own barge-in cancels it.

DEMO 2026-11-25 (haier/qa/REPORT_2026-11-25.md, problems 2 and 3): "hello"
over the greeting or at a question cut the bot's speech, and then nothing was
heard — silence until the no-response prompt. The speech-to-text labelled the
word English ("Hello."), the short English-labelled segment was re-read in the
call's language (a network call), and the interruption that this very segment
confirmed cancelled the handling of the frame in the middle of that call.
"""
import asyncio

import pytest

from voice_runtime.brain import ConversationBrain
from tests.unit.test_ml_ta_deterministic import (
    ENGLISH_LOOKALIKE, MALAYALAM_NO, _multilingual_brain,
)
from tests.unit.test_opening_affirm_and_language_rescue import (
    _GateStub, _frame_in, make_brain, stub_turn_handler, transcript,
)


async def _settle():
    for _ in range(8):
        await asyncio.sleep(0)


def _malayalam_brain(batch):
    gate = _GateStub()
    brain = _multilingual_brain(gate, batch)
    brain._conversation_language = brain._config.language = "ml-IN"
    gate.retained = (b"\x00\x01" * 16000, 16000)
    return brain


def _lookalike():
    return transcript(ENGLISH_LOOKALIKE, language="en-IN", language_code="en-IN",
                      language_probability=0.76)


@pytest.mark.asyncio
async def test_a_reread_cancelled_by_its_own_interruption_still_delivers_the_segment():
    started, release = asyncio.Event(), asyncio.Event()

    async def _batch(pcm, rate, language):
        started.set()
        await release.wait()
        return MALAYALAM_NO

    brain = _malayalam_brain(_batch)
    handled = stub_turn_handler(brain)
    handler = asyncio.create_task(brain._on_transcription(_lookalike()))
    await started.wait()
    handler.cancel()                      # the interruption this segment confirmed
    with pytest.raises(asyncio.CancelledError):
        await handler
    assert handled == []
    release.set()                         # the re-read comes back afterwards
    await _settle()
    await asyncio.gather(*list(brain._detached_segment_tasks))
    for _ in range(40):
        if handled:
            break
        await asyncio.sleep(0.02)
    assert handled == [MALAYALAM_NO]
    resumed = brain._recorder.events_of("stt_segment_resumed_after_interruption")
    assert resumed and resumed[-1]["reread"] is True
    assert not brain._detached_segment_tasks


@pytest.mark.asyncio
async def test_when_the_reread_fails_the_segment_stands_as_heard():
    started, release = asyncio.Event(), asyncio.Event()

    async def _batch(pcm, rate, language):
        started.set()
        await release.wait()
        return ""                          # nothing recovered

    brain = _malayalam_brain(_batch)
    handled = stub_turn_handler(brain)
    handler = asyncio.create_task(brain._on_transcription(_lookalike()))
    await started.wait()
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    release.set()
    await _settle()
    await asyncio.gather(*list(brain._detached_segment_tasks))
    for _ in range(40):
        if handled:
            break
        await asyncio.sleep(0.02)
    assert handled == [ENGLISH_LOOKALIKE]
    assert brain._recorder.events_of("stt_segment_resumed_after_interruption")[-1]["reread"] is False


@pytest.mark.asyncio
async def test_an_uninterrupted_reread_behaves_as_before():
    calls = []

    async def _batch(pcm, rate, language):
        calls.append(language)
        return MALAYALAM_NO

    brain = _malayalam_brain(_batch)
    handled = stub_turn_handler(brain)
    await _frame_in(brain, _lookalike())
    assert calls == ["ml-IN"] and handled == [MALAYALAM_NO]
    assert "stt_segment_resumed_after_interruption" not in brain._recorder.event_kinds()


@pytest.mark.parametrize("text", ["Hello.", "Hello", "Hi", "hello hello", "Thanks.", "Thank you", "Bye", "Good morning"])
def test_a_reaction_is_understood_as_heard_and_never_sent_for_a_reread(text):
    assert not ConversationBrain._unrecognized_latin_fragment(text)


@pytest.mark.parametrize("text", ["In law", "Venda sir", "Era Eli"])
def test_an_unknown_short_latin_fragment_is_still_reread(text):
    assert ConversationBrain._unrecognized_latin_fragment(text)


@pytest.mark.asyncio
async def test_hello_labelled_english_reaches_the_turn_without_a_network_call():
    calls = []

    async def _batch(pcm, rate, language):
        calls.append(language)
        return "हेलो"

    gate = _GateStub()
    brain = make_brain(gate=gate, batch_transcriber=_batch)     # Hindi-default bot
    handled = stub_turn_handler(brain)
    gate.retained = (b"\x00\x01" * 16000, 16000)
    await _frame_in(brain, transcript("Hello.", language="en-IN", language_code="en-IN",
                                      language_probability=0.8))
    assert calls == [] and handled == ["Hello."]
