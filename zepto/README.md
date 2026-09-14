# Zepto Support — delivery-partner deduction concern bots

## Four dedicated single-concern bots (2026-08-31, demo-friendly)

One bot per approved script — no selector, no cross-concern branches; the
call greeting IS the script's concern greeting (hi-IN default, Hinglish
questions, English understood). Setup `zepto/setup/06` + `07`; suite
`zepto/tests/run_single_bot_scenarios.py` (18/18). All published, 7/7,
channels enabled, Hindi greetings live-verified:

| Bot | ID | Workflow | Number |
|---|---|---|---|
| Zepto MDND Support | `bot_59a84478f155` | `wf_7e4cf166c7bd` (flow v3, see below) | +918047133651 |
| Zepto Raincoat T-shirt Bag Support | `bot_75ce66eb9e63` | `wf_dfa638b8dcc4` (4) | +918047133652 |
| Zepto Onboarding Fee Deduction Support | `bot_faf32177a32e` | `wf_469fbdafb2b9` (4) | +918047133653 |
| Zepto RTO Issue Support | `bot_57b55721e7c1` | `wf_eef67b5bfbe2` (4 + conditional) | +918047133654 |

Per-bot concern FAQ KBs (`zepto/docs/Zepto_*_FAQ.md`); the four tenant
connections are shared with the combined bot. Testing guide artifact:
https://claude.ai/code/artifact/b794b899-fb5a-4262-a720-e55bf60a3451

## The original combined bot (kept, unchanged)

Inbound voice support bot for **Zepto / Zepto** delivery partners
(tenant `tn_04250683f1b3`), built from the four approved call scripts in
`tenant/zepto/`:

| Source image  | Concern                                         |
|---------------|-------------------------------------------------|
| `Image-1.jpg` | MDND — Mark Delivered but Not Delivered         |
| `Image-2.jpg` | Raincoat, T-shirt and Bag related deduction     |
| `Image-2.jpg` | Onboarding Fee related deduction                |
| `Image.jpg`   | RTO issue                                       |

## Live IDs

| Thing              | Value                                             |
|--------------------|---------------------------------------------------|
| Tenant             | `tn_04250683f1b3` (Zepto, industry logistics)     |
| Bot                | `bot_3213a1508a96` "Zepto Support" (published)   |
| Workflow           | `wf_adfb7e149ea1` "Zepto partner deduction support" (approved, 48 nodes / 61 edges) |
| Guardrail profile  | `gp_6a139d0dd017` `logistics_partner_support`     |
| Voice channel      | `+918047133650` · freeswitch (enabled)            |
| Voice              | Sarvam saaras:v3 STT (auto language) + bulbul:v3 TTS `vp-sv-kavya`; hi-IN default + en-IN; gpt-4o-mini |
| Service account    | `zepto.config@zepto.com` / `Demo@2026!`          |
| KB                 | "Zepto Partner Support FAQ" (bot-scoped, indexed) |

## Design

**One bot, one workflow, four ISOLATED concern branches.** The platform runs
one workflow per bot, so each concern is a separate branch of one graph and
a branch asks only its own script's questions:

- `n_ask_issue` — the `issue_type` lexicon ask (allowedValues + Latin and
  Devanagari synonyms). The utterance that routed into the workflow is
  consumed here first (`entry_slot_filled`), so **a caller who already named
  their concern is branched immediately and never hears the selector
  question** — the "issue type already provided" path. Retry exhaustion
  falls back to a human handover.
