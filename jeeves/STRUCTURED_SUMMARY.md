# Jeeves Structured Summary

Applies to `bot_44a50e8c4146` (tenant Jeeves). Everything described here lives in
the LOCAL workspace and the LOCAL database; nothing was deployed, published or
changed on DEMO LIVE / NEW PRODUCTION.

## How a call's structured summary is produced

1. The call flow (`wf_9685ed07e312`, stage 8 of `jeeves/setup/01_structured_summary.py`)
   writes its final slots; `voice_runtime/brain.py` snapshots them onto the
   recorder and `voice_runtime/recording.py` persists the transcript and queues a
   `conversation_memories` row.
2. The post-call worker (`shared/post_call/processor.py`, embedded in the voice
   worker and the telephony gateway) loads the bot's `goal_policy.summaryFields`,
   sends ONE narrative analysis request (summary, outcome, commitments, next best
   action, slot-only fields) and — for a bot with evidence-required fields — ONE
   focused evidence request (`shared/post_call/analyzer.py::extract_evidence_fields`)
   that carries only those fields' rules and the whole transcript, numbered
   `[turn N]`, and answers with a value plus the supporting Customer turn number
   per field.
3. `shared/post_call/structured.py::merge_structured_fields` keeps workflow slots
   authoritative for the eight slot fields, validates each analyst value against
   the field's vocabulary, requires a cited customer turn, checks the cited turn
   against the field's `topicWords` (and, for a `date`, that the date words share a
   clause with a topic word and that a relative "kal"/"parso" was resolved in the
   direction its tense implies), fills the derived Yes/No fields from their status
   field, and applies `requires`.
4. The validated values, their provenance and the cited customer text persist in
   `conversation_memories.memory` (`structured_fields`, `structured_field_sources`,
   `structured_field_evidence`); `backend/serializers.py` exposes them as
   `structuredFields` / `structuredFieldSources` / `structuredFieldEvidence` /
   `structuredFieldDisplay` on `GET /api/v1/conversations/{id}`; the Conversations
   drawer (`src/pages/tenant/Conversations.tsx`) renders every configured field,
   the quote under its label, and "N of 15 determined".

If the evidence request fails (timeout, no JSON) the row is retried like a failed
analysis and, after the attempts are exhausted, the deterministic fallback keeps
only the workflow fields — the analyst fields are never silently persisted as
"not determined" on a successful narrative.

The Testing Studio preview (`backend/routers/testing.py::_structured_summary_preview`)
is slot-only by design: the seven analyst fields are determined only by the
post-call worker, so they never show in the simulator.

## Fields

The eight workflow fields (`address_verified`, `appointment_booked` "Pickup
confirmed", `appointment_date`, `appointment_time`, `call_result`, `callback_date`,
`callback_time`, `callback_preference`) are unchanged and slot-only.

`jeeves/summary_intents.py` owns the seven analyst fields:

