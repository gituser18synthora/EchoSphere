**Fix status — 11 September 2026:** Follow-up fixes have been applied to this bot. See [fix results](ob_deduction_fixes_2026-09-11.md). The audit and evidence below describe the original configuration.

**OB deduction bot audit — 10 September 2026**

Bot: `bot_4b8d6fe95cb1`, Zepto OB Fee Deduction Verification (Outbound).

**Verdict: the prompt, workflow and API JSON concern the correct OB deduction use case, but the bot has confirmed data correctness defects and its real ticketing integration is unfinished.** The existing passing tests are insufficient to approve it for production.

The attached `OB Deduction BOT.docx` was used as a requirements reference. Its bot dialogue and instructions were evaluated as document content. No bot configuration, workflow, prompt or production code was changed during this audit. No phone call was placed.

Evidence: [captured configurations, conversations and payloads](ob_deduction_audit_2026-09-10.json), [existing scenario transcripts](ob_deduction_baseline_2026-09-10.log).

**What was verified**

| Check | Result |
| --- | --- |
| Saved bot | Correct Zepto tenant; published |
| Published prompts | System v2 and greeting v1 match the setup file |
| Saved workflow | `wf_fac5f8fae156`, v5, approved; 29 nodes, 41 edges, no structural validation issues |
| Workflow versus setup | Nodes and edge routing match; generated edge IDs differ only |
| Summary configuration | All 12 configured fields match setup; LLM filling disabled |
| Existing engine tests | 18 passed, 0 failed |
| Existing conversation scenarios | 15 passed, 0 failed, through the local Testing API |
| Independent checks | Six additional text conversations, three intercepted payload cases, three API contract probes, and two follow-up conversations |
| Real ticket update | Unverified: endpoint is a placeholder; success tests use mocked/intercepted results |
| Voice/audio | Not tested; text simulator and voice runtime have different language adaptation paths |

Commands used for the existing suites:

```bash
env/bin/python -m pytest tests/unit/test_zepto_ob_deduction_definition.py -q
env/bin/python -u zepto/tests/run_ob_deduction_scenarios.py -v
```

**Document alignment and API data mapping**

The document's three checks are present: deduction explained during onboarding, deduction amount communicated, and actual deduction matching the communicated amount. The first-answer-no branch explains the fee and continues. The approved facts about variable store fees, one-time payment, upfront payment and weekly installments are represented in the prompt and local KB.

Mismatch handling, verification readback, additional numeric questions, correction loops, ticket payload fields and support handoff are implementation additions. The document does not specify an API, actual fee amounts, a refund policy or a mismatch resolution. The KB additionally includes payment-security guidance that is not in the attached document; this should be identified as bot guardrail content rather than a verbatim document fact.

| Data | Actual source and destination |
| --- | --- |
| `deduction_explained`, `amount_informed`, `amount_matches` | Extracted from rider replies; raw `yes`/`no` sent in API args; summary uses `Yes`/`No` |
| `informed_amount`, `deducted_amount` | Rider-provided amounts, sent as strings; not independently looked up from payout records |
| `payment_mode`, `upfront_amount_paid`, `deduction_date_or_week`, `additional_concern` | Captured when provided or asked by the branch; absent answers normally omitted from API args and shown as null in the summary |
| `explanation_given_on_call`, `verification_status`, `ticket_type` | Set by workflow logic |
| `ticket_id`, `partner_id` | Passed from per-call runtime context through `contextArgs` |
| `bot_id`, `tenant_id`, `session_id`, `workflow`, `conversation_language` | Added by workflow runtime metadata |
| `partner_name` | Used for call context/greeting; not included in API args |
| `channel`, `ticket_type`, `concern_label` | Pinned in `bodyTemplate`; other workflow args are merged into the outgoing body |
| Response `ticket_id` and `status` | Mapped to `ticket_reference` and `ticket_update_status` |
| Response `verification_recorded` | Defined in the response schema but not mapped or checked for success |

