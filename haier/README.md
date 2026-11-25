# Haier rejected Work-Order follow-up bot (tenant `tn_fb39b67f5361`)

Outbound Hinglish customer-care bot `bot_aba8f9101217` ("Customer Support") that calls
customers whose Haier installation / service / complaint Work Order was rejected by the
Service Center, confirms what happened, and either re-dispatches (Action 1) or cancels
(Action 2) the WO as per the customer's voice.

Sources of truth: `tenant/Haier/Rejection_Script_24_March_26.xlsx` (official script, both
sheets), `tenant/Haier/call_transcripts.md` (8 real agent calls),
`tenant/Haier/HAIER_BOT_UPDATE_PROMPT.md` (requirements). Deliverables live in
`tenant/Haier/` (CHANGELOG, COVERAGE_CHECKLIST, TEST_RESULTS, WEBHOOK_CONTRACT).

| Thing | Value |
|---|---|
| Tenant / bot | `tn_fb39b67f5361` / `bot_aba8f9101217` |
| Workflow | `wf_d5df4464f348` "Haier rejected work order follow-up" (saved in place) |
| Prompts | system `pr_260fba3ed84a` (v1 kept as rollback), greeting `pr_9a7806bf1873` |
| Entry intent | `opening_acknowledgement` → workflowId; `haier_future_requirement` → workflowId |
| Guardrail profile | `haier_standard_payment_restricted` (Haier only; Standard untouched) |
| CRM hooks | 3 placeholder connections on `crm-integration.haier.example` (see WEBHOOK_CONTRACT) |
| Voice | Sarvam STT auto hi/en + bulbul:v3 `vp-sv-shubh` (male → authored forms raha/sakta/chahunga) |
| Logins | none stored; stages use the platform super admin, and `HAIER_ADMIN_EMAIL`/`HAIER_ADMIN_PASSWORD` (existing admin@haier.com) only for `PUT /tenant/turn-detection` |

## Layout

- `setup/haier_config.py` — EVERY Haier-specific value: script lines (Roman Hinglish, gendered
  by `AGENT_GENDER`), 8 reject reasons, 5-day lexicon, disposition fields, hook definitions,
  dialer variables, turn-detection overrides. Nothing Haier-specific exists in shared code.
- `setup/00…05` — idempotent REST configuration stages. **Dry-run by default** (prints every
  write); `--apply` executes. Order: `00_tenant_governance` (super admin: guardrail profile, timezone) →
  `01_bot_settings_connections` → `02_prompts` → `03_workflow` → `04_intents_context` →
  `05_go_live`.
- `setup/06_runtime_context_dialer_variables.py` — stands alone (targets the Studio-built
  demo bot `bot_450a4f7b4832`, not the 00…05 build): switches the bot's runtime context to
  the `dialer` source mode so the FreeSWITCH webhook's `variables` (customer_name,
  product_name, rejection_reason, rejection_reason_label) are the call's context and the
  saved test data only fills sessions without variables. Dry-run by default; `--apply`.
  Requires API + voice-worker code that knows the mode (shared/runtime_context.py).
- `setup/07_demo_workflow_repair.py`, `haier_unit_ok_repair.py` and
  `haier_demo_policy_repair.py` — pure configuration transforms for the reviewed
  Studio DEMO bot `bot_450a4f7b4832`. They separate technician contact from
  dissatisfaction, route confirmed pending problems through the required apology,
  and amend the bot's prompt and summary policy. These helpers do not run in the
  per-call runtime or add model requests. Contact-match precedence is stored in
  ordered capture arrays because MySQL JSON objects may reorder dictionary keys.
- `setup/haier_service_need_repair.py` — pure transform for the same Studio bot's
  service-requirement question and clarification. Explicit service refusals take
  the existing no-service cancellation branch; existing postponement and special
  cases retain their authored routes. The helper uses the existing capture and
  correction contracts and does not change shared routing code. Apply and publish
  through the API of the intended local environment after validation.
- `setup/haier_call_audit_repair.py` — local configuration repair for the
  23 November call review. It scopes technician contact and service status to
  the caller's actual statement, separates pending work from completed work,
  and requires explicit re-dispatch consent. Unknown replies clarify instead
  of agreeing. Ordered capture arrays preserve priority after MySQL JSON
  storage; real interpreter replays cover corrections and unrelated branches.
  The helper does not publish to DEMO or deploy runtime code.
- `setup/haier_followup_audit_repair.py` and `haier_followup_policy_repair.py` —
  pure amendments for the 23 November afternoon follow-up audit of the Studio
  bot `bot_450a4f7b4832`. They separate payment from resolution, preserve visit
  and installation facts, clarify unknown completion sources, retain scheduling
  preferences and require natural, explicit visit permission. The English
  system prompt is in `prompts/customer_support_followup.txt`. The workflow
  marks terminal outcome writes explicitly; the corresponding runtime preserves
  authoritative null signals, re-asks the pending question after hearing checks,
  and contests outcomes withdrawn by the caller. Apply the configuration with
  the matching runtime, then publish a bot release that pins the new workflow
  and prompt versions. These helpers never deploy or restart a server.
