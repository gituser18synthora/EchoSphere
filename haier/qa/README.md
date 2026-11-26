# Haier rejection bot — regression suite (`bot_450a4f7b4832`)

Re-run this after every prompt, workflow or code change, on local and on demo.
Nothing here edits the bot, its prompt, its workflow or any server code.

| File | What it is |
|---|---|
| `haier_test_cases.json` | The test case set: the 7 payloads, ~90 cases (agree / dispute / no contact / timing / consent / identity / busy / language / audio behaviour), expectations, and the real call IDs each case came from. |
| `export_config.py` | Read-only export of an environment's effective config (published release, pinned workflow, prompt). |
| `run_text_cases.py` | Text-level runner: real voice brain + real LLM + the exported workflow/prompt. Fast, repeatable, no audio. |
| `haier_hub_cases.json` | Step-level cases: one workflow question + one caller reply (222 cases: real replies of the 2026-11-25 DEMO calls, the permission rule, and replies not seen in those calls for every kind of answer). |
| `run_hub_cases.py` | Step-level runner: pauses a real call at one question, plays one reply through the real brain + decision model + engine, records where the flow went. `--compare BEFORE AFTER` prints a side-by-side table. |
| `haier_call_replays_2026-11-25.json` | The caller turns of the 65 DEMO phone calls of 2026-11-25 that had caller speech, as conversation cases for `run_text_cases.py --cases`. |
| `run_voice_cases.py` | Audio-level runner over the FreeSWITCH `mod_audio_fork` websocket protocol: real STT, endpointing, barge-in, TTS. No number is dialled. |
| `haier_voice_cases_phase1.json` | Audio-level cases for the 2026-11-25 review: greeting cut by a pickup word, hello / silence, answers before or over the question, short answers, the call ending after the closing line, speech after the closing, a farewell cut by an acknowledgement. Run with `run_voice_cases.py --cases`. |
| `local_voice_worker.py` | Starts a LOCAL voice worker on a spare port from the code tree in the working directory, optionally with the turn-detection settings of an exported snapshot (for example DEMO's) for that process only. Nothing is written to a database. |
| `create_session.py` / `fetch_sessions.py` | Helpers used by the voice runner (mint one test session; read the finished transcript/checkpoint). |
| `summarize_results.py` | Builds the reason × scenario pass/fail matrix from result files. |

## Prerequisites

- Run from the repo root with `env/bin/python -B`.
- Local voice runs need the local voice worker on `127.0.0.1:9002` and the local bot published.
- Demo runs need `ssh echosphere-live` and network access to `192.168.60.123:9011` (telephony gateway).
- Check `curl localhost:9002/health` (local) and `ssh echosphere-live 'curl -s localhost:9011/health'` (demo):
  `active_sessions` should be 0 or low. Voice cases run one call at a time.

## 1. Export the configuration you want to test

```bash
mkdir -p storage/tmp/haier_qa
env/bin/python -B haier/qa/export_config.py local > storage/tmp/haier_qa/snapshot_local.raw
ssh echosphere-live 'cd /var/www/html/EchoSphere && PYTHONDONTWRITEBYTECODE=1 env/bin/python - demo' \
    < haier/qa/export_config.py > storage/tmp/haier_qa/snapshot_demo.raw
```

To run demo's *code* (not your working tree) for the text runner, export the deployed commit:

```bash
git -C . rev-parse HEAD; ssh echosphere-live 'cd /var/www/html/EchoSphere && git rev-parse --short HEAD'
mkdir -p storage/tmp/haier_qa/demo_code && git archive <demo-commit> | tar -x -C storage/tmp/haier_qa/demo_code
ln -sf "$PWD/.env" storage/tmp/haier_qa/demo_code/.env
```

## 2. Text-level run (all reasons, all non-audio cases)

```bash
env/bin/python -B haier/qa/run_text_cases.py --snapshot storage/tmp/haier_qa/snapshot_local.raw \
    --output storage/tmp/haier_qa/text_local.json --transcripts storage/tmp/haier_qa/text_local.txt
env/bin/python -B haier/qa/run_text_cases.py --code-root storage/tmp/haier_qa/demo_code \
    --snapshot storage/tmp/haier_qa/snapshot_demo.raw \
    --output storage/tmp/haier_qa/text_demo.json --transcripts storage/tmp/haier_qa/text_demo.txt
```

Useful flags: `--only BILL-04,UOK-03` (subset), `--repeat 5` (override repeats), `--concurrency 4`,
`--decision-timeout 8` (parallel runs slow the decision model down; a decision that exceeds the bot's
own budget falls back to the authored labels exactly as on a live call, which hides what the model
would have understood. Use the relaxed budget to judge understanding and the default to judge the
live budget).

Step-level cases and the replay of a day's calls:

```bash
env/bin/python -B haier/qa/run_hub_cases.py --snapshot storage/tmp/haier_qa/snapshot_local.raw \
    --output storage/tmp/haier_qa/hub_local.json --decision-timeout 8
env/bin/python -B haier/qa/run_hub_cases.py --compare storage/tmp/haier_qa/hub_demo.json storage/tmp/haier_qa/hub_local.json
env/bin/python -B haier/qa/run_text_cases.py --snapshot storage/tmp/haier_qa/snapshot_local.raw \
    --cases haier/qa/haier_call_replays_2026-11-25.json --output storage/tmp/haier_qa/calls_local.json \
    --transcripts storage/tmp/haier_qa/calls_local.txt --decision-timeout 8
```
Each case runs `repeat` times (P0 cases 3×) because the decision model is not deterministic.

## 3. Audio-level run (telephony path)

```bash
env/bin/python -B haier/qa/run_voice_cases.py --env local --output storage/tmp/haier_qa/voice_local.json
env/bin/python -B haier/qa/run_voice_cases.py --env demo  --output storage/tmp/haier_qa/voice_demo.json
```

A full voice run is ~100 calls and takes ~2.5 hours per environment; use `--only` for quick checks
and `--resume` to continue an interrupted run. To score calls that already ran (for example after
stopping a run) without placing new ones:

```bash
env/bin/python -B haier/qa/run_voice_cases.py --env demo --evaluate-only storage/tmp/haier_qa/voice_demo.json \
    --output storage/tmp/haier_qa/voice_demo.json
```

Audio behaviour depends on the tenant's turn-detection settings, and LOCAL and DEMO differ (on
2026-11-26 DEMO ran the background-speech guard, a 0.6 s endpoint and no ducking; LOCAL did not). To
judge a change for DEMO, run the worker with DEMO's settings and compare the old code with the new:

