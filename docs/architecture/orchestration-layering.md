# Orchestration layering — where behaviour lives

Status: implemented locally 2026-09-18 (Phases 0–6 of the workflow-architecture hardening).

## Why

Every live incident used to be fixed by editing `shared/orchestration/router.py`
or `shared/orchestration/workflow_engine.py`. Those files ran for every tenant,
so a fix for Zepto could silently change mPokket, Honasa or OYO. The engine also
carried one tenant's node ids, one domain's vocabulary and three languages'
word lists.

## Layers

| Layer | Module(s) | Owns | Must never contain |
|---|---|---|---|
| **Core engine** | `workflow_engine.py`, `ask_resolution.py`, `workflow_state.py`, `behavior.py` | graph walk, checkpoints, retries, audit, off-script contract, the ask pipeline order | tenant names, node ids, slot names, caller-language words, domain vocabulary |
| **Core router** | `router.py` | route priority, intent matching, knowledge detection, call control composition | any regex literal in a caller language |
| **Behaviour versions** | `behavior.py` | frozen engine semantics per definition (`behavior.version` on the start node) | — |
| **Language packs** | `lang/{hi,en,ml,ta}.py` | surface forms: yes/no words, fillers, question markers, hang-up/DNC/emergency phrases, digit phrases, per-signal fragments | meanings |
| **Signal packs** | `signals/{core,collections,insurance}.py` | meanings (`refusal`, `hardship`…) with engine flags (entry, literal fallback, off-script, yes/no) and KB vocabulary | language forms other than the Hinglish/English baseline |
| **Extensions** | `extensions/{zepto_mdnd,mpokket,reference_flows}` | tenant graph builders, semantic-slot providers, custom ask resolvers — registered by NAME via `extensions/manifest.py` | — |
| **Workflow definition** | DB `workflows.nodes/edges` (+ `workflow_revisions` snapshots) | nodes, edges, synonyms, patterns, unmatched replies, `behavior`, `semanticSlots`, `semanticRole`, `askResolvers` | — |
| **Release** | `releases.pinned_workflows` | which workflow revision a published bot executes | — |

The guard test `tests/unit/test_core_purity.py` fails the build when a core
module gains an Indic-script literal, a tenant token, a workflow node/slot id,
or an import of a tenant extension.

## Behaviour versioning

* Undeclared definitions run **version 1** = live semantics as of 2026-09-17.
* A new definition saved through the API is stamped with the latest version on
  its start node (`config.behavior.version`). Existing definitions are never
  re-stamped automatically.
* Knobs (`question_label_yields_free_text`, `literal_answer_min_words`,
  `max_ask_retries`, `max_lookahead_hubs`, `max_node_steps`,
  `question_label_yields_hub`) may be overridden per definition.
* Adding a behaviour change = new version number in `behavior.py`, new defaults
  under that number, goldens re-recorded ONLY for definitions that opt in.

## Release pinning

* Every workflow save writes a `workflow_revisions` snapshot (once migration
  `a1b2c3d4e5f6` is applied; feature-detected at runtime).
* Publishing a release freezes `{workflow_id: version}` on the release.
* The runtime executes the pinned revision when it differs from the latest save
  and the snapshot exists; otherwise the latest save runs and the turn result
  carries `pinMissing`. Testing Studio always runs the latest save.

## Golden replays

`tests/golden/` freezes 182 engine cases (715 caller turns over 19 bots) and a
1,253-utterance router corpus, recorded from the dev control plane
(read-only). Run `pytest tests/golden` before and after any change under
`shared/orchestration`. Re-record only for a reviewed behaviour change:
`python -m tests.golden.record` and commit the case diff with the code.

## Decision rule for a shared-orchestration change

> Change core orchestration code only when a replay shows the engine producing
> the wrong outcome for a definition *as authored* under its behaviour version,
> and the fix contains no tenant vocabulary, node id, slot name or
> caller-language literal. If other tenants' goldens change, the fix ships
> behind a new behaviour version. Everything else is a definition, language
> pack, signal pack, extension or prompt change.

Checklist before merging a core change:
1. Reproduce with `tests/golden/harness.py` (or a new case), not on live.
2. At least two unrelated tenants would want the change.
3. `pytest tests/unit/test_core_purity.py tests/golden` green; goldens diff is
   empty or explicitly re-recorded with a behaviour version.
4. Add the incident as a golden case.

## Case A vs Case B (examples)

| Incident | Layer | Where it lives now |
|---|---|---|
| Hinglish "Are"/"do"/"is" read as English question auxiliaries | language pack | `lang/en.py` question aux + subject rule |
| Statement labelled `question` at a free-text ask should be the answer | behaviour v2 | `behavior.py`, opted in by Zepto MDND (`zepto/setup/06_single_bots.py`) |
| Demo Bot bare "busy" edge token | definition | workflow edge label |
| "X ke bajaye Y" amount contrast | definition | node `regexPatterns` |
| MDND four-fact semantics, verify-hub summary | extension | `extensions/zepto_mdnd` (`semanticSlots: mdnd_v1`, `semanticRole`) |
| mPokket MOP flow script | extension | `extensions/mpokket` |
| hardship / already_paid / payment_intent / "not my loan" | domain signal pack | `signals/collections.py` |
| Malayalam / Tamil yes-no, hardship, payment forms | language packs | `lang/ml.py`, `lang/ta.py` |
| Devanagari `\b` tokenization | core | `_is_word_char` |
