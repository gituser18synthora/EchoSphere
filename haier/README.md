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
- `tests/scenarios.py` — 37 customer-side scripts (8 transcripts, 21 reason branches, 8 edge cases).
- `tests/replay_offline.py` — real engine, no DB, hooks mocked: runs before anything is applied.
- `tests/run_chat_scenarios.py` — same scripts over `POST /bots/{id}/testing/simulate` after apply.

## Changing the agent voice

Pick the voice in the Studio, set `AGENT_GENDER` in `setup/haier_config.py` to match, re-run
`02_prompts.py --apply` and `03_workflow.py --apply`. The platform's speaker-grammar adapter
(raha/rahi, sakta/sakti, -unga/-ungi) is a second safety net for fixed text.