```bash
# new code (working tree) and, in another shell, the deployed commit from an archive
env/bin/python -B haier/qa/local_voice_worker.py --port 9103 --turn-detection-from storage/tmp/haier_qa/snapshot_demo.raw
(cd storage/tmp/haier_qa/demo_code && "$OLDPWD/env/bin/python" -B "$OLDPWD/haier/qa/local_voice_worker.py" \
    --port 9102 --turn-detection-from "$OLDPWD/storage/tmp/haier_qa/snapshot_demo.raw")
env/bin/python -B haier/qa/run_voice_cases.py --env local --port 9103 --cases haier/qa/haier_voice_cases_phase1.json \
    --output storage/tmp/haier_qa/voice_new.json
```

A worker that was started before the code changed serves a mix of old and new modules: restart it, or
use the launcher above. Turn timing in a case: `after` = `bot_done` (1.5 s after the bot fell silent),
`bot_start` + `delay`, `bot_end` + `delay` (needs `min_speech_s`, see the runner's docstring) or
`immediate`; `gain_db` makes a quiet caller. Audio-level expectations: `closes_within_s`, `stays_open`,
`events_any` / `events_all` / `events_none`, `user_turns_all`, `say_count_min` / `say_count_max`,
`say_before`, `max_gap_after`.

Run one call at a time on a dev machine: two parallel calls made local workflow steps 5–10× slower. Each call leaves a stereo WAV
(`storage/tmp/haier_qa/voice_<env>/`, left = caller, right = bot as played).
Test sessions are ordinary conversations of the bot in that environment: channel `freeswitch`,
no caller number, no call id (so no ESL command reaches the real switch).

## 4. Matrix

```bash
env/bin/python -B haier/qa/summarize_results.py --reevaluate \
    --text local=storage/tmp/haier_qa/text_local.json --text demo=storage/tmp/haier_qa/text_demo.json \
    --voice local=storage/tmp/haier_qa/voice_local.json --voice demo=storage/tmp/haier_qa/voice_demo.json \
    --out storage/tmp/haier_qa/matrix.md
```

## Writing new cases

Add an object to `cases` in `haier_test_cases.json`:

```json
{"id": "RV-08", "reason": "customer_refused_visit", "category": "dispute", "priority": "P1", "repeat": 2,
 "modes": ["text", "voice"], "title": "...", "patterns": ["P1"], "source_calls": ["cv_..."],
 "turns": [{"say": "हाँ बोलिए।"}, {"say": "...", "after": "bot_start", "delay": 2.0}, {"silence": 8}],
 "expect": {"outcome_in": ["redispatch"], "say_none": ["confirm नहीं कर पाई"], "max_identity_asks": 0}}
```

Write utterances the way Sarvam STT returns them (Devanagari Hindi; English sentences in Latin script).
Never put a real customer's name, number or address in a case.

Expectation keys: `outcome_in`, `outcome_not_in`, `must_close`, `open_ok`, `say_any`, `say_all`, `say_none`
(regex over bot replies after the greeting), `max_identity_asks`, `intro_has`, `max_intro_repeats`,
`max_same_question`, `max_bot_turns`, `nodes_none`, `never_outcome` (outcome recorded at any turn,
e.g. at the closing before a post-closing turn blanked it), `max_dead_air_s` (voice only).
The voice runner also fails a call whose saved conversation record lost the workflow's outcome.

Limitations: the text runner cannot judge STT, timing, barge-in, silence prompts or language switching
(no STT language tag). The voice runner uses clean TTS caller audio, so real phone lines will be
noisier than these runs.
