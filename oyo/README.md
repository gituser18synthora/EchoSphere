# OYO Meta-Bot Solution — Booking Confirmation

> **Testing and usage: see [`TESTING_GUIDE.md`](TESTING_GUIDE.md)** — services and startup
> order, per-bot test cases with exact messages and expected replies, the end-to-end
> meta-bot flows, the full mock-API reference, and the clean-start checklist.

Implementation of the two OYO documents in `oyo_doc/`:

- **`Booking_Confirmation.docx`** — the requirements spec (Flows 1–9 + the nine
  required integrations).
- **`Booking Confirmation prompt.docx`** — the detailed call script, including the
  property-side denial scenarios (overbooked / maintenance / price vs 7-day ARR)
  and the relocation flow.

Tenant: **`tn_de5cc992b1e9` (oyo)** — guardrail profile *Travel and Hospitality*.

## Architecture — why three bots

The platform has no mid-call outbound/consult-call capability and no bot-to-bot
runtime layer, so the "meta bot" is composed the way this platform composes bots:
**one bot per call party**, connected through a shared integration backbone
(this mock service, standing in for OYO's real backend):

| Bot | ID | Party | Covers |
|---|---|---|---|
| **OYO Booking Support** (reused existing bot) | `bot_e8cf0b05bb79` | Inbound customer (IVR "upcoming booking") | Flows 1–6, 8, 9: intent identification, verification, booking confirmation, details, voucher, check-in-confirmation orchestration, shift flow, disposition |
| **OYO Property Verification** | `bot_99177674902a` | Outbound → Property Manager ("Amit") | Property Verification Flow: honor/deny, overbooked → occupancy check → penalty advisory, maintenance → alternate room, price → 7-day ARR / complimentary amount, outcome reporting |
| **OYO Stock Team Validation** | `bot_78b6aa83d94a` | Outbound → internal Stock Team | Flow 7: validate whether the booking will be honoured when the PM is unreachable / does not confirm |

**Orchestration & context passing.** The customer bot's workflow invokes
`POST /calls/property-manager` / `POST /calls/stock-team` (this service) — the
stand-in for triggering the outbound verification call. The PM / Stock bots
report their call outcome to `POST /verification-reports`; when a **live report**
exists for a booking it **wins over the scripted outcome**, so a PM-bot
conversation genuinely changes what the customer bot says next (verified:
`pm_outcome_source = live_report`). Booking context flows to the outbound bots
as workflow slots (booking ID → booking details → property id), and every
customer call closes with a CRM disposition carrying the full slot state.

```
Customer ──► OYO Booking Support ──► /calls/property-manager ─┐
                    │                                          │ live report wins
                    │                /verification-reports ◄───┤
                    │                        ▲                 │
                    ▼                        │                 ▼
              /crm/dispositions    OYO Property Verification / Stock bots
```

## Mock integration service (`oyo/`)

```
oyo/
  api/main.py     FastAPI app — reads oyo/data/*.json, no hardcoded payloads
  data/           static mock data (one file per use case) + runtime_state.json (runtime writes)
  setup/          the REST scripts that configured the tenant (rerunnable/idempotent)
  tests/          chat.py (interactive tester) · run_chat_scenarios.py (34 scenarios)
  run.sh          starts the service on port 9021
  TESTING_GUIDE.md  how to run and verify everything
```

Run: `./oyo/run.sh` (or `env/bin/uvicorn oyo.api.main:app --port 9021`).
Delete `oyo/data/runtime_state.json` to reset reports/dispositions/vouchers/shifts.

### Endpoints (all under `/api/v1`)

| Spec integration | Endpoint |
|---|---|
| Customer Verification API | `POST /customers/verify` (200 only when verified; 401/404 otherwise) |
| Booking Details API | `GET /bookings/{booking_id}` |
| Booking Voucher API | `POST /bookings/{booking_id}/voucher` |
| PM Outbound Calling | `POST /calls/property-manager` (live report > `pm_call_outcomes.json`) |
| Stock Team Outbound Calling | `POST /calls/stock-team` (live report > `stock_team_outcomes.json`) |
| Shift API | `POST /bookings/{booking_id}/shift` + `GET /properties/{pid}/alternates` |
| CRM / Ticket Update + Call Disposition | `POST /crm/dispositions` (+ `GET` to inspect) |
| IVR Transfer API | `POST /ivr/transfer` |
| Property backend (occupancy / status / 7-day ARR + complimentary amount) | `GET /properties/{pid}/occupancy` · `/status` · `/pricing?booking_id=` |
| Outcome reporting (PM/Stock bots) | `POST /verification-reports?channel=pm|stock&outcome=honored|not_honored` (+ `GET`) |

The PM bot reports through **one connection per outcome path** (`OYO PM Report Honored — ARR
Pitch`, `… Denied — Overbooked`, …), each pinning `deny_reason` + `resolution` in its
`bodyTemplate`. Workflow api nodes can only send slots, never constants — so without this the
reason was lost whenever the manager named it inside the denial sentence, and the customer bot
replayed an overbooking as a generic price denial. See `setup/05_pm_report_context.py`.

### Demo bookings (`data/bookings.json`)

| Booking | Guest | Scenario it demonstrates |
|---|---|---|
| 601001 | Rahul Sharma | confirmed; PM confirms; voucher w/ email on file; details Q&A |
| 601002 | Priya Verma | cancelled by system → dispute → transfer to agent |
| 601013 | Nisha Reddy | cancelled by customer → polite close |
| 601003 | Arjun Mehta | PM no answer → stock team confirms |
| 601004 | Sneha Iyer | overbooked (occupancy really 100%) → shift |
| 601005 | Vikram Singh | "overbooked" but inventory available → penalty advisory → honored |
| 601006 | Ananya Das | maintenance + alternate room → honored |
| 601007 | Rohan Kapoor | maintenance, no room → shift offer |
| 601008 | Meera Nair | price denial, rate ≥ 7-day ARR → honored after ARR pitch |
| 601009 | Aditya Rao | price denial, rate < ARR → complimentary amount → honored |
| 601010 | Kavita Joshi | price denial, refuses even compensation → shift |
| 601011 | Sanjay Gupta | PM no answer + stock unavailable → shift |
| 601012 | Farhan Ali | no email on file (voucher asks for one); PM-bot live-report demo |

Verification passes with the booking ID plus a matching guest name / registered
phone / hotel name / check-in date (`data/customers.json`).

## Bot configuration highlights

- **Workflows** (one per bot, status approved): `oyo_booking_support_journey`
  (~55 nodes: verification → status branch → requirement hub → PM orchestration →
  denial branches → stock team → shift → disposition), `oyo_property_verification_journey`
  (deny-reason classification + occupancy/ARR/complimentary negotiation + outcome
  reporting), `oyo_stock_validation_journey`.
- **Intents**: in-scope intents route `workflow:<slug>` (threshold 0.05);
  cancellations / refunds / new bookings / complaints route `handoff`
  (transfer back to the IVR); booking-detail *follow-up questions* deliberately
  fall through to the LLM, which answers from the runtime-context facts.
- **Prompts**: published system prompts (full mode — Kartik / Amit personas,
  scope, verification-first, no invented facts, en/hi) + greeting variants
  (en-IN + hi-IN). Outbound bots speak the greeting first.
- **Voice**: Deepgram Flux STT (`flux-general-multi`), ElevenLabs `eleven_flash_v2_5`
  (Aarav / Niraj / Viraj), `gpt-5-mini`, en-IN default + hi-IN.
- **API connections** (17, tenant-wide): verification and every state-changing
  action (voucher, PM/stock call, shift) are `is_state_changing` +
  `require_confirmation`, so the tool executor blocks them until the workflow's
  verify step has set `customer_verified` — spec Flow 2's "no info without
  verification" enforced at the tool layer too.
- **Runtime context** (customer bot): manual test payload = booking 601001 facts,
  used by the Testing Studio LLM turns for booking-detail Q&A.

## Testing

```
env/bin/python oyo/tests/chat.py customer --trace         # interactive, one bot
env/bin/python oyo/tests/run_chat_scenarios.py            # all 34 scenarios
env/bin/python oyo/tests/run_chat_scenarios.py "06 "      # one scenario
env/bin/python oyo/tests/run_chat_scenarios.py "E1"       # one cross-bot flow
```

All 34 pass: customer flows 1–20, PM flows 21–28, stock flows 29–30, and cross-bot
flows E1–E4 (which assert that an outbound bot's conversation changes what the
customer bot says).
The suite logs in as `oyo.config@oyo.com` (tenant-admin service account created
for this configuration).

## Notes / conventions discovered

- Workflow replies never interpolate `{slots}` — node texts are authored
  placeholder-free; dynamic facts are spoken by the LLM from runtime context.
- Router samples match as **substrings** — never use samples like "hi"/"yes"
  (they match inside "which"/"yesterday").
- Intent-node edge tokens: longest-contained-token wins, so denial tokens must
  outlength affirmative substrings ("we cannot" beats "we can"); tokens ending
  in `?` let question-phrased utterances advance.
- Equal-score question-signal edges resolve in AUTHORED ORDER — at the hubs the
  details edge is deliberately first, so a question no literal token catches
  ("what is my checking date?") exits to LLM Q&A over the verified facts
  instead of starting the property-verification call.
- `.env` gained `API_CONNECT_ALLOWED_HOSTS=127.0.0.1,localhost` so tenant API
  connections may call this local mock through the SSRF guard.