The three fields visible in `bodyTemplate` are therefore not the entire request. Intercepted engine tests confirmed that collected fields and context IDs reach the executor. The saved runtime test JSON contains sample partner/ticket values, and the response example only simulates a ticket update. They do not prove that any actual Zepto payout record was fetched or updated.

**Confirmed failures, in priority order**

1. **High — different numeric amounts are accepted as matching.** Start a new Hindi session with `haan` → `haan bataya tha` → `haan 500 bataya tha` → `haan, 700 kata hai` → `haan sahi hai`. The final state and summary contain `informed_amount="500"`, `deducted_amount="700"`, `amount_matches="yes"`, `verification_status="consistent"`. The bot says the deduction appears consistent. The Q3 affirmative pattern accepts a leading affirmation plus a deducted amount without reconciling it with the stored informed amount. This requires clarification or conflict handling before confirming consistency. See `followup_probes.numeric_mismatch_final_outcome` in the evidence and [Q3 patterns](../setup/09_ob_deduction_outbound.py#L315).

2. **High — a correction leaves dependent answers stale and produces the wrong outcome.** Use `haan` → `haan bataya tha, 500 katega bola tha, utna hi kata` → `nahi, amount nahi bataya tha` → `800 kata hai` → `haan sahi hai`. The final state contains `amount_informed="no"`, old `informed_amount="500"`, old `amount_matches="yes"`, `deducted_amount="800"`, and `verification_status="consistent"`. The bot again speaks the consistency conclusion. Corrections need to invalidate dependent fields, and the outcome must check communication status before trusting an old match value. See `followup_probes.retracted_amount_final_outcome`, [correction capture](../setup/09_ob_deduction_outbound.py#L426), and [outcome routing](../setup/09_ob_deduction_outbound.py#L733). The earlier partial probe only checked a status before this conversation finished; its apparent pass is not evidence of correct correction handling.

3. **High — an English identity acknowledgement silently answers Q1.** With a fresh session and `language="en-IN"`, send `Yes, speaking`. The bot immediately stores `deduction_explained="yes"` and asks Q2. The rider never answered whether the deduction was explained. The entity extractor adds each canonical synonym key to its literal matching lexicon, so the key `yes` matches despite the bot setup's comment that bare yes/no surfaces were excluded. Hindi `haan ji boliye` correctly leaves Q1 unanswered. See `live_probes.English greeting acknowledgement and language`, [Q1 entity](../setup/09_ob_deduction_outbound.py#L152), and [canonical matching](../../shared/orchestration/entity_extractor.py#L218).

4. **High — real ticket integration is not connected.** Connection `api_485496c6be7f` points to `https://partner-support.zepto.example/api/v1/deduction-concerns/onboarding_fee/verification`, with `authType="none"`, `status="untested"`, and no recorded connection test. The existing no-mock scenario reaches the API failure branch. The success cases replay `responseSchema.example`. A real endpoint and its authentication/response contract must be integrated before claiming that verification is saved on the existing ticket. This placeholder is documented in the setup, but it remains an operational gap.

5. **High — API business failure is treated as success.** In an isolated executor probe, the HTTP request was intercepted to return HTTP 200 with `{"status":"failed","verification_recorded":false}`. The actual executor returned `ok=true`, `status="ok"` and mapped `ticket_update_status="failed"`. The workflow selects its success edge solely from `result.ok`, so a real service returning this shape would enter the ticket-recorded branch. This was tested below the simulator mock shortcut; it is not just mock behavior. Require the successful business status and recorded flag before confirming the update. See `contract_probes.http_200_business_failure`, [success evaluation](../../shared/orchestration/tool_executor.py#L332), and [workflow API result handling](../../shared/orchestration/workflow_engine.py#L2096).

6. **Medium — ticket identifiers and outcome values are not validated.** `requestSchema.required` is empty, runtime context has no declared fields, and the engine forwards ticket/partner IDs only when present. An isolated full workflow with empty runtime context still called the executor without either ID; the configured validator accepted the request. It also accepted `{}` and `{"verification_status":"anything","informed_amount":"banana"}`. The intended operation updates an existing ticket, so the actual API contract needs a required correlation identifier and appropriate outcome/amount validation. This is a client-side validation gap; behavior of the future Zepto service cannot be established without that service. See `engine_probes.payload_missing_ticket` and the schema contract probes.

7. **Medium — unknown amounts differ between outgoing JSON and summary.** With `haan` → `haan` → `haan` → `yaad nahi hai exact` → `haan utna hi` → `sahi hai` → `nahi`, the intercepted API args contain `informed_amount="not remembered"`, while the structured summary contains `informed_amount=null`. The summary normalizes that sentinel but the API sends raw slots. Normalize missing amounts consistently and, if needed, carry the reason in a separate field. See `engine_probes.payload_unknown_amount`, [summary normalization](../setup/09_ob_deduction_outbound.py#L789), and [raw API args](../../shared/orchestration/workflow_engine.py#L2066).

8. **Medium — generated readback invents a comparison when it is unknown.** With `haan` → `haan bataya tha par amount nahi bataya tha` → `aath sau rupaye kate hain`, slots contain `amount_informed="no"`, `deducted_amount="800"` and no `amount_matches`. The bot nevertheless says `जो amount कटा वो उतना नहीं है` in the confirmation readback, inventing a mismatch with an unknown communicated amount. The same issue appeared in baseline OB-03 and in the independent probe. Build the readback from explicit populated values and validate semantic claims, not only required words. See `live_probes.Unknown comparison readback`. Generated wording can vary between runs.

9. **Medium — prompt and workflow disagree about the consistency condition.** The system prompt permits the document's consistency wording only when all three answers are yes; the consistency node's directive also describes all three as confirmed. The workflow checks `amount_matches` and does not check `deduction_explained` before this outcome. A tested `deduction_explained="no"`, explanation-given-on-call case still produces `verification_status="consistent"`. The attached sample does not fully define the later outcome after a first-answer-no explanation. Agree on the intended business rule and align the prompt, KB, workflow and tests. This is a confirmed internal conflict, not a claim that the document explicitly prohibits every such outcome.

10. **Medium — English text testing does not match the voice delivery path.** The existing English scenarios pass while authored questions and closing replies appear in Hindi, despite `language="en-IN"`. The simulator handles grounded rewriting but does not apply the voice brain's separate scripted-question translation path. This confirms a Testing Studio parity/coverage defect; it does not establish that a real English phone call behaves identically. Add reply-language assertions and test an actual voice session before approving English behavior. See baseline OB-07/OB-08, [simulator delivery](../../backend/routers/testing.py#L1353), and [voice translation](../../voice_runtime/brain.py#L4757).

**Why the existing green result missed these defects**

The English opener test checks whether `n_ask_explained` appears anywhere in the node trace; visiting and immediately skipping that node passes. It does not assert that Q1 remains pending. Several readback tests check for the confirmation phrase but not whether each spoken fact matches the slots. The unknown-amount scenario checks the summary's null, not the final API request. The tests do not cover the numeric-conflict or dependent-correction cases above.

Separately, the platform's `/scenarios/run` endpoint retains previous pass flags and defaults never-run scenarios to passing; it does not execute these conversation scripts. Therefore the bot's readiness status is not a substitute for the independently executed suites. See [scenario runner](../../backend/routers/testing.py#L117).

The captured evidence records exact inputs, replies, final slots, request args, derived summaries, saved API JSON and source hashes. The constructed `http_body` examples combine captured args with the saved constant template; they were not transmitted to a real Zepto endpoint. Fixes and deployment remain outside this audit.
