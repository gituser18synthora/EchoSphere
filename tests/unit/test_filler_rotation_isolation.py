"""Call-local selection history over shared, cached operator filler assets.

Use the real processor arm/deadline/PCM/readiness path; one library stands in
for the process-wide catalog used by every call. The sequence output is useful
when checking interleaving: all calls must start with their own primary.
"""

import asyncio
import gc
import weakref

import pytest
from pipecat.frames.frames import EndFrame, InterruptionFrame

from tests.unit.test_latency_filler import (
    DOWN,
    RATE,
    _ShortLibrary,
    _tone_wav,
    filler_audio,
    make_filler,
    tts_audio,
)
from voice_runtime.latency_filler import FillerClipLibrary


KINDS = ("breath", "inhale", "exhale")


def configured_library(tmp_path, *, alternate="available"):
    selections = {}
    for kind in KINDS:
        primary = f"{kind}_male_primary.wav"
        secondary = f"{kind}_male_alternate.wav"
        (tmp_path / primary).write_bytes(_tone_wav(RATE, 60, 1000))
        if alternate == "available":
            (tmp_path / secondary).write_bytes(_tone_wav(RATE, 60, 2000))
        elif alternate == "broken":
            (tmp_path / secondary).write_bytes(b"RIFF invalid PCM WAV")
        selections[kind] = {
            "primary": f"file:{primary}",
            "alternates": [] if alternate == "none" else [f"file:{secondary}"],
        }
    return FillerClipLibrary(tmp_path), selections


async def play_turn(filler, turn, selection, *, kind="breath", interrupt=False):
    """Wait for actual owned PCM, then give real response/interruption priority."""
    before = len(filler_audio(filler))
    await filler.arm(
        turn_id=turn, gender="male", filler_kind=kind, filler_selection=selection,
    )
    async with asyncio.timeout(1):
        while len(filler_audio(filler)) == before:
            await asyncio.sleep(0.002)
    frame = filler_audio(filler)[before]
    assert frame.owner.turn_id == turn
    clip_id = filler._recorder.data("latency_filler_played")[-1]["clip"]
    # The signal value in the actual emitted PCM must match its selected id;
    # telemetry alone would not prove the alternate reached the runtime path.
    expected_peak = 2000 if "alternate" in clip_id else 1000
    samples = memoryview(frame.audio).cast("h")
    assert max(samples) == expected_peak
    await filler.process_frame(InterruptionFrame() if interrupt else tts_audio(), DOWN)
    assert frame.owner.cancelled
    assert not filler.armed
    return "A" if "alternate" in clip_id else "P"


def show_sequences(label, sequences):
    print(f"\n{label}")
    for call, choices in sequences.items():
        print(f"Call {call}: {', '.join(choices)}")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("calls", [1, 2, 3])
async def test_ten_runtime_turns_rotate_independently_for_each_call(tmp_path, calls, kind):
    library, selections = configured_library(tmp_path)
    fillers = {
        chr(65 + index): make_filler(delay_ms=10, library=library)
        for index in range(calls)
    }
    sequences = {call: [] for call in fillers}
    try:
        assert len({id(filler._library) for filler in fillers.values()}) == calls
        for turn in range(1, 11):
            # Reverse the dispatch order on alternate turns. Concurrency plus
            # changing order catches global cursors even with three variants.
            order = list(fillers)
            if turn % 2 == 0:
                order.reverse()
            choices = await asyncio.gather(*(
                play_turn(fillers[call], turn, selections[kind], kind=kind)
                for call in order
            ))
            for call, choice in zip(order, choices):
                sequences[call].append(choice)
        show_sequences(f"{calls} call(s), {kind}, concurrent/interleaved", sequences)
        assert sequences == {call: ["P", "A"] * 5 for call in fillers}
        assert not library._cursor
        assert library.last_clip_id is None
        assert library.last_played_at is None
    finally:
        await asyncio.gather(*(filler.cleanup() for filler in fillers.values()))


