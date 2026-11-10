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