- A condition chain (`issue_type equals …`) selects the branch: `n_m_*`
  (MDND, 7 enquiries), `n_u_*` (uniform kit, 4), `n_o_*` (onboarding fee, 4),
  `n_r_*` (RTO, 4 + the handover-date follow-up asked ONLY when the product
  was handed to the store team — the scripts' one real conditional).
- Data questions are free-text asks with per-branch variable prefixes
  (`m_…`, `u_…`, `o_…`, `r_…`) — verbatim capture for the ticket, and no
  cross-branch slot reuse. Order-ID last-4 asks are digit asks
  (`[0-9]{4}`) with spoken-digit accumulation.
- Each branch ends in its own "Zepto Register … Concern" api node:
  **success** → grounded confirmation speaking the ticket reference from the
  system result; **failure** → the approved script's own closing ("Thank you
  for providing all the information. Please rest assured, we will connect
  with you shortly."). The reserved `.example` ticketing host guarantees the
  failure edge on live calls until the real endpoint replaces it.
- `n_hub_more` ("anything else?") jumps a second concern STRAIGHT to that
  branch's greeting — never back through the issue ask, whose slot is
  already filled. Close: "Thank you for contacting Zepto Support!"

**Tools (no mock service, per project constraint):** four API connections
POSTing to `https://partner-support.zepto.example/...` (reserved TLD — DNS
can never resolve, deterministic failure edge, no data can leak). Each pins
its concern in `bodyTemplate` and carries the response contract + sample
payload in `responseSchema.example`; the regression suite replays those
samples via `/testing/simulate` `mockToolResults`. Swap `url` + auth when
the real ticketing endpoint exists; nothing else changes.

**Routing:** four concern intents + a generic "some deduction happened"
opener route `workflow:wf_adfb7e149ea1`; `zepto_policy_question`
("MDND kya hota hai") routes to the KB; `human_handoff` routes to transfer.
Dialer/IVR-supplied input JSON (`variables` on POST /voice-sessions or the
telephony webhook) reaches the greeting placeholders (`{customer_name}`)
and the LLM call context; deterministic in-workflow branch selection keys
off the caller's own words by platform design (session variables never
become workflow slots — that is the platform's trust model, not a gap).

**Guardrails:** tenant profile `logistics_partner_support` =
profanity_deescalation (flag) + payment_collection_restriction (block) on
top of the four always-on mandatory rules (pii_redaction,
secret_leakage_prevention, unsafe_tool_call_block,
prompt_injection_protection).

## Setup (idempotent, in order)

```bash
env/bin/python zepto/setup/00_tenant_governance.py   # super admin: profile, languages, service account
env/bin/python zepto/setup/01_bot_entities_connections.py
env/bin/python zepto/setup/02_prompts.py
env/bin/python zepto/setup/03_workflow.py
env/bin/python zepto/setup/04_intents_context_runtime.py
env/bin/python zepto/setup/05_go_live.py             # knowledge channel scenarios recompute publish activate
```

## Tests

```bash
env/bin/python zepto/tests/run_chat_scenarios.py     # 13/13
```

Covers: all four branches end-to-end (mocked ticket success AND live
failure-edge fallback), the RTO conditional, concern isolation (no
cross-branch questions or nodes), direct concern routing from the opener
(Latin + Devanagari), a second concern in the same call, off-script
questions mid-branch, KB routing, human handoff, spoken-digit order IDs,
and selector retry-exhaustion fallback. The same scenarios are recorded as
platform test scenarios (readiness r7).

## Notes / assumptions

- The scripts address the caller as "Hi Zepto," (a template artifact) and
  are written in English; the bot greets with the partner's name when the
  dialer provides it and keeps the scripted node texts in English (en-IN
  default), with hi-IN STT/TTS and Hinglish LLM replies for off-script
  turns.
- "Please rest assure" in the scripts is spoken as "Please rest assured"
  (grammar only; wording otherwise verbatim).
- The scripts' closing thank-you plays on the api failure edge; on success
  the grounded confirmation adds the ticket reference and the 24–48h
  callback window (from the tool's response contract).
- Re-raising the SAME concern twice in one call reuses the first pass's
  answers (platform slot-reuse) — a second DIFFERENT concern works via the
  anything-else hub. Known, accepted for this scope.

## MDND flow v3 (2026-09-03) — `zepto/setup/08_mdnd_flow_v3.py`

Applied to the dedicated MDND bot only (workflow rebuilt from
`06_single_bots.build_mdnd_workflow()`, a new published system-prompt
version, and `goalPolicy.summaryFields`). Greeting and ticket readout are
unchanged. After the partner's narrative the flow collects exactly four
facts and skips every one the story already answered:

1. **Reached the customer's location + called the customer** — asked in ONE
   natural question when both are unknown (`n_ask_reached_called`); condition
   nodes fall back to the single question when one half is already known.
   Both values are extracted independently ("dono/both", "pahuncha par call
   nahi kiya", bare yes/no → the reached half only, call asked separately).
2. **Who received the order** (`m_handover_recipient`): guard / security,
   customer (direct), mother, father, brother, relative (other), left at door,
   someone else, or not handed over. Guard-name follow-up only when the guard
   received it and no name was captured.
3. **CX-support call about this delivery** (`n_ask_cx`, `m_cx_support_call`) —
   new.
4. **Verification** — grounded summary ending in "क्या ये सब सही है?" (English
   callers: "Is all of this correct?", pinned via `responseMustIncludeByLanguage`).
   It opens the partner's-answers part with a short natural "आपके द्वारा दी गई
   जानकारी को एक बार confirm कर लेता हूँ" line (English equivalent for English
   calls). The ticket facts ("record के हिसाब से … 9203 / 4 अगस्त / 400") are
   repeated ONLY when the partner did not hear the opening readout in full:
   the voice brain reports which nodes' replies played to completion
   (`heard_nodes`, from the TTS router's completion signal + bot-stopped,
   un-marked on barge-in; a readout that fell back to its authored question
   is not counted) and the engine picks the `responseDirectiveVariants`
   entry keyed on `n_ask_issue_desc` heard. In /testing/simulate every reply
   is heard unless the next turn is sent with `"interrupted": true`. A
   rejection that carries the fix ("nahi, customer ko nahi — guard ko diya
   tha") is applied at the hub and re-verified (the "which part?" ask has
   `skipIfCorrectedThisTurn`); a field named as wrong without a value
   ("cx wala galat hai") is CLEARED (`alsoCapture … clear: true`) and only
   that question is asked again; the correction edge re-walks the enquiry
   chain, so filled answers are never re-asked and nothing restarts.

**Structured call summary** (`goalPolicy.summaryFields`, stored on the
post-call memory row as `structured_fields`, exposed as `structuredFields` on
the conversation detail API and shown in the Conversations drawer):

```json
{"call_customer": "Yes/No", "reach_customer_location": "Yes/No",
 "hand_over_product": "Yes/No",
 "hand_over_to": "customer|security_guard|mother|father|brother|relative|doorstep|someone_else",
 "call_cx": "Yes/No"}
```

Values come from the FINAL workflow slots (so corrections are reflected);
the post-call analyst may fill only a field the flow never collected, and
only with an allowed value. `/testing/simulate` returns the same derivation
per turn as `workflow.structuredSummary`. Suite:
`env/bin/python zepto/tests/run_single_bot_scenarios.py MDND` (21/21;
optional scenario filters, e.g. `MDND "MDND 15"`).

## OUTBOUND OB-fee deduction verification bot (2026-09-10) — `zepto/setup/09_ob_deduction_outbound.py`

Source of truth: `bot_POC/Zepto/OB/OB Deduction BOT.docx` (onboarding-fee KB +
sample verification dialogue). The rider has ALREADY raised an onboarding-fee
deduction ticket; the bot calls, gives the ticket context in the greeting and
verifies conditionally — it never asks "how can I help you?".

| Thing | Value |
|---|---|
| Bot | `bot_4b8d6fe95cb1` "Zepto OB Fee Deduction Verification (Outbound)" (published, 7/7) |
| Workflow | `wf_fac5f8fae156` "Zepto OB fee deduction verification (outbound)" (29 nodes / 41 edges) |
| Connection | `api_485496c6be7f` "Zepto Register OB Fee Verification" (bot-scoped, reserved `.example` host) |
| KB | "Zepto OB Fee Deduction KB (document)" ← `zepto/docs/Zepto_OB_Fee_Deduction_Verification_KB.md` |
| Channel | `+918047133655` · freeswitch (enabled) |
| Languages / voice | hi-IN primary + en-IN — **copied verbatim from `bot_59a84478f155`** (sarvam saaras:v3 auto-detect STT, bulbul:v3 `vp-sv-ashutosh` for both locales, gpt-4o-mini, elevenlabs fallback, humanSpeech) |

**Flow (condition-driven):** Q1 deduction explained? → (no → explain the fee
from the document ONCE, `explanation_given_on_call`) → Q2 amount communicated?
→ (yes → informed amount; no → actual deducted amount, no "does it match")
→ Q3 deducted amount same as communicated? → (no → deducted amount + week) →
grounded readback "क्या ये सब सही है?" with inline corrections / field clears
(`n_ask_correction` self-skips, re-walk skips filled asks) → outcome message
sets `verification_status` (`consistent` | `amount_mismatch` |
`amount_not_communicated`, via message-node `setSlots`) → "anything else
about this deduction?" (substantive answer consumed as `additional_concern`,
declared `correction` edge re-verifies a changed fact) → api node → close
("You're all set! Thanks for confirming.").

Every ask carries the narrative `alsoCapture` set (bilingual negation-aware
`synonymPatterns`, spoken-number retry), so one utterance can fill several
fields and answered questions are skipped. Slots are the user-facing schema:
`deduction_explained, amount_informed, informed_amount, deducted_amount,
amount_matches, payment_mode, upfront_amount_paid, deduction_date_or_week,
verification_status, additional_concern` (+ `explanation_given_on_call`,
`ticket_type`). The api node adds `bot_id / tenant_id / session_id / workflow /
conversation_language` (`includeMetadata`) and the dialer's `ticket_id` /
`partner_id` (`contextArgs`). `goalPolicy.summaryFields` reports the same
fields post-call with `allowLlm: false` everywhere (never guessed).

Design decisions: a communicated amount ("500 katega bola tha") also counts as
"deduction explained = yes"; amounts/week/payment mode are capture-only except
the discrepancy branches; the greeting confirms the partner's identity so a
bare "haan" routes into the flow without being swallowed by Q1 (Q1's own
entity has no bare yes/no surfaces — bare answers resolve from the
affirm/refusal signal). Not defined by the document (not implemented): any
resolution for a mismatch or a non-communicated amount, fee amounts, store
lists, refund/reversal rules, timelines.

Tests: `env/bin/python zepto/tests/run_ob_deduction_scenarios.py` (15/15,
hi/Hinglish/en, all branches, multi-answer, corrections, nulls, interruption,
language switching, handover) and `tests/unit/test_zepto_ob_deduction_definition.py`
(engine replay incl. the final API payload). Stages:
`09_ob_deduction_outbound.py config|all|<stage>`.

### 2026-09-11 review fix (cv_e4df054b5651)

Root cause: `n_msg_explain` was `llm_grounded`; when it was walked together
with the next ask, the brain sent BOTH through one constrained generation and
the model returned only the question (the validator only checked "has a
question mark" + length), so the document explanation was never spoken while
`explanation_given_on_call` had already been recorded. Fixes: the explanation
is now FIXED text (Hindi + `textByLanguage` en), the grounded validator rejects
a multi-sentence script that collapses to under 40 % of its length
(`_collapsed_to_question`), and the flow was reworked to EXTRACT → STATE →
MISSING → NEXT: not explained → explain once → deducted amount (if unknown) →
Q2 (if unknown); both figures known → `amount_matches` DERIVED (`numeric_eq` /
`numeric_ne` + silent `setSlots`, spoken once when they differ) and Q3 never
asked; a "same" answer that contradicts two differing figures is overridden;
the payout-week question only on a confirmed mismatch; the shared
deducted-amount ask exits via `n_cond_ded_next`. Inference "amount told ⇒
deduction explained" applies only when answering Q1. Suite 21/21 (OB-13…18
added: the review examples in Hinglish, English, mixed script).
Note: admin@zepto.com set the bot's languageVoiceMap default to en-IN on
2026-09-11 (English testing); hi-IN remains the intended primary — the suite
pins the language per scenario. Greeting v3 (hand-authored: identity question
first) is the published one and mirrored in stage 09.

### 2026-09-11 (2) Default Language + Hindi wording (cv_10dd1b13a5e1)

- `shared/bot_config.py`: the greeting variant is chosen by the bot's default
  language (`languageVoiceMap.default`, else first bot language) via
  `select_greeting_variant` — it used to take the first authored variant (Hindi)
  regardless of the Default Language. Changing Default Language in the Voice tab
  is now enough; the greeting/fixed texts are not language-hardcoded.
- `shared/orchestration/voice_identity.py`: the speaker-grammar adapter regenders
  habitual/modal forms (करती/सकती/जाती …) ONLY right before हूँ/हूं; it used to
  rewrite every -ती form in any first-person sentence ("fee ली जाती है" → "जाता है").
- Engine: retry re-asks use `textByLanguage`; a matcher ask that captures a
  downstream field without an own value goes off-script instead of burning a
  retry (`_captures_other_field`).
- Script: natural Hinglish explanation ("Zepto join करने पर नए rider से onboarding
  fee ली जाती है …"), English `textByLanguage` for every fixed/grounded-fallback
  node (explain, outcomes, register hold, noted, readback prompt), readback
  phrasing; `_W`/`_END_W` exclude the danda (STT ends sentences with "।", which is
  inside the Devanagari block — "कट गए।" did not match); the deducted-amount ask
  also captures Q2's answer; greeting text = published v4; stage `bot` no longer
  overwrites voice settings on a re-run (`--reset-voice` to re-copy).
- Voice E2E (browser channel, synthesized caller speech via Sarvam):
  English default cv_580f0c746fdb / cv_c5e7ddfd3acf, Hindi default
  cv_cb018a40fdde, Hinglish caller cv_0d77925f5b4a — greetings follow the
  default, fixed texts follow the conversation language, structured summaries
  correct. Harness: scratchpad `e2e_voice.py` (waits for the bot's audio
  playout before the caller speaks; the caller gate drops audio inside the echo
  guard otherwise).

### 2026-09-11 (3) KB questions during the outbound flow (cv_56df956b0430)

Root cause: with a workflow active, `TurnRouter.decide` routes every turn to
the workflow (`active_workflow`), `_apply_classification` never upgrades a
WORKFLOW decision, so a `policy_question` (route knowledge) classified by the
LLM went to the engine → off-script → persona-only LLM reply ("I can only
assist with the ticket"). No retrieval ran. Fix (platform, generic):
- `TurnRouter.detect_knowledge_question(text)` — clause-level detection
  (sentence/connector split: "waise", "aur", "but"…): a knowledge-intent sample
  match, or a question-shaped clause naming a topic from the knowledge intents'
  vocabulary; returns the clause as the retrieval query.
- `ConversationBrain._handle_workflow`: off-script turn with a knowledge
  intent / question signal → `RouteKind.KNOWLEDGE` generation (retrieval-
  grounded, paused step restated); consumed turn + KB question →
  `_kb_answer_for_workflow_turn` (retrieval + constrained answer in the caller
  language, `wf_kb_miss` on a miss) prepended to the flow's reply. Events
  `workflow_kb_question`, `kb_retrieval(in_workflow)`.
- Engine: node `coversKnowledgeQuestion` → result `knowledgeCovered` (the
  flow's own explanation counts as the KB answer; no double explanation).
- Simulate mirrors all of it (`workflow_off_script_kb`, `workflow_kb`,
  `workflow_kb_covered` routes; `_knowledge_context` = all passages).
- Bot: `policy_question` intent (in_30d4265b0d33, route knowledge, threshold
  0.5) extended to 45 Hindi/Hinglish/English phrasings; KB doc gained a
  Hindi/Hinglish rendering of the same facts (keyword retrieval on the local
  mock embedder); payment-mode capture is personal-statement-only (questions
  never fill slots); `_DEDUCTED` covers STT "कट हुआ"/"बट".
- Suite OB-19…OB-24 (standalone KB, KB mid-flow, mixed utterances, slot
  confusion). Voice: cv_a7ab55eca496 (standalone KB at Q1), cv_2d58823dfc59
  (Q1 no + KB), cv_f0e097a61048 (300/400 + store-fee KB).
  Intent-confirmed knowledge questions retrieve with a relaxed gate (0.15 vs the
  0.35 default); simulate's knowledge route is retrieval-grounded. Suite 27/27.
  Negative pass: unsupported/unrelated/value questions never invent policy or fill
  slots (OB-25…28); knowledge clause needs two topic words; below-gate retrievals dropped.

### 2026-09-11 (4) conversational quality (cv_2c60d51f61fb)

- Engine `readback` v2: per-locale `groups` (one natural sentence for related
  slots, `requires`/`equals`/`absent`/`differ`/`same`, consumes its slots) before
  per-field phrases; templates take `{slot}` and derived `{diff:a,b}` (₹100
  difference spoken, never stored). Hub `correctionAck` (`{changes}` = the
  changed slots' readback phrases, `variables` = leaf facts) acknowledges a
  restated/changed value instead of "बस confirm करना है"; a changed steering
  fact or an invalidated derived slot still re-walks. `awaitingKind` in the
  result; `also_invalidated` audit; `regexPatterns`/`synonymPatterns` count as
  ask matchers.
- Brain/simulate: at an intent hub the off-script instruction forbids reciting
  the hub prompt verbatim after answering (varied or no follow-up).
- Analyst: narrative/important_facts follow the configured field order and
  keep the most specific value ("last week, Monday").
- Bot: weekday-aware date capture ("पिछले हफ्ते मंडे", "Monday"), grouped hi/en
  readbacks, no "team will review" anywhere (prompt, directives, context), no
  "updating your ticket" hold and no "not confirmed" closing (placeholder API →
  "details noted"), summaryFields in conversation order with ticket_type last.
