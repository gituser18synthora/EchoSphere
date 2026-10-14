# CHANGELOG — bot_aba8f9101217 (Haier rejected Work-Order follow-up)

Prepared 2026-10-01. **State: built and tested locally, NOTHING applied yet** (local DB
untouched, live untouched). Apply order and every payload: `tenant/Haier/APPLY_PAYLOADS.txt`
(+ `haier/setup/haier_workflow_definition.json`). Push to live only on the explicit
instruction "push to live".

Scope discipline: tenant files only — `haier/` (setup + tests) and `tenant/Haier/` (docs).
No file under `shared/`, `voice_runtime/`, `backend/` or any other tenant was modified.

## 1. STEP 0 — current state vs required state (audit of 2026-10-01, local == live)

| # | Area | Current (live and local were identical) | Required | Status |
|---|---|---|---|---|
| 1 | Workflow entry | 22-node workflow saved but NO intent routed to it → every call ran prompt-only | Flow owns the script | Fixed: `opening_acknowledgement` → workflowId + new `haier_future_requirement` |
| 2 | Scope | Installation-only, "cancelled by field team" | Any L3 type, rejected by Service Center, 8 reject reasons | Fixed: workflow rebuilt (119 nodes / 268 edges) |
| 3 | Purpose line | Reject reason never spoken | Product + L3 + reject reason | Fixed: `n_hub_purpose` |
| 4 | Variables | customer_name, product_name, service | + wo_number, l3_tagging, reject_reason, request_date, helpline_number, customer_gender | Fixed: runtime-context schema; WEBHOOK_CONTRACT.md |
| 5 | Reason probing | bill/charges, refuse-confirm, delivery, e-com only; branching on what the caller volunteered | All 8 reasons, exact script questions, driven by `reject_reason` | Fixed: `n_cond_r_*` chain + 8 reason hubs |
| 6 | Action 1 / 2 | api nodes referenced non-existent connections → "could not process" apology | Script lines + CRM hooks | Fixed: 3 placeholder connections; script lines spoken on success AND failure (hook_status recorded) |
| 7 | 5-day rule | Absent | Enforce, capture date | Fixed (lexicon heuristic + confirmation; raw phrase in `required_date_text`) |
| 8 | Edge cases E1–E16 | wrong-person retry + no-call apology only | all | Fixed (see COVERAGE_CHECKLIST) |
| 9 | No-response | platform default 3 check-ins | 2 | Fixed: tenant turn detection (telephony) |
| 10 | Dispositions | none | structured object | Fixed: `goal_policy.summaryFields` (13 fields) |
| 11 | Closing | verbatim text but `llm_grounded` | exact | Fixed: `responseMode: exact` everywhere |
| 12 | Language / address | prompt v1 Devanagari, Sir/Ma'am banned; workflow Roman Hinglish with 2 KB of LLM directives per node | Roman Hinglish, consistent address | Fixed: prompt v2 companion, exact nodes, `{{customer_name}} ji` default |
| 13 | Guardrails | tenant Standard = profanity only | no payment/OTP/card | Fixed: new Haier-only profile |
| 14 | Timezone | UTC | Asia/Kolkata | Fixed (stage 00) |

## 2. Added

- **Workflow `wf_d5df4464f348` rebuilt in place** (same id, version 3 → 4, status approved):
  start → bookkeeping (`customer_reached`, dialer facts prefilled into slots) → purpose +
  technician check → reason chain → 8 reason hubs → service requirement → date / 5-day rule →
  Action 1 (`n_api_redispatch` + exact line) or Action 2 (branch line + helpline advice +
  `n_api_cancel`) → exact closing. Edge-case branches on every hub: gift, resale, do-not-call,
  escalate, callback, wrong person, "no such complaint". Behaviour version 3 declared.
- **Disposition capture**: silent `setSlots` nodes per branch (`action`, `cancel_sub_reason`,
  `technician_contacted`, `service_required`, `within_5_days`, `dnc_requested`, `hook_status`,
  `resolution`, `charges_accepted`) + `alsoCapture` harvesting of `required_date_text`,
  `date_bucket`, `technician_contacted`, `dnc_requested` from free speech (also on LLM turns).
- **System prompt v2** (Roman Hinglish persona + rules companion; v1 kept as rollback) and
  **greeting v3** (script 3.1 opening in both language variants, name/gender from the voice).
- **Intents**: `opening_acknowledgement` now routes into the workflow and gains short
  acknowledgements (hmm, ji, han…); new `haier_future_requirement` entry intent.
- **Runtime-context schema** with all dialer variables + Testing-Studio payload + missing-value policy.
- **3 CRM hook connections** (placeholder `.example` host): re-dispatch, cancel, set do-not-call.
- **Guardrail profile** `haier_standard_payment_restricted` (profanity de-escalation + payment
  collection restriction), assigned to the Haier tenant only; Standard untouched.
- **Tenant**: timezone Asia/Kolkata; telephony silence ladder 8 s / 8 s / 2 prompts (browser
  overrides kept). No service account is created: the super admin performs every write except `PUT /tenant/turn-detection`, which is tenant_admin-only and is run with the existing admin@haier.com login supplied via environment variables (never stored in the repo).
- `goal_policy.summaryFields` (13 fields) — `endCall.enabled: true` kept.
- `llm_settings.time_context_enabled: true` (date grounding for the LLM; nothing else in
  llm_settings changed).

## 3. Changed (official script wins over the existing bot)

