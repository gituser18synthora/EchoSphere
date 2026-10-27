# Jeeves Structured Summary

Applies to `bot_44a50e8c4146`. All work in this change is local. No live code,
workflow, service, deployment or historical conversation is changed.

## Architecture inspected

- `jeeves/setup/01_structured_summary.py` defines the eight existing fields and
  workflow bookkeeping. These remain unchanged and slot-only.
- `voice_runtime/brain.py` snapshots final corrected workflow slots;
  `voice_runtime/recording.py` persists the transcript and enqueues analysis.
- `shared/post_call/processor.py` loads the bot's goal policy and transcript,
  invokes the analyst, reconciles fields, and saves `PostCallAnalysis.memory_payload()`
  in `conversation_memories.memory` (JSON). No SQL migration is required.
- `shared/post_call/analyzer.py` builds the multilingual analyst prompt from
  configured `summaryFields`; `schema.py` validates and bounds its response;
  `structured.py` applies field vocabularies, workflow precedence and dependencies.
- `backend/routers/conversations.py` supplies configured field labels/order;
  `backend/serializers.py` exposes values, provenance, date display and evidence.
- `src/pages/tenant/Conversations.tsx` displays every stored field dynamically.
  Supporting customer quotes are visible beneath its label. Long addresses wrap.
- `shared/post_call/recall.py` loads the same validated memory, with its existing
  narrative/facts prompt projection. The Testing Studio preview remains slot-only;
  analyst-only fields are undetermined until post-call analysis runs.

## New fields

| Key | Values and meaning |
| --- | --- |
| `already_picked_up` | Yes for customer-confirmed completed device collection/handover; No for explicit denial/continued possession; null when unknown, including future pickup discussion alone. |
| `address_update_status` | `requested`, `reported_updated`, `not_requested`, or null. Incorrect registered address counts as requested; retracted changes count as not requested. |
| `address_change_requested` | Computed from status: Yes for requested; No for reported completion, withdrawal or explicit no change; null without a settled status. |
| `updated_address` | Customer's final complete or partial replacement address, up to 1,000 characters; null without one. No missing components are filled. |
| `address_update_date` | Explicitly associated request/update date in ISO format; relative dates use call time in Asia/Kolkata and tense; null if unreliable or absent. |
| `refund_status` | `requested`, `status_inquiry`, `reported_refunded`, `withdrawn`, or null. |
| `refund_requested` | Computed from status: Yes for a demand or outstanding status inquiry; No for reported receipt/withdrawal; null when no refund intent is established. |

Yes/No/null preserves the existing API/UI contract instead of mixing JSON booleans
with string fields. `reported_updated` and `reported_refunded` are customer claims,
not backend verification. Status-derived booleans cannot contradict their status.

## Extraction and validation

`jeeves/summary_intents.py` owns bot-specific instructions and examples. The
shared engine has optional field settings with backwards-compatible defaults:

- `extractionInstructions`: detailed context, negation/correction and multilingual rules.
- `evidenceRequired`: the analyst cites a numbered customer turn; the backend
  copies its actual text into the validated memory. Bot references, invalid
  indices, invented quotes, or missing evidence fail closed. Legacy verbatim
  quote output is also validated. This checks attribution; semantic correctness
  remains model-dependent.
- `derivedFrom`: one direct summary field mapped through existing `values` rules.
  Derived values and provenance follow the validated source, irrespective of an
  inconsistent analyst proposal. Chains/cycles remain undetermined.
- `maxLength`: optional text bound; existing fields keep 120 characters.

Only bots explicitly configured with evidence-required fields use expanded
transcript context (all turns, up to 4,000 characters per turn) and a 2,600-token
output allowance. Existing bots retain the compact context and output budget.
The analyst is instructed to use the final clear correction, distinguish future
actions from completed ones, and leave uncertain/hypothetical statements null.
Address details require a settled requested/reported-updated status. Dates must
be valid ISO calendar dates. A call date alone never becomes an update date.

Validated quotes persist as `structured_field_evidence` and are exposed as
`structuredFieldEvidence`. Fallback retains known workflow fields and leaves the
new analyst fields null. Existing summary fields retain workflow precedence;
the new fields do not change call routing, spoken replies, or the booked outcome.

## Local configuration

Prepare a local review artifact without contacting any server:

```bash
env/bin/python jeeves/setup/03_summary_intents.py --dump /tmp/jeeves-summary-policy.json
```

Pass `--policy existing-policy.json` to preserve additional local policy settings
and custom fields. The helper only adds/replaces its seven named fields and is
idempotent. It does not run the earlier workflow setup, publish, or deploy.
For local activation, merge the fields into this bot's `goal_policy.summaryFields`
on a verified local database and run local processes with the changed source.
Do not apply this artifact to DEMO LIVE or NEW PRODUCTION without explicit approval.
Previously saved summaries are not reprocessed or backfilled.

The local database was updated from eight to fifteen fields on 27 October 2026,
with the original eight definitions verified unchanged. The previous policy and
reviewable patch are retained under `.devrun/summary-intents/`. No services were
restarted; running local Python processes need a reload to use the changed code.

## Verification

Deterministic tests cover evidence attribution/rejection, status/boolean
consistency, date validation, long addresses, unchanged workflow precedence,
fallback and bot scoping. An isolated worker test exercises the real analyst
parser, merge, persistence function and API serializer with database/LLM stubs.
Frontend tests cover labels, values, evidence and date rendering.

`tests/fixtures/jeeves_summary_intents.json` contains synthetic semantic cases
for Hindi, Hinglish and English, including positives, denials, future actions,
uncertainty, corrections, requests with no address, partial/complete addresses,
reported completion, relative dates, refund inquiries and unrelated payments.
Pickup-request safety cases accept No or conservative abstention; they must
never become Yes. Confirmed collections and literal collection denials require
their exact expected values. This follows the nullable platform contract and
does not force an inference from a request into a completed-collection fact.

```bash
env/bin/python -m pytest tests/unit/test_jeeves_summary_intents.py -q
ECHOSPHERE_RUN_SUMMARY_EVAL=1 env/bin/python -m pytest tests/evals/test_jeeves_summary_intents.py -q
```

The semantic evaluation calls the bot's configured provider with synthetic
conversations and does not persist conversations or change settings. It is
opt-in to avoid provider calls during routine unit tests. A passing fixture set
does not guarantee correctness on unseen speech-to-text errors, very long calls,
or future model versions. No live call or production end-to-end run is performed.

Final local validation: 603 backend regression tests passed, 34 conversation UI
tests passed, TypeScript checking passed, and all 49 semantic cases passed against
the configured `gpt-4o-mini` analyst. Conservative abstention remains possible on
pickup requests; it must not be interpreted as confirmed collection.