@pytest.mark.parametrize("alternate", ["none", "missing", "broken"])
async def test_primary_only_and_unavailable_alternate_stay_playable(tmp_path, monkeypatch, alternate):
    library, selections = configured_library(tmp_path, alternate=alternate)
    renders = []
    if alternate == "broken":
        broken = library.find(selections["breath"]["alternates"][0])
        render = broken.render

        def counted_render(rate):
            renders.append(rate)
            return render(rate)

        monkeypatch.setattr(broken, "render", counted_render)
    fillers = {call: make_filler(delay_ms=10, library=library) for call in ("A", "B")}
    sequences = {call: [] for call in fillers}
    try:
        for turn in range(1, 11):
            for call, filler in fillers.items():
                sequences[call].append(await play_turn(filler, turn, selections["breath"]))
        show_sequences(f"Alternate {alternate}, two calls", sequences)
        assert sequences == {call: ["P"] * 10 for call in fillers}
        if alternate == "broken":
            assert renders == [RATE], "failed WAV should be negatively cached across calls"
    finally:
        await asyncio.gather(*(filler.cleanup() for filler in fillers.values()))


async def test_interrupted_turn_advances_only_if_filler_was_emitted(tmp_path):
    library, selections = configured_library(tmp_path)
    filler = make_filler(delay_ms=10, library=library)
    other = make_filler(delay_ms=10, library=library)
    try:
        # Interrupted before its latency deadline: no filler was selected/heard.
        await filler.arm(turn_id=1, gender="male", filler_selection=selections["breath"])
        await filler.process_frame(InterruptionFrame(), DOWN)
        assert filler_audio(filler) == []
        assert filler._library.last_clip_id is None
        heard = [await play_turn(filler, 2, selections["breath"], interrupt=True)]
        interrupted_owner = filler_audio(filler)[-1].owner
        for turn in range(3, 7):
            heard.append(await play_turn(filler, turn, selections["breath"]))
        other_choices = [await play_turn(other, 1, selections["breath"])]
        show_sequences("Pending and audible interruption", {"A": heard, "B": other_choices})
        assert heard == ["P", "A", "P", "A", "P"]
        assert other_choices == ["P"]
        assert interrupted_owner.cancelled
        assert all(frame.owner.cancelled for frame in filler_audio(filler))
    finally:
        await filler.cleanup()
        await other.cleanup()


async def test_recently_played_history_is_shared_within_call_but_not_across_calls(tmp_path):
    library, selections = configured_library(tmp_path)
    session_a = library.new_session()
    session_b = library.new_session()
    filler_a = make_filler(delay_ms=10, library=session_a)
    filler_b = make_filler(delay_ms=10, library=session_b)
    try:
        # A call's TTS router receives the same session object, so its inhale
        # guard observes that call's pre-reply breath and no other call's.
        assert filler_a._library is session_a
        assert filler_b._library is session_b
        assert not session_a.recently_played(10)
        assert not session_b.recently_played(10)
        await play_turn(filler_a, 1, selections["breath"])
        assert session_a.recently_played(10)
        assert not session_b.recently_played(10)
        assert not library.recently_played(10)
        await filler_b.arm(turn_id=1, gender="male", filler_selection=selections["breath"])
        await filler_b.process_frame(InterruptionFrame(), DOWN)
        assert not session_b.recently_played(10)
        assert session_a.recently_played(10)
    finally:
        await filler_a.cleanup()
        await filler_b.cleanup()


@pytest.mark.parametrize("end_frame", [False, True])
async def test_call_end_releases_history_and_new_call_starts_primary(tmp_path, end_frame):
    library, selections = configured_library(tmp_path)
    filler = make_filler(delay_ms=10, library=library)
    old_history = filler._library
    assert await play_turn(filler, 1, selections["breath"]) == "P"
    assert old_history._cursor and old_history.last_clip_id
    assert old_history.recently_played(10)
    if end_frame:
        await filler.process_frame(EndFrame(), DOWN)
    else:
        await filler.cleanup()
    assert old_history._cursor == {}
    assert old_history.last_clip_id is None
    assert old_history.last_played_at is None
    assert library._rendered, "call end must retain shared rendered assets"
    assert old_history._rendered is library._rendered
    if end_frame:
        await filler.cleanup()
    reference = weakref.ref(old_history)
    del old_history
    del filler
    # No global map may retain finished call histories. Let completed asyncio
    # callbacks release their temporary references before checking collection.
    await asyncio.sleep(0)
    gc.collect()
    assert reference() is None
    replacement = make_filler(delay_ms=10, library=library)
    try:
        sequence = [await play_turn(replacement, turn, selections["breath"]) for turn in range(1, 5)]
        show_sequences("Call ended; replacement call", {"new": sequence})
        assert sequence == ["P", "A", "P", "A"]
    finally:
        await replacement.cleanup()


async def test_injected_test_library_keeps_its_existing_contract():
    library = _ShortLibrary()
    filler = make_filler(delay_ms=10, library=library)
    try:
        assert filler._library is library
    finally:
        await filler.cleanup()
