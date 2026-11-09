# Background speech replay: dataset and baseline

No live settings or code were changed. Audio is kept in ignored local storage.
The 20 real clips come from six recent calls of the same bot. Only `incident_later` has user-confirmed background-speaker ground truth. Other labels are explicitly unverified review candidates. Speakerphone use is unknown; continuous windows are not confirmed continuous background speech.

This is recorded-event/acoustic-evidence replay through the current level and interruption policy. It is not a full audio/VAD/STT replay. Clip boundaries use event timestamps with padding. Observed and replayed interruptions can differ because the replay does not reconstruct the complete transport/turn-controller timeline.

| Clip | Label | Review | Classification | Replayed interruption | Observed interruption | Accepted as input |
|---|---|---|---|---|---|---|
| incident_later | background | user_confirmed | caller | True | True | True |
| incident_now_1 | background_candidate | unverified | background_suspect | False | False | True |
| incident_now_2 | background_candidate | unverified | background_suspect | False | False | True |
| incident_identity | caller_candidate | unverified | unknown | False | False | True |
| incident_yes | caller_candidate | unverified | caller | False | False | True |
| incident_tomorrow | caller_candidate | unverified | caller | False | False | True |
| incident_soft_ok | soft_caller_candidate | unverified | caller | False | False | True |
| recent_normal_1 | caller_candidate | unverified | caller | False | False | True |
| recent_normal_2 | caller_candidate | unverified | caller | False | False | True |
| start_incident | call_start_unknown | unverified | unknown | False | False | True |
| start_quiet | call_start_unknown | unverified | unknown | False | False | True |
| start_hello | call_start_unknown | unverified | unknown | False | False | True |
| start_latest | call_start_unknown | unverified | unknown | False | False | True |
| soft_recent_1 | soft_caller_candidate | unverified | background_suspect | False | False | True |
| soft_recent_2 | soft_caller_candidate | unverified | background_suspect | False | False | True |
| soft_overlap | soft_overlap_candidate | unverified | caller | False | False | True |
| overlap_words | caller_interruption_candidate | unverified | unknown | True | False | True |
| overlap_no | caller_interruption_candidate | unverified | unknown | True | True | True |
| continuous_review_1 | continuous_speech_review | unverified | unknown | True | True | True |
| continuous_review_2 | continuous_speech_review | unverified | background_suspect/caller | False | False | True |

## Coverage gaps

- Confirmed soft-caller and speakerphone interruption labels are pending owner review.
- Continuous windows need speaker and continuity annotation.
- `incident_now_1` and `incident_now_2` are suspected background; they are excluded from confirmed-ground-truth metrics.
- One confirmed negative cannot establish a safe production threshold or recall for genuine callers.

## Reproduction

Run `env/bin/python -m tests.background_replay` and `env/bin/python -m pytest tests/unit/test_background_replay.py`. Private WAV integrity checks skip when assets are absent; policy replay still runs.
The extraction helper reads an existing local `calls.json` export and can fetch selected recordings read-only with `--fetch-readonly`. No script applies settings or deploys code.
