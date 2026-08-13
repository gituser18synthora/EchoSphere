"""StreamingTTSRouter call-stability regressions.

Root cause of the live "call drops after one or two turns" incident
(2026-08-13): an orphan punctuation fragment reached Sarvam, which rejected
it with a 422 error event ({"message": "400: Text must contain at least one
character from the allowed languages.", "code": 422}) and closed the socket;
the router categorized it invalid_input and ended the WHOLE call
(EndWorkerFrame → media socket close → FreeSWITCH hangs up the PSTN leg).

The contract under test:
- unspeakable text (no letter/digit in any script) never reaches a provider;
- invalid_input is call-fatal ONLY before any audio was delivered this call
  (a genuine configuration error); after audio has played it drops the one
  generation and the call lives on;
- auth stays call-fatal always;
- pause mode arms the first-audio watchdog on the first dispatch.
"""

import asyncio

from pipecat.frames.frames import EndWorkerFrame

from shared.providers.base import ProviderError
from shared.providers.tts.streaming import TTSStreamEvent

from tests.unit.test_tts_router_flush_finals import (
    ENGINE,
    KEY,
    FakeProvider,
    FakeRecorder,
    make_router,
)
from voice_runtime.tts_router import _Generation, _Sentence


def _capture_frames(router):
    pushed = []

    async def push_frame(frame, direction=None):
        pushed.append(frame)

    async def push_error(error_msg=None, **kwargs):
        pushed.append(("error", error_msg))

    router.push_frame = push_frame
    router.push_error = push_error
    return pushed


def _end_worker_frames(pushed):
    return [f for f in pushed if isinstance(f, EndWorkerFrame)]


async def _run_tts(router, text, context_id):
    async for _ in router.run_tts(text, context_id):
        pass


async def test_unspeakable_segment_never_reaches_provider():
    recorder = FakeRecorder()
    router = make_router(pause_ms=200, recorder=recorder)
    provider = FakeProvider()

    async def fake_get_provider(engine, locale):
        return provider

    router._get_provider = fake_get_provider

    for fragment in (".", "…", "!!", "-", "😊"):
        await _run_tts(router, fragment, "ctx_unspeakable")

    assert provider.calls == []
    assert "ctx_unspeakable" not in router._generations
    skipped = [e for e in recorder.events if e["kind"] == "tts_segment_skipped"]
    assert len(skipped) == 5
    assert all(e["reason"] == "no_speakable_text" for e in skipped)

    # A speakable sentence on the same context still dispatches normally.
    await _run_tts(router, "ठीक है।", "ctx_unspeakable")
    assert ("text", "ctx_unspeakable~1", "ठीक है।") in provider.calls


async def test_midcall_invalid_input_does_not_end_call():
    recorder = FakeRecorder()
    router = make_router(pause_ms=200, recorder=recorder)
    pushed = _capture_frames(router)
    state = _Generation(engine=ENGINE, provider=FakeProvider())
    router._generations["ctx_mid"] = state
    state.active = "ctx_mid"  # pause mode only accepts the in-flight dispatch

    # The call has demonstrably spoken: first sentence delivered audio.
    await router._dispatch_event(KEY, TTSStreamEvent(
        kind="audio", generation_id="ctx_mid", audio=b"\x01\x02" * 8,
    ))
    # Then the provider rejects one payload (live Sarvam 422 shape).
    await router._dispatch_event(KEY, TTSStreamEvent(
        kind="error", generation_id="ctx_mid",
        error=ProviderError(
            "sarvam-tts-ws", "invalid_input",
            "400: Text must contain at least one character from the allowed languages.",
        ),
    ))

    assert _end_worker_frames(pushed) == []
    assert router._fatal_call_ended is False
    assert "ctx_mid" not in router._generations  # generation failed and closed
    await asyncio.gather(*router.tasks)
    assert "tts_segment_rejected_by_provider" in recorder.persisted
    assert "tts_fatal" not in recorder.persisted


async def test_repeated_invalid_input_without_audio_between_ends_call():
    """One rejected payload is survivable; a second in a row with no audio
    in between means the engine broke mid-call (e.g. a language switch onto
    a bad config) — the dead-air protection must end the call again."""
    recorder = FakeRecorder()
    router = make_router(pause_ms=200, recorder=recorder)
    pushed = _capture_frames(router)

    error = ProviderError("sarvam-tts-ws", "invalid_input", "rejected")
    for ctx in ("ctx_r1", "ctx_r2"):
        state = _Generation(engine=ENGINE, provider=FakeProvider())
        router._generations[ctx] = state
        state.active = ctx
        if ctx == "ctx_r1":
            # The call spoke before the trouble started.
            await router._dispatch_event(KEY, TTSStreamEvent(
                kind="audio", generation_id=ctx, audio=b"\x01\x02" * 8,
            ))
        await router._dispatch_event(KEY, TTSStreamEvent(
            kind="error", generation_id=ctx, error=error,
        ))

    frames = _end_worker_frames(pushed)
    assert len(frames) == 1 and frames[0].reason == "tts_failure:invalid_input"
    assert router._fatal_call_ended is True


async def test_invalid_input_before_any_audio_still_ends_call():
    """A configuration-level invalid_input (nothing has ever rendered) keeps
    the dead-air protection: the call ends instead of playing silence."""
    recorder = FakeRecorder()
    router = make_router(pause_ms=200, recorder=recorder)
    pushed = _capture_frames(router)
    state = _Generation(engine=ENGINE, provider=FakeProvider())
    router._generations["ctx_cfg"] = state

    await router._dispatch_event(KEY, TTSStreamEvent(
        kind="error", generation_id="ctx_cfg",
        error=ProviderError("sarvam-tts-ws", "invalid_input", "bad config"),
    ))

    frames = _end_worker_frames(pushed)
    assert len(frames) == 1 and frames[0].reason == "tts_failure:invalid_input"
    assert router._fatal_call_ended is True
    assert "tts_fatal" in recorder.persisted


async def test_auth_failure_still_ends_call_even_after_audio():
    recorder = FakeRecorder()
    router = make_router(pause_ms=200, recorder=recorder)
    pushed = _capture_frames(router)
    state = _Generation(engine=ENGINE, provider=FakeProvider())
    router._generations["ctx_auth"] = state
    state.active = "ctx_auth"

    await router._dispatch_event(KEY, TTSStreamEvent(
        kind="audio", generation_id="ctx_auth", audio=b"\x01\x02" * 8,
    ))
    await router._dispatch_event(KEY, TTSStreamEvent(
        kind="error", generation_id="ctx_auth",
        error=ProviderError("sarvam-tts-ws", "auth", "key revoked mid-call"),
    ))

    frames = _end_worker_frames(pushed)
    assert len(frames) == 1 and frames[0].reason == "tts_failure:auth"
    assert router._fatal_call_ended is True


async def test_pause_mode_first_dispatch_arms_first_audio_watchdog():
    """Pause mode flushes per sentence, so the turn-level flush that used to
    arm the watchdog never runs — the first dispatch must arm it itself."""
    router = make_router(pause_ms=200)
    provider = FakeProvider()
    state = _Generation(engine=ENGINE, provider=provider)
    router._generations["ctx_wd"] = state
    state.pending.append(_Sentence(text="One."))

    await router._dispatch_next_sentence("ctx_wd", state)
    assert state.watchdog is not None
    state.watchdog.cancel()