| Existing | Now | Why |
|---|---|---|
| System prompt in Devanagari, "Do not use Sir/Ma'am" | Roman Hinglish; `{{customer_name}} ji` default, Sir/Ma'am only when `customer_gender` supplied, never switched | user decision + script |
| Purpose: "field team ne cancel kar di thi", no reject reason | "Service Center ki taraf se reject hokar aayi hai, {{reject_reason}} ke reason mein" | script 3.2 / STEP 7.1 |
| Charges node: "bina bill ke hardware charges dena zaroori hota hai… process nahi kar payenge" (quoted a reason, closed the service) | Script: "Kya aap charges dene ke liye ready hain?" → Action 1 / Action 2; no amounts ever | script Sheet2 P39, rule 3 |
| Cancel on "not required" → generic cancel text | Script L22 + Action 2 helpline advice | script |
| Every node `llm_grounded` with a long per-node language directive | `exact` everywhere; one companion prompt | verbatim script, no paraphrase |
| `n_msg_request_fail` ("process nahi kar paaya") on hook failure | Script line still spoken; `hook_status=failed` recorded for Haier's export | hooks are placeholders until Haier provides endpoints |
| Existing identity hub re-asked "Kya meri baat X se ho rahi hai?" after the greeting already asked it | Greeting asks; workflow starts at the purpose statement | no double question |

## 4. Removed

- Nothing from the official script. Removed from the OLD workflow: the duplicate identity hub,
  the "reconfirm you raised this" hub (replaced by the two-step "koi Haier ka product hai?"
  verification of E8), the hardware-charges explanation, the generic failure apology, and the
  per-node LLM directives. The old definition (22 nodes) is preserved in the workflow row's
  revision history (version 3) and in `tenant/Haier/APPLY_PAYLOADS.txt` context.

## 5. Script-internal conflicts noted (see COVERAGE_CHECKLIST "conflicts")

Sheet1 vs Sheet2 differ on the NO branches of Bill / DOA / Refuse-to-Pay and on
Unit-OK-problem-persists / Not-Haier-yes. Resolution: Sheet2 decision table where the
requirements table (STEP 3.4) agrees with it; the requirements table otherwise.

## 6. Needs platform-level change (NOT implemented — your decision)

1. **Day-count condition operator for the 5-day rule.** The engine's condition node compares
   strings/numbers only. Request: a `date_within_days` operator (slot date phrase normalised
   against a context date such as `request_date`, with Hinglish relative-date parsing) or an
   LLM date-normalisation step that writes `required_date_iso`. Today's tenant heuristic:
   `DATE_BUCKET_PATTERNS` (later / within) + a confirmation hub before any cancel; the raw
   phrase is stored in `required_date_text` for audit.
2. **Per-tenant silence check-in wording.** "Hello, kya aap mujhe sun pa rahe hain?" lives in
   `shared/orchestration/phrases.py`. Tenant config can only set count/timings (done). Request:
   `textByLanguage`-style override for `silence_check_*` in tenant settings.
3. **Entry-turn captures.** `alsoCapture` does not run on the utterance that ENTERS a workflow,
   so a first answer like "ek mahine baad karwayenge" reaches the purpose hub without the date
   captured (asked again later). Request: run the first hub's captures on the entry text.
4. **Deterministic wrong-person regex** misses "main Vinay nahi hoon" (known platform gap);
   the LLM + Goal Engine `endCall` cover it today.
5. **Platform DNC interception** ends the call before the workflow can record `dnc_requested`
   or call the Haier DNC hook for phrasings the detector catches ("dobara call mat karna").
   Request: expose the platform DNC event to the post-call structured summary.

## 7. Could not implement / needs Haier input

| Item | Why | Recommendation |
|---|---|---|
| Real re-dispatch / cancel / DNC APIs | Haier endpoints and auth unknown | Replace `url` (+ `authType`/`secretRef`) on the three connections; request/response contracts in WEBHOOK_CONTRACT.md |
| Helpline number | Not in the script | Dialer sends `helpline_number` per call (digit-grouped for TTS); until then the advice line drops the number and says "helpline number par" |
| Dialer variables | Campaign side | Send the 8 keys in the webhook `variables` (WEBHOOK_CONTRACT.md) |
| Sir/Ma'am in FIXED lines | Needs a conditional-text feature; fixed lines use `{{customer_name}} ji` | LLM turns use Sir/Ma'am when `customer_gender` is sent; if Haier insists on Sir/Ma'am in every line, the dialer could send `customer_name` already as "Sir"/"Ma'am" |
| Exact 5-day arithmetic | see §6.1 | heuristic + confirmation now |
| Optional "Aap apni side se call disconnect kar sakte hain" | platform hangs up right after the end node | not needed |

## 8. Known behaviour to watch on the first controlled live calls

- Roman Hinglish TTS (Sarvam bulbul:v3, en-IN voice map default): pronunciation of some words
  may differ from Devanagari input — if needed, `textByLanguage.hi` Devanagari variants can be
  added per node without changing the flow.
- Local `stt_model` is saaras:v4 (changed by the Haier admin login on 2026-10-01 08:06); live is
  saaras:v3. Not touched by these scripts; the user decides which to run.
- "Theek hai" / "Okay" as the answer to a yes/no question is taken as YES (platform affirm
  signal) — same as the human agents did ("Okay" → proceed).

## 9. Pending

1. Review `tenant/Haier/APPLY_PAYLOADS.txt`; approve → run stages 00–05 with `--apply` (local).
2. Run `haier/tests/run_chat_scenarios.py --md tenant/Haier/TEST_RESULTS_simulate.md`; fix and re-run.
3. Live DB push only on "push to live" (same stages against the live API; never live code).
