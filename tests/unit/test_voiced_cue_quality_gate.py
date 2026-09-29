"""Filler/cue render quality gate (voice_runtime.voiced_cues, 2026-09-29).

A cue is rendered once per voice and replayed on every call. These tests pin
what reaches that cache: acknowledgements are synthesized without their
trailing ellipsis (Eleven v3 read "अच्छा…" as a sigh), a take is never cut
while a word is still sounding, clearly bad acknowledgement takes are
rejected and rendered again, and a cue with no acceptable take is skipped for
that voice instead of cached breathy. Voice is modelled by a harmonic tone
(periodic), breath/whisper by noise (aperiodic).
"""

import hashlib
import json

import numpy as np
import pytest

from shared.audio.pcm import pcm_to_wav_bytes
from shared.audio.text import sanitize_for_tts
from voice_runtime.voiced_cues import (
    _MAX_RENDER_ATTEMPTS,
    _RENDER_VERSION,
    VoicedCueLibrary,
    assess_take,
    synthesis_text,
    trim_silence,
)

RATE = 24000
ENGINE = {"provider": "elevenlabs", "model": "eleven_v3_conversational", "voice": "raju",
          "params": {"stability": 0}}
ACHHA = "अच्छा…"
JI_THEEK_HAI = "जी, ठीक है…"
LONG_LOOKUP = "एक सेकंड, देख रहा हूँ…"


def voiced(ms, *, f0=120.0, level=0.3):
    """A sounded vowel: harmonic tone with a 20 ms attack and 40 ms release."""
    n = int(RATE * ms / 1000)
    t = np.arange(n) / RATE
    tone = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 8))
    tone = tone / np.abs(tone).max() * level * 32767
    env = np.ones(n)
    attack, release = int(RATE * 0.02), int(RATE * 0.04)
    env[:attack] = np.linspace(0.0, 1.0, attack)
    env[-release:] = np.linspace(1.0, 0.0, release)
    return tone * env


def breath(ms, *, level=0.02, seed=7):
    """Breath / whisper: aperiodic noise."""
    return np.random.default_rng(seed).normal(0.0, level * 32767, int(RATE * ms / 1000))


def silence(ms):
    return np.zeros(int(RATE * ms / 1000))


def pcm(*parts):
    return np.clip(np.concatenate(parts), -32768, 32767).astype("<i2").tobytes()


def ms_of(clip, rate=RATE):
    return len(clip) / 2 / rate * 1000.0


def renderer(takes, calls):
    """A renderer returning ``takes`` in order (the last one repeats)."""
    async def render(engine, language, text, *, sample_rate):
        calls.append(text)
        return takes[min(len(calls), len(takes)) - 1], RATE
    return render


# ── what is synthesized, and under which identity ─────────────────────────

def test_ack_ellipsis_is_dropped_only_from_the_synthesized_text():
    assert synthesis_text("ack", ACHHA) == "अच्छा"
    assert synthesis_text("ack", JI_THEEK_HAI) == "जी, ठीक है"
    assert synthesis_text("ack", "Okay...") == "Okay"
    # Only the TRAILING ellipsis goes; an inner one is part of the phrasing.
    assert synthesis_text("ack", "Hmm… देख रहा हूँ…") == "Hmm… देख रहा हूँ"
    # Ladder cues are synthesized exactly as written.
    assert synthesis_text("hmm", "Hmm…") == "Hmm…"
    assert synthesis_text("wait", "एक सेकंड…") == "एक सेकंड…"


async def test_renderer_receives_the_ack_without_its_ellipsis(tmp_path):
    calls = []
    lib = VoicedCueLibrary(tmp_path, renderer=renderer([pcm(silence(150), voiced(420), silence(150))], calls))
    assert lib.acknowledgement_clip(ENGINE, "hi-IN", ACHHA, 16000) == b""  # never blocks
    await lib.wait_ready(ENGINE, "hi-IN")
    for task in list(lib._tasks.values()):
        await task
    assert calls == ["अच्छा"]
    assert lib.acknowledgement_clip(ENGINE, "hi-IN", ACHHA, 16000)


