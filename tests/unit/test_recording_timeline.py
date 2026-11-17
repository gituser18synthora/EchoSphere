"""Recording sample-zero metadata and legacy API timeline compatibility."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.core.transcripts import recording_descriptor, ui_turns
from voice_runtime.recording import AlignedStereoRecorder, CallRecordingWriter, SessionRecorder, TurnRecord


def test_audio_origin_tracks_retiming_without_changing_samples(monkeypatch):
    monkeypatch.setattr("voice_runtime.recording.time.time", lambda: 1_800_000_000.0)
    monkeypatch.setattr("voice_runtime.recording.time.monotonic", lambda: 100.0)
    aligner = AlignedStereoRecorder(sample_rate=8000)
    assert aligner.started_at is None
    aligner.add_user_audio(b"\x01\x00" * 160, now=100.3)
    assert aligner.started_at == pytest.approx(1_800_000_000.28)
    # A delayed first frame is corrected when the held-back frames arrive.
    aligner.add_user_audio(b"\x01\x00" * 160, now=100.3)
    assert aligner.started_at == pytest.approx(1_800_000_000.26)


@pytest.mark.asyncio
async def test_writer_passes_audio_origin_to_saved_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("shared.config.get_settings", lambda: SimpleNamespace(voice_recordings_dir=str(tmp_path)))
    recorder = SimpleNamespace(config=SimpleNamespace(tenant_id="tn_test"), session_id="vs_timeline", set_recording=Mock())
    writer = CallRecordingWriter(recorder)
    writer.audiobuffer = SimpleNamespace(started_at=1_800_000_000.125)
    await writer.append(b"\x00\x00" * 160, 8000, 1)
    await writer.finalize()
    assert recorder.set_recording.call_args.kwargs["started_at"] == 1_800_000_000.125
    assert recorder.set_recording.call_args.kwargs["duration_sec"] == .02
    # No constructor/state is needed to verify this independent metadata setter.
    session = SessionRecorder.__new__(SessionRecorder)
    session.set_recording(**recorder.set_recording.call_args.kwargs)
    assert session.recording_info["startedAt"] == "2027-01-15T08:00:00.125Z"


@pytest.mark.parametrize("audio_start, expected, source", [
    ("2026-11-17T15:30:10+05:30", "2026-11-17T10:00:10.000Z", "recording"),
    (None, "2026-11-17T10:00:05.000Z", "session"),
    ("invalid", "2026-11-17T10:00:05.000Z", "session"),
])
def test_descriptor_uses_audio_start_then_legacy_document_start(tmp_path, monkeypatch, audio_start, expected, source):
    wav = tmp_path / "test.wav"
    wav.write_bytes(b"audio")
    monkeypatch.setattr("backend.core.transcripts.resolve_recording_path", lambda _: wav)
    conversation = SimpleNamespace(id="cv_test", started_at=datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc))
    doc = {"started_at": datetime(2026, 11, 17, 10, 0, 5),
           "recording": {"path": "test.wav", "durationSec": 300, "startedAt": audio_start}}
    descriptor = recording_descriptor(conversation, doc)
    assert descriptor["startedAt"] == expected
    assert descriptor["timingSource"] == source
    del doc["started_at"]
    if source == "session":
        assert recording_descriptor(conversation, doc)["startedAt"] == "2026-11-17T10:00:00.000Z"


def test_transcript_preserves_completion_time_and_saves_separate_speech_start():
    session = SessionRecorder("vs_timing", SimpleNamespace(language="en-IN"))
    session.add_turn(TurnRecord(role="user", text="Hello", timestamp=1_800_000_020, audio_started_at=1_800_000_011))
    stored = session.transcript_rows()
    assert stored[0]["ts"] == 1_800_000_020
    assert stored[0]["audioStartTs"] == 1_800_000_011
    mapped = ui_turns(stored)[0]
    assert mapped["at"] == "2027-01-15T08:00:20.000Z"
    assert mapped["audioStartedAt"] == "2027-01-15T08:00:11.000Z"


def test_legacy_turn_timing_events_are_joined_read_only_by_dispatch():
    turns = [{"role": "user", "text": "First", "ts": 1_800_000_020.02},
             {"role": "user", "text": "Second", "ts": 1_800_000_045.01}]
    events = [{"kind": "turn_timing", "turn_dispatched_at": 1_800_000_020_000, "user_speech_start_at": 1_800_000_011_000},
              {"kind": "turn_timing", "turn_dispatched_at": 1_800_000_045_000, "user_speech_start_at": 1_800_000_035_000}]
    mapped = ui_turns(turns, list(reversed(events)))
    assert mapped[0]["audioStartedAt"] == "2027-01-15T08:00:11.000Z"
    assert mapped[1]["audioStartedAt"] == "2027-01-15T08:00:35.000Z"
    assert "audioStartTs" not in turns[0]  # No database backfill/mutation.
    ambiguous = ui_turns([turns[0], {**turns[0], "ts": 1_800_000_020.04}], events)
    assert all("audioStartedAt" not in row for row in ambiguous)


def test_bot_audio_end_fallback_never_uses_a_later_reply():
    turns = [{"role": "bot", "text": "Question", "ts": 1_800_000_002},
             {"role": "user", "text": "Answer", "ts": 1_800_000_020}]
    events = [{"kind": "bot_audio_end", "audio_end_ts": 1_800_000_008},
              {"kind": "bot_audio_end", "audio_end_ts": 1_800_000_024}]
    caller = ui_turns(turns, events)[1]
    assert caller["playbackAt"] == "2027-01-15T08:00:08.000Z"
    assert caller["at"] == "2027-01-15T08:00:20.000Z"
    turns[1]["audioStartTs"] = 1_800_000_006
    caller = ui_turns(turns, events)[1]
    assert caller["audioStartedAt"] == "2027-01-15T08:00:06.000Z"
    assert "playbackAt" not in caller
