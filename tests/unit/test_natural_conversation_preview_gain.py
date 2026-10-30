"""Bot breathing previews use the same attenuation as call playback."""

from types import SimpleNamespace

import pytest

import backend.routers.natural_conversation as router
from shared.audio.pcm import wav_to_pcm
from voice_runtime.latency_filler import FillerClipLibrary, scale_pcm


@pytest.mark.parametrize("gain", [0.0, -6.0])
def test_clip_preview_applies_effective_bot_gain_without_changing_shared_pcm(monkeypatch, gain):
    library = FillerClipLibrary(None)
    clip_id = "synth:breath:male:3"
    original = library.render_clip(clip_id, router.PREVIEW_SAMPLE_RATE)
    monkeypatch.setattr(router, "get_filler_library", lambda: library)
    monkeypatch.setattr(router, "_bot_checked", lambda *args: SimpleNamespace(id="bot-test"))
    monkeypatch.setattr(router, "_load_config_sync", lambda *args: SimpleNamespace(
        human_speech={"breath_gain_db": gain},
    ))

    response = router.natural_conversation_clip("bot-test", id=clip_id, user=None, db=None)

    pcm, rate = wav_to_pcm(response.body)
    assert rate == router.PREVIEW_SAMPLE_RATE
    assert pcm == scale_pcm(original, gain)
    assert library.render_clip(clip_id, rate) == original
    # A settings change must revalidate the otherwise unchanged preview URL.
    assert response.headers["cache-control"] == "private, no-cache"