def test_cache_identity_is_the_synthesized_text_and_render_version():
    lib = VoicedCueLibrary()
    key = lib._key(ENGINE, "hi-IN", "ack", ACHHA)
    assert key == lib._key(ENGINE, "hi-IN", "ack", "अच्छा")
    assert key.endswith(f"_r{_RENDER_VERSION}")
    assert key != lib._key(ENGINE, "hi-IN", "ack", JI_THEEK_HAI)
    assert key != lib._key({**ENGINE, "params": {"stability": 0.5}}, "hi-IN", "ack", ACHHA)


async def test_clips_cached_by_the_previous_pipeline_are_never_selected(tmp_path):
    # The pre-gate key: digest of the pool text WITH its ellipsis, no version.
    params = json.dumps({"stability": 0}, sort_keys=True, default=str)
    digest = hashlib.sha1((ACHHA + "\x1f" + params).encode("utf-8")).hexdigest()[:8]
    legacy = f"{VoicedCueLibrary.engine_key(ENGINE, 'hi-IN')}_ack_{digest}_p"
    (tmp_path / f"{legacy}.wav").write_bytes(pcm_to_wav_bytes(pcm(voiced(1000)), sample_rate=RATE))
    calls = []
    fresh = pcm(silence(150), voiced(420), silence(150))
    lib = VoicedCueLibrary(tmp_path, renderer=renderer([fresh], calls))
    assert lib.acknowledgement_clip(ENGINE, "hi-IN", ACHHA, RATE) == b""  # old sigh not reused
    for task in list(lib._tasks.values()):
        await task
    assert calls == ["अच्छा"]
    assert ms_of(lib.acknowledgement_clip(ENGINE, "hi-IN", ACHHA, RATE)) < 600


# ── the gate ─────────────────────────────────────────────────────────────

async def test_short_natural_ack_passes_on_the_first_take(tmp_path):
    calls = []
    take = pcm(silence(150), voiced(420), silence(150))
    lib = VoicedCueLibrary(tmp_path, renderer=renderer([take], calls))
    lib.acknowledgement_clip(ENGINE, "hi-IN", ACHHA, RATE)
    for task in list(lib._tasks.values()):
        await task
    clip = lib.acknowledgement_clip(ENGINE, "hi-IN", ACHHA, RATE)
    assert len(calls) == 1 and lib.rejected_takes == 0
    assert ms_of(clip) == pytest.approx(ms_of(trim_silence(take, RATE)), abs=1.0)  # trimmed, not cut
    key = lib._key(ENGINE, "hi-IN", "ack", ACHHA)
    assert lib.render_log[key]["attempts"] == 1 and (tmp_path / f"{key}.wav").is_file()


def test_overlong_voiced_ack_is_rejected_not_hard_cut():
    take = trim_silence(pcm(voiced(1800)), RATE)
    keep, reason = assess_take(take, RATE, "ack", synthesis_text("ack", LONG_LOOKUP))
    assert keep == 0 and "1400 ms ceiling" in reason
    clip, reason = VoicedCueLibrary._finish_take(take, RATE, "ack", synthesis_text("ack", LONG_LOOKUP))
    assert clip == b"" and "ceiling" in reason  # no truncated word is ever produced


def test_breath_tail_past_the_ceiling_is_trimmed_with_the_fade():
    # Speech ends at ~1330 ms; only breath continues past the 1400 ms ceiling.
    take = pcm(voiced(1330), breath(600, level=0.03))
    clip, reason = VoicedCueLibrary._finish_take(take, RATE, "ack", synthesis_text("ack", LONG_LOOKUP))
    assert reason is None
    assert ms_of(clip) == pytest.approx(1400.0, abs=0.1)
    tail = np.abs(np.frombuffer(clip, dtype="<i2")[-24:]).max()
    assert tail < 0.1 * np.abs(np.frombuffer(clip, dtype="<i2")).max()  # faded out, not clipped off


def test_whispered_drawn_out_and_hesitant_acks_are_rejected():
    spoken = synthesis_text("ack", ACHHA)
    whispered = trim_silence(pcm(silence(100), voiced(120), silence(60), breath(260, level=0.08), silence(100)), RATE)
    assert "whispered" in assess_take(whispered, RATE, "ack", spoken)[1]
    drawn_out = trim_silence(pcm(silence(100), voiced(900), silence(100)), RATE)
    assert "drawn out" in assess_take(drawn_out, RATE, "ack", spoken)[1]
    hesitant = trim_silence(pcm(voiced(250), silence(450), voiced(350)), RATE)
    assert "pause" in assess_take(hesitant, RATE, "ack", synthesis_text("ack", JI_THEEK_HAI))[1]
    # A breath too short to dominate the ending window, still a trailing sigh.
    sigh_tail = trim_silence(pcm(voiced(350), breath(170, level=0.06)), RATE)
    assert "trailing" in assess_take(sigh_tail, RATE, "ack", spoken)[1]
    natural = trim_silence(pcm(silence(100), voiced(200), silence(80), voiced(250), silence(100)), RATE)
    assert assess_take(natural, RATE, "ack", synthesis_text("ack", JI_THEEK_HAI))[1] is None


