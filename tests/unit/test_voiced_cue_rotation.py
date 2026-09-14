"""Per-call voiced cue histories with shared, nonblocking render caches."""

import asyncio
import gc
import weakref

import pytest

from shared.orchestration import naturalness
from shared.orchestration.naturalness import ladder_cue_text
from voice_runtime.voiced_cues import VoicedCueLibrary


RATE = 16000
ENGINE = {"provider": "mock", "voice": "call-voice"}
CHOICE = {"primary": "hmm", "alternates": ["ji"]}


def ready_library(cues=("hmm", "ji"), *, language="hi", kind="hmm"):
    library = VoicedCueLibrary()
    for index, cue_id in enumerate(cues):
        text = ladder_cue_text(language, kind, cue_id)
        assert text
        library._clips[library._key(ENGINE, language, kind, text)] = (
            (index + 1000).to_bytes(2, "little") * 160, RATE,
        )
    return library


def pick(session, *, language="hi", kind="hmm", selection=CHOICE):
    assert session.clip(ENGINE, language, kind, RATE, selection=selection)
    return session.last_cue_id


@pytest.mark.parametrize("call_count", [1, 2, 3])
def test_interleaved_calls_match_independent_sequences(call_count):
    library = ready_library()
    calls = [library.new_session() for _ in range(call_count)]
    sequences = [[] for _ in calls]
    for turn in range(10):
        # Change interleaving order, so a shared counter cannot accidentally
        # look independent just because each call has a fixed parity.
        order = range(call_count) if turn % 2 == 0 else reversed(range(call_count))
        for index in order:
            sequences[index].append(pick(calls[index]))
    for index, sequence in enumerate(sequences):
        print(f"Voiced Call {chr(65 + index)}: {', '.join(sequence)}")
        assert sequence == ["hmm", "ji"] * 5
    assert library.last_cue_id is None


def test_context_with_only_one_valid_cue_allows_repetition():
    session = ready_library().new_session()
    selection = {"primary": "ji", "alternates": []}
    assert [pick(session, selection=selection) for _ in range(10)] == ["ji"] * 10
    # Switching context must not pull a prior context's cue back into use.
    assert pick(session, selection={"primary": "hmm"}) == "hmm"


@pytest.mark.asyncio
async def test_failed_preference_rotates_ready_fallbacks_without_render_storm():
    library = ready_library()
    attempts = []

    async def fail(engine, language, text):
        attempts.append(text)
        raise RuntimeError("intentional unavailable alternate")

    library._renderer = fail
    selection = {"primary": "hoon", "alternates": ["hmm", "ji"]}
    session = library.new_session()
    sequence = []
    for _ in range(10):
        sequence.append(pick(session, selection=selection))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    await asyncio.gather(*library._tasks.values())
    print(f"Voiced failed preference: {', '.join(sequence)}")
    assert sequence == ["hmm", "ji"] * 5
    assert attempts == ["हूँ…"]
    assert library.render_failures == 1


@pytest.mark.asyncio
async def test_only_ready_cue_repeats_when_alternate_fails():
    library = ready_library(("hmm",))
    attempts = []

    async def fail(engine, language, text):
        attempts.append(text)
        raise RuntimeError("intentional unavailable alternate")

    library._renderer = fail
    session = library.new_session()
    assert [pick(session) for _ in range(10)] == ["hmm"] * 10
    await asyncio.gather(*library._tasks.values())
    assert [pick(session) for _ in range(10)] == ["hmm"] * 10
    assert attempts == ["जी…"]


def test_wait_phrase_singleton_repeats_safely_and_is_call_local():
    library = ready_library(("ek_second",), kind="wait")
    calls = [library.new_session(), library.new_session()]
    for index, session in enumerate(calls):
        sequence = [pick(session, kind="wait", selection=None) for _ in range(10)]
        print(f"Wait Call {chr(65 + index)}: {', '.join(sequence)}")
        assert sequence == ["ek_second"] * 10
    assert calls[0]._last_cues is not calls[1]._last_cues


def test_wait_pool_with_multiple_valid_options_rotates_without_cross_call_state(monkeypatch):
    # Exercise generic selection with a test-only second phrase. Production
    # phrase pools are unchanged by this fix.
    monkeypatch.setitem(
        naturalness._LADDER_CUE_POOLS["en"], "wait",
        (("one_second", "One second…"), ("one_moment", "One moment…")),
    )
    library = ready_library(("one_second", "one_moment"), language="en", kind="wait")
    calls = [library.new_session(), library.new_session()]
    sequences = [[], []]
    for _ in range(10):
        for index, session in enumerate(calls):
            sequences[index].append(pick(session, language="en", kind="wait", selection=None))
    for index, sequence in enumerate(sequences):
        print(f"Multi-option wait Call {chr(65 + index)}: {', '.join(sequence)}")
        assert sequence == ["one_second", "one_moment"] * 5


def test_new_call_and_cleanup_reset_only_session_history():
    library = ready_library()
    first = library.new_session()
    second = library.new_session()
    assert pick(first) == "hmm"
    assert pick(second) == "hmm"
    first.clear_history()
    assert first._last_cues == {}
    assert first.last_cue_id is None
    assert pick(second) == "ji"
    assert pick(library.new_session()) == "hmm"
    assert len(library._clips) == 2


@pytest.mark.asyncio
async def test_background_render_cache_does_not_retain_ended_session():
    rendering = asyncio.Event()
    finish = asyncio.Event()

    async def render(engine, language, text):
        rendering.set()
        await finish.wait()
        return b"\xe8\x03" * 1600, RATE

    library = VoicedCueLibrary(renderer=render)
    session = library.new_session()
    assert session.clip(ENGINE, "hi", "hmm", RATE) == b""
    await rendering.wait()
    reference = weakref.ref(session)
    session.clear_history()
    del session
    gc.collect()
    assert reference() is None
    finish.set()
    await asyncio.gather(*library._tasks.values())
    assert pick(library.new_session(), selection={"primary": "hmm"}) == "hmm"


def test_histories_are_separate_for_active_voice_and_rung():
    library = ready_library()
    other_engine = {**ENGINE, "voice": "different-voice"}
    for cue in ("hmm", "ji"):
        text = ladder_cue_text("hi", "hmm", cue)
        library._clips[library._key(other_engine, "hi", "hmm", text)] = (
            b"\xe8\x03" * 160, RATE,
        )
    session = library.new_session()
    assert pick(session) == "hmm"
    assert session.clip(other_engine, "hi", "hmm", RATE, selection=CHOICE)
    assert session.last_cue_id == "hmm"
    assert pick(session) == "ji"
