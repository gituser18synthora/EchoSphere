# OB deduction bot fixes — 11 September 2026

Bot `bot_4b8d6fe95cb1`: saved workflow `wf_fac5f8fae156` is now approved version 6 (31 nodes, 44 edges). The system prompt and API request/response schemas were updated. The local API and idle voice worker were gracefully restarted to load the shared engine changes.

The aligned bot knowledge document was indexed successfully as
`kdoc_3cfde77c2e01` in `ks_9f1e3b8c7ae0`. Its superseded copy was soft-deleted
after the replacement became ready. The first permission review timed out;
the permitted retry succeeded.

| Original finding | Result |
| --- | --- |
| ₹500 communicated / ₹700 deducted accepted as matching | Fixed: a numeric comparison routes to mismatch before readback and confirmation. |
| Corrections retain stale dependent answers | Fixed: changing the communicated flag or either figure invalidates dependent answers and the previous outcome. |
| English greeting answers Q1 implicitly | Fixed: the Q1 entity disables implicit canonical matching; a greeting leaves Q1 unanswered. |
| Placeholder ticket endpoint | Still pending: a real endpoint, authentication and its agreed contract are required. |
| HTTP 200 business failure accepted as success | Fixed: authored response constraints are enforced for real and mocked results. Failed/missing recorded flags cannot confirm the ticket update. |
| Missing ticket IDs and invalid field values accepted | Fixed: required correlation/outcome fields, enumerated statuses and numeric-string validation. |
| Unknown amount sentinel differs from summary | Fixed: the sentinel stays in conversation state but is omitted from API arguments. |
| Readback invents an unknown comparison | Fixed: authored bilingual readbacks use only populated slots and are delivered in exact mode. |
| Prompt and workflow consistency rule conflict | Aligned: communicated amount and confirmed match, without contradictory numeric amounts. An initial lack of explanation is preserved and explained on the call. |
| English authored questions remain Hindi in simulator | Fixed for this bot with configured English text. All replies in the tested English conversation contained no Hindi script. Actual audio was not tested. |

Validation: **160 tests passed** across the bot definition, workflow definitions, entity extraction and tool executor suites. **Five saved-bot conversations passed**: numeric conflict, retracted communicated amount, English flow, business-failure response and missing ticket context. Ticket writes were mocked; no real ticket update or phone call was performed.

[Conversation evidence](ob_deduction_fixes_2026-09-10.json) records the actual replies, slots and `nodeTrace`. An initial checker incorrectly read `trace` instead of the API's `nodeTrace`; the assertions were corrected against those captured responses. Both failure conversations already reached `n_pending` and avoided `n_confirmed`.

```bash
env/bin/python -m pytest tests/unit/test_zepto_ob_deduction_definition.py tests/unit/test_tool_executor.py tests/unit/test_entity_patterns.py tests/unit/test_workflow_definitions.py -q
```

The shared engine has no new tenant-ID or bot-ID conditional. Tenant business rules live in workflow and API JSON; the Zepto setup script produces that configuration. See [configuration options](../../docs/WORKFLOWS.md#configuring-corrections-comparisons-and-readbacks). Dedicated Studio form controls for these new options have not been added. Other tenants' configurations were not changed, and their live conversations were not tested.
