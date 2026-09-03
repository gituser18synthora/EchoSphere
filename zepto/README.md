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
4. **Verification** — grounded summary ending in "क्या ये सब सही है?". A
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