async def test_rejected_takes_are_retried_up_to_the_limit_then_the_cue_is_skipped(tmp_path):
    calls = []
    whispered = pcm(silence(100), breath(450, level=0.06), silence(100))
    lib = VoicedCueLibrary(tmp_path, renderer=renderer([whispered], calls))
    assert lib.acknowledgement_clip(ENGINE, "hi-IN", ACHHA, RATE) == b""
    for task in list(lib._tasks.values()):
        await task
    assert len(calls) == _MAX_RENDER_ATTEMPTS == 4
    assert lib.rejected_takes == 4 and lib.skipped_cues == 1
    key = lib._key(ENGINE, "hi-IN", "ack", ACHHA)
    assert not (tmp_path / f"{key}.wav").exists()  # nothing stored
    marker = json.loads((tmp_path / f"{key}.skip.json").read_text())
    assert marker["attempts"] == 4 and len(marker["rejections"]) == 4
    # Skipped for this voice: later turns and a restarted worker render nothing.
    assert lib.acknowledgement_clip(ENGINE, "hi-IN", ACHHA, RATE) == b""
    restarted = VoicedCueLibrary(tmp_path, renderer=renderer([whispered], calls))
    assert restarted.acknowledgement_clip(ENGINE, "hi-IN", ACHHA, RATE) == b""
    assert not restarted._tasks and len(calls) == 4


async def test_a_bad_first_take_is_replaced_by_a_good_retry(tmp_path):
    calls = []
    takes = [pcm(silence(100), breath(450, level=0.06), silence(100)),
             pcm(silence(150), voiced(420), silence(150))]
    lib = VoicedCueLibrary(tmp_path, renderer=renderer(takes, calls))
    lib.acknowledgement_clip(ENGINE, "hi-IN", ACHHA, RATE)
    for task in list(lib._tasks.values()):
        await task
    key = lib._key(ENGINE, "hi-IN", "ack", ACHHA)
    assert len(calls) == 2 and lib.render_log[key]["attempts"] == 2
    assert lib.acknowledgement_clip(ENGINE, "hi-IN", ACHHA, RATE)


# ── ladder cues: only the never-cut-voice rule is new ──────────────────────

def test_hmm_cap_never_cuts_voice_but_still_trims_a_breath_tail():
    assert "900 ms ceiling" in assess_take(trim_silence(pcm(voiced(1200)), RATE), RATE, "hmm")[1]
    clip, reason = VoicedCueLibrary._finish_take(pcm(voiced(820), breath(500)), RATE, "hmm")
    assert reason is None and ms_of(clip) == pytest.approx(900.0, abs=0.1)
    # A short cue is untouched by the gate (no acknowledgement checks apply).
    short = pcm(silence(100), breath(300, level=0.05), silence(100))
    assert VoicedCueLibrary._finish_take(short, RATE, "hmm")[1] is None


async def test_ladder_cue_text_is_rendered_unchanged(tmp_path):
    calls = []
    lib = VoicedCueLibrary(tmp_path, renderer=renderer([pcm(silence(100), voiced(300), silence(100))], calls))
    lib.clip(ENGINE, "hi-IN", "hmm", RATE)
    await lib.wait_ready(ENGINE, "hi-IN")
    assert calls == ["Hmm…"]


# ── the reply path is not the cue path ───────────────────────────────────

def test_reply_text_keeps_its_ellipsis():
    # StreamingTTSRouter applies sanitize_for_tts to reply text; only cue
    # renders go through synthesis_text.
    assert sanitize_for_tts(JI_THEEK_HAI) == JI_THEEK_HAI
    assert sanitize_for_tts("अच्छा… ठीक है, मैं देखता हूँ…") == "अच्छा… ठीक है, मैं देखता हूँ…"
