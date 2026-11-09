"""Replay-set integrity and regression checks; no network or real databases."""
import hashlib
from pathlib import Path
import wave

import pytest

from tests.background_replay import load_clips, replay_segment


@pytest.mark.parametrize("clip", load_clips(), ids=lambda c: c["id"])
async def test_recorded_evidence_is_replayable(clip):
    for segment in clip["segments"]:
        result = await replay_segment(segment)
        assert result["classification"] in {"caller", "background_suspect", "unknown"}
        assert result["accepted"]  # legacy guard is off, including suspects


@pytest.mark.parametrize("clip", load_clips(), ids=lambda c: c["id"])
def test_local_audio_matches_review_manifest(clip):
    path = Path("storage/tmp/background_replay") / clip["file"]
    if not path.is_file():
        pytest.skip("Private local recording asset absent; evidence replay still runs")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == clip["sha256"]
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 2
        assert wav.getframerate() == clip["sample_rate"]
        assert wav.getnframes() > 0


def test_unverified_labels_are_not_promoted_to_speakerphone_ground_truth():
    clips = load_clips()
    assert len(clips) == 20
    assert len({c["id"] for c in clips}) == 20
    assert all(c["device"] == "unknown" and not c["speakerphone_verified"] for c in clips)
    assert next(c for c in clips if c["id"] == "incident_later")["review"] == "user_confirmed"