- `setup/haier_followup_consent_repair.py` — incremental configuration repair
  for conversation `cv_f5293164ece8`, applied after the follow-up audit repair.
  It accepts explicit short requests such as “haan, badha dijiye” at the visit
  permission step, keeps negation and special-case precedence, uses a distinct
  clarification question, and removes the unsolicited customer-care referral
  from the unconfirmed closing. The outcome remains unconfirmed when no clear
  permission is received. Its v2 upgrade treats Hindi sentence terminators as
  punctuation when matching explicit service refusals at both permission steps.
  Its v3 upgrade uses the reviewed “aage forward kar doon” permission wording
  in initial, clarification and date-preference readbacks, and accepts explicit
  forward commands at those same permission steps. For `cv_1815c8433f93`, the
  matching runtime accepts a whole non-question “hmm” only at a pending standard
  identity question; strict identity still requires an explicit answer, and
  “hmm” cannot grant business-action consent or bypass a caller clarification.
  The v4 amendment fixes `cv_c485f6c330a8`: a complete permission answer such
  as “haan kar dijiye” uses the existing visit-consent route without another
  confirmation question. It keeps the reviewed wording and protected routes.
  This helper has no server or publication side effects.
- `setup/haier_dialogue_regression_repair.py` — incremental repair for the
  named-customer identity loop and repeated complaint introductions. It marks
  the primary identity gate explicitly and authors short resume questions for
  the nine combined introductions. Initial speech, capture rules and graph
  edges remain unchanged. Use the matching runtime: accepted gate outcomes
  synchronize identity state, ambiguous caller questions pause for clarification,
  and their answers cannot become consent at the underlying business step.
  This pure helper never publishes configuration or restarts services.
- `setup/haier_pdf_wording_repair.py` — uses the current
  `Haier_Rejection_Call_Flow.pdf` wording for the Studio bot's non-Haier-product
  and no-service cancellations. Page 2 sends a negative product-ownership answer
  to Action 2; page 3 supplies Action 2 and the common closing. This replaces the
  older Excel's company-helpline and customer-care referrals in those two fixed
  closings. The user-approved neutral non-Haier variant avoids claiming that
  a product-ownership denial means service is not required; the no-service
  branch retains the PDF's service-refusal wording. Routing and outcomes remain
  unchanged.
  The helper has no publication or service side effects.
- `setup/haier_bill_state_repair.py` — workflow-only amendment for
  `cv_6f9d8cbf10c7`. It keeps bill availability at the earlier visit separate
  from current availability, handles bill corrections at the dealer/charges
  steps, and verifies a visit separately from a phone call. Current bill
  possession does not establish warranty, free service or payment consent.
  Use `setup/haier_bill_summary_policy_repair.py` for the matching two temporal
  summary fields and explicit warranty evidence rules. These helpers use the
  existing interpreter contracts and do not modify runtime code, publish
  configuration or restart services.
- `setup/haier_answer_options_repair.py` — pure amendment for the review of all
  77 DEMO calls of 25 November (`qa/REPORT_2026-11-25.md`). Every question (81
  intent hubs) receives `answerOptions`: what each possible answer means and
  where it leads. With the matching runtime a reply is resolved in one order
  at every step: a reaction that answers nothing ("hello", "hmm", "thank you")
  re-asks the question; then the option the decision model chose for the whole
  reply; then a plain yes / no ("ji", "haan", "theek hai", "nahi", with or
  without sir / ma'am); the existing labels and capture patterns remain the
  fallback for a turn the model did not judge. A bare yes/no word inside a
  longer sentence no longer decides a branch. No route is added (an option may
  only lead where its hub could already go) and no spoken text changes.
  `setup/09_answer_options_local.py` saves that revision on the LOCAL bot
  (dry-run by default); it refuses any API that is not on this machine.
- `setup/08_publish_demo_repair.py` — dry-run by default; publishes hash-bound,
  validated configuration through the existing DEMO API only. Unchanged prompts
  and summary settings are retained. It never deploys server code or restarts
  services.
- `tests/replay_demo_decisions.py` — isolated real decision-model/workflow replay
  of private exports, optionally using a trusted downloaded engine source. It
  keeps checkpoints and recordings in memory and forbids external workflow tools.
  It verifies text routing and summaries; it does not verify live audio or
  interruption timing. Validate database-returned definitions as well as builders.
- `tests/scenarios.py` — 37 customer-side scripts (8 transcripts, 21 reason branches, 8 edge cases).
- `tests/replay_offline.py` — real engine, no DB, hooks mocked: runs before anything is applied.
- `tests/run_chat_scenarios.py` — same scripts over `POST /bots/{id}/testing/simulate` after apply.

## Changing the agent voice

Pick the voice in the Studio, set `AGENT_GENDER` in `setup/haier_config.py` to match, re-run
`02_prompts.py --apply` and `03_workflow.py --apply`. The platform's speaker-grammar adapter
(raha/rahi, sakta/sakti, -unga/-ungi) is a second safety net for fixed text.
