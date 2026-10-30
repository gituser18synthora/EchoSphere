# Jeeves Structured Summary

Applies to `bot_44a50e8c4146` (tenant Jeeves). Everything described here lives in
the LOCAL workspace and the LOCAL database; nothing was deployed, published or
changed on DEMO LIVE / NEW PRODUCTION.

Revised 2026-10-29 after the test call `cv_21df0c56facf` (see "What that call
showed" below).

## How a call's structured summary is produced

1. The call flow (`wf_9685ed07e312`, the v22 definition imported on 2026-10-28
   plus the patch of `jeeves/setup/04_address_capture.py`) writes its final slots;
   `voice_runtime/brain.py` snapshots them onto the recorder and
   `voice_runtime/recording.py` persists the transcript and queues a
   `conversation_memories` row.
2. The post-call worker (`shared/post_call/processor.py`, embedded in the voice
   worker and the telephony gateway) loads the bot's `goal_policy.summaryFields`,
   sends ONE narrative analysis request (summary, outcome, commitments, next best
   action) and — for a bot with evidence-required fields — ONE focused evidence
   request (`shared/post_call/analyzer.py::extract_evidence_fields`) that carries
   only those fields' rules and the whole transcript, numbered `[turn N]`, and
   answers with a value, the supporting Customer turn number and the exact words
   from that turn (`{"turn": N, "quote": "…"}`) per field.
3. `shared/post_call/structured.py::merge_structured_fields` keeps workflow slots
   authoritative for the slot fields, validates each analyst value against the
   field's vocabulary, keeps it only with evidence that passes the structural
   checks (the cited turn is the customer's; the quote occurs verbatim in THAT
   turn — `analyzer._expand_turn_references`; a legacy bare quote must be a
   verbatim span of some customer turn — `customer_evidence`), fills the derived
   Yes/No fields from their status field, and applies `requires`.
4. The validated values, their provenance and the quoted customer words persist in
   `conversation_memories.memory` (`structured_fields`, `structured_field_sources`,
   `structured_field_evidence`); `backend/serializers.py` exposes them as
   `structuredFields` / `structuredFieldSources` / `structuredFieldEvidence` /
   `structuredFieldDisplay` on `GET /api/v1/conversations/{id}`; the Conversations
   drawer (`src/pages/tenant/Conversations.tsx`) renders every configured field,
   the quote under its label, and "N of 12 determined". A field the configuration
   no longer declares is still shown when an older row holds a value for it, and
   left out when that row never determined it.

If the evidence request fails (timeout, no JSON) the row is retried like a failed
analysis and, after the attempts are exhausted, the deterministic fallback keeps
only the workflow fields — the analyst fields are never silently persisted as
"not determined" on a successful narrative.

The Testing Studio preview (`backend/routers/testing.py::_structured_summary_preview`)
is slot-only by design: it shows the slot-derived fields (including Address
Changed / Updated Address and a flow-recorded Already Picked Up); the analyst
values are determined only by the post-call worker.

## Fields (12)

Slot-only fields, owned by `jeeves/setup/01_structured_summary.py` (`SUMMARY_FIELDS`):

| Key | Label | Source | Meaning |
| --- | --- | --- | --- |
| `address_changed` | Address Changed | slot `address_verified` | `changed` or `no` → Yes (the customer said the registered address is not the right one: moved / wrong / changed — including the mismatch end, where they could not or would not give a usable new one); `yes` → No (they confirmed it); unset → not determined. A customer statement, never a record update. |
| `updated_address` | Updated Address | slot `new_address` | The new address as the customer spoke it, address components only (see "Address capture"). Shown only with Address Changed = Yes; empty when none was given. |
| `appointment_booked` | Pickup Visit Booked | slot `visit_status` | `booked` → Yes, anything else → No. The visit, not the collection. |
| `appointment_date` / `appointment_time` | Pickup date / time | `visit_date_iso` / `visit_time_24h` | Normalized, only for a booked visit. |
| `call_result` | Call result | slot `visit_status` | booked, date_pending, callback_requested, address_mismatch, wrong_person, not_disconnected, incomplete, already_picked_up, refund_first. |
| `callback_date` / `callback_time` / `callback_preference` | Callback … | callback slots | Only for `callback_requested`. |

Analyst fields, owned by `jeeves/summary_intents.py` (`INTENT_SUMMARY_FIELDS`):

| Key | Label | Meaning |
| --- | --- | --- |
| `already_picked_up` | Already Picked Up | Yes from the flow when the customer said so at a hub (`visit_status = already_picked_up`); otherwise `openWhenUnmapped` leaves the field to the analyst: Yes when the customer states the device was already collected / handed over, No when they explicitly say it has not been collected yet, null when never stated. A booked or pending visit decides nothing — a scheduled pickup never shows as a completed one. Negations and corrections are read from the whole clause and the later turn wins. |
| `refund_status` | Refund status (customer reported) | `requested` (demand for money back), `status_inquiry` (asks/complains about an outstanding refund), `reported_refunded` (says the money arrived), `withdrawn` (cancels/declines), null (no mention, unrelated payment, conditional policy question, bot-only offer). |
| `refund_requested` | Refund requested / outstanding | Derived: requested / status_inquiry → Yes; reported_refunded / withdrawn → No; null otherwise. |

Retired on 2026-10-29 (`RETIRED_FIELDS`, dropped from the configuration by
`03 --apply`, values on older rows stay visible): `address_verified`,
`address_update_status`, `address_change_requested`, `address_update_date`. The
address change is read from the flow's own record now; the analyst never fills
the address fields.

`openWhenUnmapped` (a `SummaryFieldSpec` option, `shared/orchestration/goal_engine.py`):
normally any value in a field's source slot settles the field (an unmapped value
means "not determined" and the analyst is not consulted); with the flag, a slot
value the `values` map does not cover leaves the field open for the analyst. An
explicit `values[x] = ""` ("not applicable") and a `"*"` wildcard still settle it.

