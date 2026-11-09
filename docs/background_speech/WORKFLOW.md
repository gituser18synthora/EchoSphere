# Local replay and confirmed-label workflow

All commands below operate in the local workspace. No command applies settings or deploys code. Private recordings and raw exports stay under ignored `storage/tmp/background_replay`.

## Existing evidence

```bash
env/bin/python -m tests.background_replay
env/bin/python -m pytest tests/unit/test_background_replay.py tests/unit/test_background_opt_in.py tests/unit/test_background_review_import.py tests/unit/test_transcript_playout.py -q
env/bin/python -m scripts.validate_background_replay
env/bin/python -m scripts.shortlist_background_calls
```

The shortlist command reads the existing seven-day snapshot; it does not query the server or automatically include later controlled calls. Raw exports contain only the selected bot/tenant's recording metadata and relevant event types. Any refresh must remain read-only.

## Human review and controlled calls

The owner can supply IDs/scenarios in chat. A CSV is an optional local handoff format: `review_labels_template.csv` contains candidate ID, call ID and exact UTC event timestamp. Use `speaker_label=caller` or `background` only after explicit confirmation of that segment. Keep `unverified` or `unclear` otherwise. Set `scenario` to `normal`, `soft`, `speakerphone`, `continuous_background`, `call_start` or `unknown`, and identify the reviewer. A scenario alone never establishes speaker attribution.

```bash
env/bin/python -m scripts.import_background_review \
  --labels docs/background_speech/review_labels_template.csv \
  --calls storage/tmp/background_replay/seven_day_calls.json \
  --output storage/tmp/background_replay/confirmed_selection.json
```

With the current unverified template this intentionally produces zero confirmed selections. Missing reviewers, conflicting labels and timestamps without a unique measured segment are rejected. Missing acoustic instrumentation cannot be filled with invented measurements. Listening-only cases remain review evidence outside the acoustic replay metrics.

For controlled calls not present in the snapshot, first obtain a fresh read-only export containing those IDs, their relevant events and recording metadata. Pass that local export to `--calls`. Each approved selection can contain one segment index or an explicitly reviewed list of indices for a continuous window.

After nonempty confirmed selections exist, extract them into a separate manifest so the original frozen baseline remains intact:

```bash
env/bin/python -m scripts.build_background_replay_set \
  --calls storage/tmp/background_replay/seven_day_calls.json \
  --selection storage/tmp/background_replay/confirmed_selection.json \
  --manifest tests/fixtures/background_controlled_manifest.json \
  --fetch-readonly

env/bin/python -m tests.background_replay \
  --manifest tests/fixtures/background_controlled_manifest.json

env/bin/python -m scripts.validate_background_replay \
  --manifest tests/fixtures/background_controlled_manifest.json \
  --output docs/background_speech/controlled_validation
```

`--fetch-readonly` uses SCP to download only missing selected WAVs. It never uploads, edits or restarts anything on the demo server. Without that option, extraction needs the WAVs already available locally. Confirmed speakerphone metadata is attached only to explicitly reviewed caller segments with that scenario.

The validator computes confirmed background blocking and genuine overlap allowance/false-hold counts when the corresponding labels exist. It retains N/A for missing categories. Its synthetic controls remain explicitly separate from recorded, confirmed caller evidence. No result should be described as an end-to-end audio/STT or far-end playback test.
