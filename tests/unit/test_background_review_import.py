"""Human attribution must never be inferred from amplitude or a call scenario."""
import pytest

from scripts.import_background_review import selections


CALLS = [{"control_plane_id": "cv_test", "events": [
    {"kind": "caller_level_segment", "at": "2026-11-09T06:00:00+00:00"},
    {"kind": "caller_level_segment", "at": "2026-11-09T06:00:01+00:00"},
]}]


def row():
    return dict(call_id="cv_test", event_utc="2026-11-09T06:00:00Z",
                speaker_label="caller", scenario="speakerphone", reviewer="Owner")


def test_call_scenario_does_not_confirm_unreviewed_segments():
    value = row(); value["speaker_label"] = "unverified"
    assert selections([value], CALLS) == []
    value["speaker_label"] = "unclear"
    assert selections([value], CALLS) == []


def test_confirmation_is_exactly_one_measured_segment_and_explicit_device():
    selection, = selections([row()], CALLS)
    assert selection["indices"] == 0
    assert selection["label"] == "caller" and selection["review"] == "human_confirmed"
    assert selection["speakerphone_verified"] and selection["device"] == "speakerphone"


def test_missing_review_and_conflicting_labels_are_rejected():
    value = row(); value["reviewer"] = ""
    with pytest.raises(ValueError, match="reviewer"):
        selections([value], CALLS)
    with pytest.raises(ValueError, match="Duplicate"):
        selections([row(), row()], CALLS)


def test_unmatched_interruption_cannot_invent_acoustic_evidence():
    value = row(); value["event_utc"] = "2026-11-09T07:00:00Z"
    with pytest.raises(ValueError, match="unique measured"):
        selections([value], CALLS)
