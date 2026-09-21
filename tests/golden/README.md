# Golden replays

Frozen tenant behaviour for the shared orchestration layer.

* `fixtures/definitions/<bot>.json` — read-only dumps of a bot's workflows,
  intents, api connections and context sample (dev control plane, 2026-09-18),
  plus `live_cv_7786bc42deca.json` (live Zepto MDND wf v15 + transcript).
* `cases/<bot>.json` — caller-turn scripts lifted from the tenant scenario
  runners (`python -m tests.golden.extract_scenarios`), `hand_*.json`
  hand-authored collections/demo flows, `live_*.json` a real call with its
  LLM-labelled signals. Each case carries the recorded `expected` outcome per
  turn: signal, question shape, hang-up, leading affirmation, route decision,
  node trace, reply, done/status, off-script flag, slots.
* `router_signals.json` — 1,253 utterances × router classifiers.

Run: `pytest tests/golden`. Re-record after a reviewed behaviour change:
`python -m tests.golden.record [--only <bot>]` and
`python -m tests.golden.build_router_corpus`; commit the diff with the code.
