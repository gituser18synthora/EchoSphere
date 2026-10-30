# Frankfinn Seminar Booking bot

Outbound Hinglish admissions bot for **Frankfinn Institute of Air Hostess
Training** (tenant `tn_6553beac240d`). It calls students who showed interest
in aviation/hospitality/travel/customer-service careers, checks eligibility,
pitches the FREE 45-minute career counselling seminar, books the seat, and
confirms the appointment SMS + Aadhaar entry mandate.

**Source material** (`/var/www/html/python/EchoSphere/Frankfinn/`):

- `Quality Call Flow_.docx` — the approved script: opening & brand name,
  reason of call, eligibility check (DOB/qualification/location, 12th-pass
  mandate, final-year probe for 3rd-year students), need creation, course
  duration (11 months for 12th-pass/UG, 8 months for graduates), seats/time,
  parents invitation (scholarship up to ₹40,000), affirmation, address/SMS
  confirmation, govt-ID mandate, closing.
- `C44989190.wav` + `Priya3260820120546.xml` — a real 8½-minute reference
  call (FIVT_AHMEDABAD campaign, disposition APPointment). Transcribed with
  Whisper; it contributed the final-year probe, the fixed entry window
  (10:15–11:30, seminar starts 11:40), non-cancellable/non-transferable seat
  policy, "no entry without Aadhaar", the C G Road centre (3rd floor, near
  Mocha Cafe), the SMS-receipt confirmation ritual and the ₹2,47,000 highest
  salary / first-come-first-serve scholarship wording.

## Key IDs

| Thing | Value |
|---|---|
| Tenant | `tn_6553beac240d` Frankfinn Institute (industry `education`) |
| Bot | `bot_059e49443c76` "Frankfinn Seminar Booking" (published) |
| Workflow | `wf_9f3b1b6928cc` "Frankfinn seminar booking journey" (approved) |
| Guardrail profile | `education_counselling` (`gp_60ac7e16e678`) |
| Tool | "Frankfinn Book Seminar Seat" (POST, state-changing) |
| KB | "Frankfinn Seminar & Courses FAQ" (bot-scoped, indexed) |
| Voice channel | +911246026010 · freeswitch (enabled) |
| Service account | frankfinn.config@frankfinn.com / Demo@2026! |

## Setup (idempotent, in order)

```
env/bin/python frankfinn/setup/00_tenant_governance.py   # SUPER ADMIN
env/bin/python frankfinn/setup/01_bot_entities_connections.py
env/bin/python frankfinn/setup/02_prompts.py
env/bin/python frankfinn/setup/03_workflow.py
env/bin/python frankfinn/setup/04_intents_context_runtime.py
env/bin/python frankfinn/setup/05_go_live.py             # kb/channel/scenarios/readiness/publish/activate
```

Regression suite (16 scenarios over `POST /bots/{id}/testing/simulate`):

```
env/bin/python frankfinn/tests/run_chat_scenarios.py
```

## Design decisions

- **No mock service** (project constraint). The single tool "Frankfinn Book
  Seminar Seat" points at Frankfinn's CRM using the reserved `.example` TLD:
  it can never resolve, so live calls deterministically take the workflow's
  `failure` edge — a graceful "your SMS with the appointment number is on
  its way, else call 1800 258 7332" fallback. The full response contract AND
  the sample payload live inside the connection's `responseSchema.example`;
  the test suite reads that sample back and replays it through the
  platform's own `mockToolResults` mechanism, so the success path (grounded
  confirmation speaking the real appointment number) is fully exercised.
  When Frankfinn provides the real CRM endpoint, swap `url` (+ auth) — no
  other change needed.
- **Guardrails**: the tenant's `standard` profile only carried
  profanity_deescalation. Created `education_counselling` =
  profanity_deescalation + **payment_collection_restriction** (the bot
  discusses scholarships/fees context but must NEVER solicit card/CVV/OTP/
  PIN — the seminar is free and no payment ever happens on a call), on top
  of the 4 always-on mandatory guardrails (pii_redaction,
  secret_leakage_prevention, unsafe_tool_call_block,
  prompt_injection_protection). booking_commitment_restriction was
  deliberately excluded — this bot's whole job is confirming seat bookings.
- **Entry mechanics**: an affirmative first response ("haan bol raha hoon")
  answers the greeting, so the opening hub speaks the reason-of-call as its
  prompt; refusal/callback/wrong-number/agent utterances ARE consumed at
  entry and branch immediately. Qualification is an intent hub (not an ask):
  a node reached after an ask resume sees no entry text, so branching
  questions must own their prompt.
- **Do-not-call** is intercepted platform-level (`detect_do_not_call` →
  route `call_control`, number marked DNC); the workflow's DNC edges remain
  as fallback for phrasings the detector misses.
- **Objection etiquette**: exactly one soft counter per decline point; the
  second decline reaches a polite close with the helpline. Handover node
  (`senior_counsellor` queue) reachable from every main hub.
- **Greeting personalization**: `{customer_name}` / `{voice_speaker_name}`
  resolve from the dialer/campaign `variables` (same contract as the
  telephony webhook); unresolved placeholders are stripped before TTS.
- Voice: Sarvam saaras:v3 STT (auto language detect) + bulbul:v3 TTS voice
  `vp-sv-priya`, hi-IN default + en-IN, gpt-4o-mini orchestration,
  time-context enabled (the script books "kal"), tenant tz Asia/Kolkata,
  turn detection "recommended".

## Verified

- `frankfinn/tests/run_chat_scenarios.py` — 16/16 (also re-run after KB
  indexing, since KB presence flips question routing).
- Platform scenario suite recorded and passing; readiness 7/7; bot
  published; voice channel activated.
- Live WS smoke (`ws://127.0.0.1:9002/ws/voice/{session}`): session_config
  (hi-IN, Sarvam Priya), personalized greeting spoken, ~200 KB real TTS
  audio streamed.