`topicWords` is deprecated and ignored (still accepted so stored configurations
load). It was a keyword list the cited turn had to contain; it discarded a
correct value cited on "नहीं नहीं, मैं लोकेशन चेंज कर दिया हूँ" (no Latin topic
word). Evidence is now checked by attribution, not vocabulary: the quote must be
the customer's own words, verbatim, in the cited turn; meaning stays with the
analyst's rules.

## Address capture (in-call)

The v22 flow asks for the correct address at two `elseIsAnswer` hubs
(`n_ask_address`, `n_ask_address_2`, `captureVariable: new_address`). The engine
used to store the whole utterance there. `jeeves/setup/04_address_capture.py`
opts the flow into the `jeeves_address_v1` semantic-slots provider
(`shared/orchestration/extensions/jeeves_address`):

- `semanticSlots: "jeeves_address_v1"` on the start node; `semanticCapture: true`,
  `captureClarificationByLanguage` (hi/en), `captureMaxRetries: 2`,
  `captureFallbackTo: n_mark_address_mismatch` on both hubs; a retraction edge
  `n_ask_address → n_mark_address_ok` ("wahi sahi hai / same address …").
- On the address turn the reader (the bot's conversation LLM, started at
  dispatch in parallel with the Goal Engine decision, so no added latency on the
  usual turn) copies the address components out of the utterance; the code keeps
  only spans that occur verbatim in what the customer said, in spoken order,
  and requires at least one span with a place word. `new_address` holds the
  cleaned value ("थाने सेक्टर 16, 404041"), `new_address_raw` the utterance.
- No usable span (filler only, a bare PIN, a refusal, an invented component)
  → the hub asks again, twice, then takes the mismatch branch (Address Changed
  Yes, Updated Address empty, Call result address_mismatch). The raw utterance
  is never the address.
- A PIN or landmark said in the next breath, at the visit-day ask, is appended
  (one continuation turn); the day is asked again as usual.
- Reader failure (timeout, unreadable output, provider error) or no reader at
  all: treated exactly like an empty answer — clarification, retries, then the
  mismatch branch with the address unresolved. The words are kept under
  `new_address_raw` for audit only (`capture_unverified` in the workflow audit);
  nothing reads that slot as the address.
- Why not the Goal Engine's decision entities: their key names are not
  configured anywhere (the model picked "address"/"pincode" itself), they are
  missing whenever the decision times out (two of six turns in the test call),
  and the decision prompt does not promise verbatim values.

The readback (`n_final_confirm`, `llm_grounded`) and the summary field both read
`new_address`, so the confirmation and the Structured Summary show the same
cleaned address.

## What that call showed (cv_21df0c56facf, 2026-10-29)

Before: `Pickup confirmed: Yes` (the visit, read as a collection); Address
verified, Address update status, Address change requested, Updated address,
Address update date and Device already collected all "not determined"; the
readback said "हमारे इंजीनियर कल आपके नए पते, मेरा इस समय करंट एड्रेस है। थाने
सेक्टर 16। पिन कोड है। 404041, पर …". Causes: the imported v22 flow sets
`address_verified = changed`, which the yes/no field could not map; the
analyst DID return `requested` and "थाने सेक्टर 16, 404041" (replayed twice,
identical) and the `topicWords` guard dropped both; the hub stored the whole
utterance.

After: Address Changed Yes, Updated Address "थाने सेक्टर 16, 404041", Pickup
Visit Booked Yes, Pickup date 30 Oct 2026, Call result booked, Already Picked
Up not determined (nothing was said about it), refund fields not determined;
readback "… आपके बताए हुए पते थाने सेक्टर 16, 404041 पर …". Pinned by
`tests/fixtures/jeeves_call_cv_21df0c56facf.json` through
`tests/unit/test_jeeves_summary_intents.py` and `tests/unit/test_jeeves_address_capture.py`.

## Applying locally

```
JEEVES_SETUP_EMAIL=… JEEVES_SETUP_PASSWORD=… env/bin/python jeeves/setup/03_summary_intents.py --apply
JEEVES_SETUP_EMAIL=… JEEVES_SETUP_PASSWORD=… env/bin/python jeeves/setup/04_address_capture.py --apply
```

Both are dry runs without `--apply` and idempotent. `01_structured_summary.py`
refuses the imported v22 flow (it is not one of its stages); `03` writes the
full field list and `04` patches the saved flow in place. The voice worker /
gateway must run the 2026-10-29 code (`semanticCapture`, the quote-bound
evidence, `openWhenUnmapped`) — on an older runtime the new keys are inert and
the raw capture continues. A bot import replaces both the flow and the fields:
re-run `03` and `04` after any import.

## Evaluations

- `tests/unit/test_jeeves_summary_intents.py`, `tests/unit/test_jeeves_address_capture.py`:
  deterministic contracts, the engine on the patched flow with scripted reader
  output, worker → persistence → API.
- `ECHOSPHERE_RUN_SUMMARY_EVAL=1 env/bin/python -m pytest tests/evals/test_jeeves_summary_intents.py`:
  the configured analyst on the fixtures (short cases + whole calls, including
  the recorded cv_21df0c56facf).
- `ECHOSPHERE_RUN_SUMMARY_EVAL=1 env/bin/python -m pytest tests/evals/test_jeeves_address_capture.py`:
  the configured conversation model as the address reader on Hindi / Hinglish /
  English utterances (verbatim components only, no filler, no invention).