| Key | Values and meaning |
| --- | --- |
| `already_picked_up` | Yes when the customer states the device was already collected / handed over; No when they explicitly say it has not been collected yet (nobody came, still with them); null otherwise — a booking alone never settles it. Never Yes without an explicit statement. |
| `address_update_status` | `requested` (registered address wrong / moved / asks for a change, with or without the new address), `reported_updated` (customer says the change is already done — a claim, not verification), `not_requested` (a change was discussed and then declined/retracted), null for a plain confirmation of the registered address (that is `address_verified`'s job). |
| `address_change_requested` | Derived: requested → Yes; reported_updated / not_requested → No; null otherwise. |
| `updated_address` | The replacement address the customer spoke (a place: area/road/building/flat/city/PIN), partial allowed, original script, up to 1,000 characters; null when no place was named — words about the address ("naya address hi sahi hai") are not an address. Requires status requested or reported_updated. |
| `address_update_date` | ISO date explicitly tied to the address change ("maine kal address update kar diya tha" → yesterday; "kal address update karna" → tomorrow), resolved in Asia/Kolkata; null when the date words belong to another event (pickup day, callback, handover) — enforced deterministically by `topicWords` — and dropped when the analyst resolved a "kal"/"parso" against the tense of its clause (`shared/post_call/dates.py::relative_day_offset`, checked in `merge_structured_fields` with the call's reference time). Requires status requested or reported_updated. |
| `refund_status` | `requested` (demand for money back), `status_inquiry` (asks/complains about an outstanding refund), `reported_refunded` (says the money arrived), `withdrawn` (cancels/declines), null (no mention, unrelated payment, conditional policy question, bot-only offer). |
| `refund_requested` | Derived: requested / status_inquiry → Yes ("outstanding"); reported_refunded / withdrawn → No; null otherwise. |

`topicWords` (a `SummaryFieldSpec` option, `shared/orchestration/goal_engine.py`):
the cited customer turn must mention one of the words (device/router/pickup/…
for the device, address/pata/… for the address, refund/paise/… for the refund),
so a value the analyst hangs on a turn about something else ("not collected"
cited on a refund line) is dropped; for a `date` field the date words must sit in
the same clause as a topic word.

## Why a separate evidence request

Codex's first version folded the seven fields, their rules and turn numbers into
the single narrative request. Measured on 15 simulated Jeeves calls (the real v21
flow, the remark made in passing, the real `gpt-4o-mini` analyst) that prompt
missed in-flow statements ("mera refund kab milega?" asked before answering the
bot → null; "abhi tak koi pickup karne nahi aaya" → null) and invented an
address-update date from "last week" said about the router handover. The same
rules in a focused request settled those, and the deterministic clause guard
removed the invented dates. Rewording alone (two rule rewrites) failed in
different places each time; the structure was the problem, not the wording.

Known judgment calls of the model (never a false Yes / never a false "requested"):
a conditional refund question on an already-disconnected line ("agar cancel karu
to refund milega kya?") is read as `status_inquiry`; a booking or a promise to hand
the device over tomorrow is occasionally read as `already_picked_up` = No (the
device is indeed still with the customer).

## Local configuration

```bash
env/bin/python jeeves/setup/03_summary_intents.py            # dry run against local (:9001)
env/bin/python jeeves/setup/03_summary_intents.py --apply    # write through the voice-settings API
env/bin/python jeeves/setup/03_summary_intents.py --dump out.json   # offline policy patch
```

Env: `ECHOSPHERE_API_BASE`, `JEEVES_SETUP_EMAIL` / `JEEVES_SETUP_PASSWORD` (a
Jeeves tenant admin or a super admin). Idempotent; the eight workflow fields are
never touched. Applied to LOCAL on 2026-10-28 (backups in
`jeeves/backups/local-2026-10-28-summary-v3/`). DEMO LIVE / NEW PRODUCTION: not
applied — they need this code deployed (api + voice + gateway restart) before the
config, since an older runtime neither sends the evidence request nor knows
`topicWords`.

Local caveat (2026-10-28): the locally running API (:9001) and voice worker
(:9002) were started before this code and before Codex's change, so they still
analyze calls with the old code; and the local MySQL is missing
`runtime_context_schemas.identity_confirmation_mode` (migration `4d8b2f6a9c1e`),
which the current code reads at call start and in the Studio simulator. Run
`env/bin/python -m backend.cli migrate` before restarting those processes on
the current code.

## Verification

```bash
env/bin/python -m pytest tests/unit/test_jeeves_summary_intents.py -q        # deterministic contracts
ECHOSPHERE_RUN_SUMMARY_EVAL=1 env/bin/python -m pytest tests/evals/test_jeeves_summary_intents.py -q
```

Deterministic tests: evidence attribution and rejection, topic-word and
date-clause guards, derived fields, `requires`, long addresses, workflow
precedence, the narrative prompt omitting the evidence fields, the evidence
prompt carrying the rules and the local calendar date, the worker running both
requests and persisting fields + evidence, the worker requeueing when the
evidence request fails, and the API serializer.

Opt-in semantic evals (real provider, synthetic data, nothing persisted):
`tests/fixtures/jeeves_summary_intents.json` (49 short cases) and
`tests/fixtures/jeeves_summary_calls.json` (15 whole simulated calls with their
final workflow slots). Cases where two readings are defensible list both under
`allowed` with a note.

End-to-end on 2026-10-28 (local): 15 simulated calls driven through the real
`WorkflowEngine` (v21), recorded with the real `SessionRecorder`, processed by
`process_memory_row` with the live analyst, read back through
`GET /api/v1/conversations/{id}` and rendered through the real Conversations page
component — every expected field, source and quote present; 7–11 s per call for
the two requests. Browser-level check of the tenant page still needs a Jeeves
tenant login.
