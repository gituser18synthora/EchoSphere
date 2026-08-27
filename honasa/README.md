# Honasa Customer Care bot (tenant `tn_620d5400d462`)

Production configuration for the Honasa/Aurexion POC, built from
`scripts/Honasa_Aurexion_Bot_FAQ_Response_Bank.xlsx`. **Scope: the two POC
categories only** — `Order / Information` and `Return / Replacement`.
Cancellation/Refund, General, Escalation-as-a-category and CSAT are NOT
implemented; cancellations and unresolved-issue complaints transfer to a
human, other unsupported requests get a polite scope decline.

| Thing | Value |
| --- | --- |
| Tenant | `tn_620d5400d462` (industry `ecommerce`) |
| Bot | `bot_71194477c0eb` "Honasa Customer Care" — **published**, readiness 7/7 |
| Workflow | `wf_c449f1421055` "Honasa order support journey" (45 nodes / 91 edges, approved) |
| Guardrail profile | `ecommerce_support` (profanity de-escalation + payment-credential block + 4 mandatory) |
| Voice | Sarvam saaras:v3 STT (auto language detect) · Sarvam bulbul:v3 TTS "Shreya" · en-IN default + hi-IN · gpt-4o-mini |
| Turn detection | tenant-wide `recommended` profile, timezone Asia/Kolkata |
| Service account | `honasa.config@honasa.com` / `Demo@2026!` (tenant admin) |
| Mock commerce API | `./honasa/run.sh` → port **9022** (must be running for API nodes) |
| Voice channel | +91 80471 33640 (freeswitch) |

## Layout

- `api/main.py` — mock Honasa commerce service (order lookup, WhatsApp
  tracking link, returns/replacements with the seven-day windows enforced
  server-side, support escalations). Data in `data/orders.json` (dates stored
  as day offsets, materialized at read time — scenarios never rot).
  Runtime writes land in `data/runtime_state.json` (delete to reset).
- `setup/00…05` — idempotent REST configuration scripts, in order:
  `00_tenant_governance` (super admin: industry, guardrail profile, service
  account) → `01_bot_entities_connections` → `02_prompts` → `03_workflow`
  → `04_intents_context_runtime` → `05_go_live [knowledge|channel|scenarios|
  recompute|publish|all]`.
- `docs/Honasa_Order_Returns_FAQ.md` — the bot-scoped knowledge base source
  (in-scope policy content only).
- `tests/run_chat_scenarios.py` — 36-scenario regression suite over
  `POST /bots/{id}/testing/chat` (**36/36 pass**). `tests/chat.py --trace` is
  the interactive tester.

## Conversation design (why it's shaped this way)

Every FAQ row's first move is "share your order ID or registered mobile
number", so the single workflow front-loads ONE ask + ONE lookup, then
branches at a hub intent node:

- **Order / Information** — the lookup maps the full order view into slots
  (status, ETA, courier, tracking, amounts, discount/cashback, refund state,
  eligibility). Fact answers are `llm_grounded` nodes: the flow decides WHAT
  happened, the LLM words it from verified facts and can never invent values
  (validated, authored fallback). Post-verification questions that name a
  verified fact may also be answered by the platform's verified-context chat
  route — same facts, same grounding, no workflow re-entry.
- **Tracking link** — a state-changing API node "sends" the link over
  WhatsApp (FAQ corrective action); fails gracefully when tracking isn't
  live.
- **Return / Replacement** — change-of-mind returns pass through the
  server-computed seven-day eligibility condition (confirm → Return Request
  API → WhatsApp-link confirmation | ineligible explanation → agent offer).
  Damaged / wrong / missing / defective-or-expired each get a tailored
  detail ask, a replacement-or-refund choice, and their own resolution
  connection with `issue_type`/`resolution` pinned in `bodyTemplate`
  (workflow api nodes can only send slots — per-branch connections are how
  constants travel). Quality issues outside the window fail server-side →
  agent offer → escalation ticket → handover.
- **Failures** — lookup retry with a second ask variable (`order_ref2` beats
  `order_ref` in the mock), then escalation + handover. The escalation
  connection deliberately does NOT require a verified caller.

Gotchas honored (from the OYO/mPokket builds): intents route by
`workflow:<id>` (never slug); ask success edge first + `fallback` second;
grounded modes only on deterministically-guaranteed branches; grounded
fallback texts avoid digit runs; hub edges carry both `?`-suffixed tokens
(question signal) and plain tokens (literal tie-break); node text never
interpolates slots. New lesson from this build: **avoid over-generic words in
lookup slot names** — a `product_*` key made `mentions_context_fact` yank
quality statements ("the product is past its expiry date") out of the
workflow, so the items list maps to `order_items`.

## Test data quick reference (`data/orders.json`)

| Order | Phone | Scenario |
| --- | --- | --- |
| 7001001 | 9876501001 | delivered 2d ago, ₹50 discount — return/damaged happy paths |
| 7001002 | 9876501002 | shipped, ETA +2d, tracking live, ₹60 cashback |
| 7001003 | 9876501003 | out for delivery today |
| 7001004 | 9876501004 | processing — no ETA, no tracking |
| 7001005 | 9876501005 | delivered 12d ago — return + quality windows closed |
| 7001006 | 9876501006 | refund in process (₹499, ETA +4d) |
| 7001007 | 9876501007 | delivered 1d ago, COD, no refund |
| 7001008 | 9876501008 | non-returnable hygiene category |
| 7001009/10 | 9876509999 | same phone — latest order wins |
| 7001011 | 9876501011 | wrong-item flow |
| 7001012 | 9876501012 | 3 items, missing-item flow |

## Runbook

```bash
./honasa/run.sh &                                   # mock API on 9022
env/bin/python honasa/tests/run_chat_scenarios.py   # 36-scenario regression
env/bin/python honasa/tests/chat.py --trace         # interactive
```

Live call: the platform voice worker (9002) picks the published config up as
is — verified end-to-end (session_config + greeting bot_text + Sarvam TTS
audio). Keep the honasa mock (9022) running: workflow api nodes execute live
HTTP against it (`API_CONNECT_ALLOWED_HOSTS` already allows 127.0.0.1).
