# OYO Bots — Testing & Usage Guide

Verified against the live configuration on **2026-08-17**. Automated suite: **34/34 pass**
(`env/bin/python oyo/tests/run_chat_scenarios.py`).

Tenant `tn_de5cc992b1e9` · service login `oyo.config@oyo.com` / `Demo@2026!`

| Bot | ID | Workflow (slug) | Voice |
|---|---|---|---|
| OYO Booking Support (inbound customer) | `bot_e8cf0b05bb79` | `oyo_booking_support_journey` — 56 nodes | Aarav |
| OYO Property Verification (outbound → PM) | `bot_99177674902a` | `oyo_property_verification_journey` — 39 nodes | Niraj |
| OYO Stock Team Validation (outbound → stock) | `bot_78b6aa83d94a` | `oyo_stock_validation_journey` — 9 nodes | Viraj |

All three: Deepgram `flux-general-multi` STT · ElevenLabs `eleven_flash_v2_5` TTS ·
OpenAI `gpt-5-mini` · languages en-IN + hi-IN.

---

## 1. Services and startup order

| Order | Service | Port | Needed for | Start |
|---|---|---|---|---|
| 1 | MySQL | 3306 | all config | system service (already running) |
| 2 | Redis | 6379 | multi-turn session state | system service (already running) |
| 3 | Backend API | 9001 | bots, `/testing/chat` | `env/bin/uvicorn backend.main:app --port 9001` |
| 4 | **OYO mock API** | **9021** | every OYO integration | `./oyo/run.sh` |
| 5 | Frontend (Studio) | 5199 | UI testing | `node node_modules/vite/bin/vite.js` |
| 6 | Voice worker | 9002 | live voice calls only | `env/bin/uvicorn voice_runtime.app:app --port 9002` |

Steps 1–4 are the minimum for text testing. `./scripts/dev.sh start|status|stop` manages
3, 5, 6 together.

**The mock must be up before the bots run** — every workflow API node calls it, and a
failed call routes down the failure edge.

### Health checks

```bash
redis-cli ping                                    # PONG
curl -s http://127.0.0.1:9001/api/health          # {"status":"up"}
curl -s http://127.0.0.1:9021/api/v1/health       # {"status":"ok","service":"oyo-mock"}
```

### Stop / restart the mock

```bash
pkill -f "oyo.api.main"                           # stop
./oyo/run.sh &                                    # start
rm -f oyo/data/runtime_state.json                 # reset recorded reports/dispositions
```

---

## 2. How to talk to a bot

### Interactive CLI (easiest)

```bash
env/bin/python oyo/tests/chat.py customer --trace   # OYO Booking Support
env/bin/python oyo/tests/chat.py pm --trace         # OYO Property Verification
env/bin/python oyo/tests/chat.py stock --trace      # OYO Stock Team Validation
```

`--trace` prints route, workflow status, node trace and slots after each turn.
In-session commands: `/trace` `/new` `/slots` `/quit`.

### Raw HTTP

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:9001/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"oyo.config@oyo.com","password":"Demo@2026!"}' | jq -r .data.token)

curl -s -X POST http://127.0.0.1:9001/api/v1/bots/bot_e8cf0b05bb79/testing/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"confirm my booking","sessionId":"demo1"}' \
  | jq '{reply:.data.reply, route:.data.route, status:.data.workflow.status,
         nodes:.data.workflow.nodeTrace, slots:.data.workflow.slots}'
```

Reuse the same `sessionId` for every turn of one conversation. A new `sessionId`
starts a fresh call.

### Studio UI (and live voice)

`http://127.0.0.1:5199/t/bots/<botId>/testing` — e.g.
`http://127.0.0.1:5199/t/bots/bot_e8cf0b05bb79/testing`.
Other tabs: `/prompts`, `/voice`, `/intents`, `/apis`, `/workflows`.
Live voice testing needs the voice worker (9002).

---

## 3. Bot 1 — OYO Booking Support (`bot_e8cf0b05bb79`)

Inbound customer bot for the IVR "upcoming booking" queue. Covers spec Flows 1–6, 8, 9:
intent identification, customer verification, booking confirmation, booking details,
booking voucher, check-in confirmation (PM + stock orchestration), hotel shift, and the
closing CRM disposition.

**Every flow opens with the same three turns** (the verification gate — spec Flow 2):

| Say | Expect |
|---|---|
| `confirm my booking` | "I can help you with that. Could you please share your booking ID?" |
| `601001` | "Thank you. For verification, may I know the guest name on this booking?" |
| `Rahul Sharma` | "Great news — your booking is confirmed in our system. Would you like me to also confirm it directly with the property, hear your booking details, or get the booking voucher emailed to you?" |

APIs fired: **Customer Verification** → **Booking Details**.
Node trace: `n_ask_name → n_api_verify → n_api_booking → n_cond_confirmed → n_hub`.
Slots now hold `customer_verified=true`, `booking_status`, `hotel_name`, `property_id`,
`amount_pending`, `guest_email`, …

Below, only the turns **after** verification are listed. Substitute the booking ID and
guest name from the table in §6.

### C1 — Confirmed, system check only (601001 / Rahul Sharma)

| Say | Expect |
|---|---|
| `no, that's all, thank you` | "Perfect. Your booking is confirmed in our system, and you can proceed with your check-in without any issues." + "Thank you for calling OYO. Have a great day!" · `done=true` |

Fires **Call Disposition**. Verify: `curl -s http://127.0.0.1:9021/api/v1/crm/dispositions | jq '.dispositions[-1]'`

### C2 — Cancelled booking, customer disputes it → transfer (601002 / Priya Verma)

| Say | Expect |
|---|---|
| *(verification turn 3)* | "I've checked, and I'm sorry to share that this booking is showing as cancelled in our system… Did you cancel this booking yourself?" |
| `no, I never cancelled this booking` | "…let me transfer you to a support executive who can investigate the cancellation right away." + "Transferring you now — please stay on the line." · `workflow.status=handoff` |

Fires **IVR Transfer**. Verify: `curl -s http://127.0.0.1:9021/api/v1/ivr/transfer` records, and
`workflow.status == "handoff"` in the response.

### C3 — Cancelled by the customer themselves (601013 / Nisha Reddy)

| Say | Expect |
|---|---|
| `yes, I cancelled it myself` | "Understood. Since this booking stands cancelled, there is nothing pending on it…" · `done=true` |

### C4 — Property manager confirms (601001 / Rahul Sharma)

| Say | Expect |
|---|---|
| `please confirm with the property` | "Certainly. Let me quickly connect with the property… Please stay on the line while I check." + "Thank you for waiting. I have successfully confirmed your booking with the property. Your reservation is secured…" · `done=true` |

Fires **PM Verification Call** → **Call Disposition**.
Verify slots: `pm_call_status=completed`, `pm_booking_honored=true`, `pm_resolution=confirmed`.

### C5 — PM unreachable → stock team confirms (601003 / Arjun Mehta)

| Say | Expect |
|---|---|
| `yes please check with the property` | "I was unable to reach the property manager at this time. Let me quickly validate your booking with our internal team instead…" + "…Our internal team has validated your reservation — your booking will be honored at check-in…" · `done=true` |

Fires **PM Verification Call** (`no_answer`) → **Stock Team Call** (`confirmed`) → **Call Disposition**.

### C6 — Overbooked → shift accepted (601004 / Sneha Iyer)

| Say | Expect |
|---|---|
| `confirm with the property please` | "…the property is currently overbooked due to high demand and is unable to accommodate your reservation." + "Would you like me to help arrange an alternate OYO property nearby with similar amenities?" |
| `yes please` | "I have found alternative OYO properties nearby… Shall I proceed with shifting your booking?" |
| `yes, go ahead` | "Done! I've initiated the shift of your booking to a nearby OYO property…" · `done=true` |

Fires **PM Verification Call** → **Alternate Properties** → **Shift Booking** → **Call Disposition**.
Verify: `curl -s http://127.0.0.1:9021/api/v1/crm/dispositions | jq '.dispositions[-1].call_state.shift_status'` → `shift_initiated`.

### C7 — "Overbooked" but inventory exists → PM honors after penalty advisory (601005 / Vikram Singh)

| Say | Expect |
|---|---|
| `please verify with the property` | "…I have successfully confirmed your booking with the property…" · `done=true` |

The property-side penalty advisory happens inside the PM flow; the customer only hears the
outcome. Slots: `pm_deny_reason=overbooked`, `pm_resolution=penalty_warning_accepted`.

### C8 — Maintenance, alternate room arranged (601006 / Ananya Das)

| Say | Expect |
|---|---|
| `confirm with the property` | "Good news! The property has arranged an alternate room for your stay, and your booking has been confirmed…" · `done=true` |

### C9 — Maintenance, no room → shift declined (601007 / Rohan Kapoor)

| Say | Expect |
|---|---|
| `please confirm with the property` | "…The property is currently undergoing maintenance and is unable to accommodate your booking." + shift offer |
| `no, don't shift` | "I understand. You may visit the property as planned, and if you face any issue during check-in, please contact OYO support right away…" · `done=true` |

### C10 — Price denial, rate ≥ 7-day ARR → honored (601008 / Meera Nair)

| Say | Expect |
|---|---|
| `confirm with the property` | "…I have successfully confirmed your booking with the property…" · `done=true` |

Slots: `pm_deny_reason=price_low`, `pm_resolution=arr_pitch_accepted`.

### C11 — Price below ARR → complimentary amount → honored (601009 / Aditya Rao)

| Say | Expect |
|---|---|
| `confirm with the property` | "Good news! Your booking has been successfully confirmed with the property…" · `done=true` |

Slots: `pm_resolution=compensation_added`.

### C12 — Price refused even with compensation → shift (601010 / Kavita Joshi)

| Say | Expect |
|---|---|
| `please confirm with the property` | "I sincerely apologize — despite our best efforts, the property is unable to accommodate this reservation." + shift offer |
| `yes` → `yes, proceed` | shift confirmation → "Done! I've initiated the shift…" · `done=true` |

### C13 — PM unreachable **and** stock team unavailable → shift (601011 / Sanjay Gupta)

| Say | Expect |
|---|---|
| `confirm with the property` | "I was unable to reach the property manager…" + "I'm sorry — I could not get a confirmation from the property or our internal team at this moment." + shift offer |
| `yes please` → `okay` | shift confirmation → "Done! I've initiated the shift…" · `done=true` |

### C14 — Voucher to the email on file (601001 / Rahul Sharma)

| Say | Expect |
|---|---|
| `send me the voucher` | "I have the email address from your booking on file. Shall I send the booking voucher there?" |
| `yes please` | "Done! I've emailed your booking voucher…" + "Is there anything else I can help you with today?" |
| `no thanks` | closing · `done=true` |

Fires **Booking Voucher**. Verify: `curl -s http://127.0.0.1:9021/api/v1/crm/dispositions | jq '.dispositions[-1].call_state.voucher_email'`.

### C15 — Voucher with no email on file (601012 / Farhan Ali)

| Say | Expect |
|---|---|
| `email the voucher` | "Sure — could you please tell me the email address where I should send the voucher?" |
| `farhan.ali@example.com` | "Done! I've emailed your booking voucher…" |

### C16 — Booking details → LLM answers from verified facts (601001)

| Say | Expect |
|---|---|
| `booking details please` | "Of course — I have your verified booking right here. Ask me anything about it…" · `done=true` |
| `when is my check-in and which hotel is it?` | `route=chat` — the LLM answers from the bot's runtime-context fact set (Studio → the bot's runtime context), e.g. "Your booking is at OYO Townhouse 121 Sector 29 Gurugram…" |

> This turn is answered from the **runtime-context fact set**, not from the mock booking.
> See §8 for the current mismatch between the two.

### C17 — Verification failure — nothing disclosed (601001 + wrong name)

| Say | Expect |
|---|---|
| `Amit Kumar` *(as verification turn 3)* | "I'm sorry, I could not verify these details against the booking, so I can't share any booking information on this call…" · `status=handoff` |

### C18 — Unknown booking ID (`999999`)

Same outcome as C17: verification fails, no data disclosed, transfer to support.

### C19/C20 — Out of scope → back to the IVR

| Say | Expect |
|---|---|
| `I want to cancel my booking` | `route=handoff` — transfer reply, no booking flow |
| `where is my refund` | `route=handoff` |

Also out of scope: new bookings, complaints/escalations.

---

## 4. Bot 2 — OYO Property Verification (`bot_99177674902a`)

Outbound bot ("Amit") that calls the property manager. It speaks first, so you play the
**property manager**. Covers the Property Verification Flow of the script document,
including all three denial scenarios, and reports the outcome to the backend.

Opening turns for every case:

| Say | Expect |
|---|---|
| `hello` | "Could you please confirm the booking ID for the reservation, as shown in your OYO manager app?" |
| `601009` | "Thank you. Could you please confirm whether this booking will be honored for check-in?" |

### P1 — PM confirms immediately (601012)

| Say | Expect |
|---|---|
| `yes, the booking is confirmed` | "Thank you for confirming — the guest will proceed with check-in as planned…" · `done=true` |

Reports `honored / confirmed`.

### P2 — "Overbooked" but inventory available → penalty advisory → accepts (601005)

| Say | Expect |
|---|---|
| `we cannot honor this booking` | "I understand. Could you please help me understand the reason for declining the booking?" |
| `the property is overbooked` | "I'd like to flag one thing: our records show available inventory at your property for these dates. Please note that denying a valid booking despite availability can lead to penalties under your agreement with OYO." + "Considering this, would you be able to honor the booking?" |
| `okay, we will honor the booking` | "Thank you for confirming…" · `done=true` |

Fires **Property Occupancy** (P105: `has_availability=true`) → **PM Report Honored — Penalty Advisory**.

### P3 — Genuinely overbooked (601004, property P104 at 100%)

| Say | Expect |
|---|---|
| `no, we are overbooked` | "I understand. We will arrange an alternate stay for the guest…" · `done=true` |

Stating the reason in the denial sentence skips the "ask reason" turn — the flow goes
straight to the occupancy check, finds no availability, and reports
`not_honored / overbooked`.

### P4 — Maintenance, no alternate room (601007)

| Say | Expect |
|---|---|
| `we cannot honor it` | reason question |
| `the property is under maintenance` | "I'm sorry to hear about the maintenance. Do you have any alternate rooms available that could accommodate this booking?" |
| `no, nothing available` | "…We will arrange an alternate stay for the guest…" · `done=true` |

Reports `not_honored / maintenance`.

### P5 — Maintenance with an alternate room (601006)

| Say | Expect |
|---|---|
| `cannot honor, maintenance work is going on` | alternate-room question (reason recognised inline) |
| `yes, we can arrange another room` | "Thank you for confirming…" · `done=true` |

Reports `honored / alternate_room`.

### P6 — Price too low, rate meets ARR (601008)

| Say | Expect |
|---|---|
| `no, the booking price is too low` | "I checked our records — this booking actually meets or exceeds your average realized rate over the last seven days. We request you to honor the booking to avoid potential penalties…" + "Can we count on you to honor this booking?" |
| `okay, we will accept the booking` | "Thank you for confirming…" · `done=true` |

Fires **Property Pricing** (P108: booking_rate 1950 ≥ arr_7day 1800) → **Report Honored — ARR Pitch**.

### P7 — Price too low, below ARR → complimentary amount (601009)

| Say | Expect |
|---|---|
| `we cannot accommodate, price is very low` | reason question |
| `the booking price is too low` | "I understand the concern on the rate. To bridge the gap, OYO can add a complimentary compensation amount to this reservation from our side." + "With this additional compensation added, would you be willing to honor the booking?" |
| `yes, that works for us` | "Excellent — I've added the complimentary amount to this booking on OYO's side." + "Thank you for confirming…" · `done=true` |

Fires **Property Pricing** (P109: 1400 < 1750 → `rate_vs_arr=below`, `complimentary_amount=350`)
→ **Add Complimentary Amount** → **Report Honored — Compensation**.
Verify slots: `comp_added=true`, `comp_amount=350`.

### P8 — Price refused even with compensation (601010)

| Say | Expect |
|---|---|
| `no, we cannot take this booking` → `the price is too low` | compensation offer |
| `no, we cannot accept this rate` | "…We will arrange an alternate stay for the guest…" · `done=true` |

Reports `not_honored / price_low`.

---

## 5. Bot 3 — OYO Stock Team Validation (`bot_78b6aa83d94a`)

Short internal outbound call used when the PM is unreachable or did not confirm
(spec Flow 7). You play the **stock team**.

| Say | Expect |
|---|---|
| `hello` | "Could you please confirm the booking ID that needs validation?" |
| `601011` | "The property has not confirmed this booking so far. Could you check whether this booking can be honored at check-in?" |

### S1 — Stock confirms

| Say | Expect |
|---|---|
| `yes, the booking will be honoured` | "Perfect, thank you for validating. We'll inform the guest that the booking stands confirmed…" · `done=true` |

Reports `channel=stock / honored`.

### S2 — Stock cannot confirm

| Say | Expect |
|---|---|
| `no, we cannot confirm, no inventory` | "Understood. We'll proceed with offering the guest an alternate property…" · `done=true` |

Reports `channel=stock / not_honored`.

---

## 6. End-to-end meta-bot flows

The outbound bots and the customer bot are joined by the **verification report** in the
shared backend: when a live report exists for a booking, it **overrides** the scripted
outcome in `pm_call_outcomes.json` / `stock_team_outcomes.json`. That is what makes a real
PM conversation change what the customer hears.

**Always reset first** so stale reports don't leak between runs:

```bash
rm -f oyo/data/runtime_state.json
```

### E1 — Customer → PM bot → report → customer (601004)

```bash
rm -f oyo/data/runtime_state.json
# 1. Play the property manager
printf 'hello\n601004\nno, we are overbooked\nthe property is fully overbooked, no rooms\n/quit\n' \
  | env/bin/python oyo/tests/chat.py pm
# 2. Check what was reported
curl -s http://127.0.0.1:9021/api/v1/verification-reports \
  | jq '.reports[-1] | {channel, outcome, deny_reason, resolution}'
#    → {"channel":"pm","outcome":"not_honored","deny_reason":"overbooked","resolution":"not_honored"}
# 3. Now play the customer
printf 'confirm my booking\n601004\nSneha Iyer\nplease confirm with the property\n/quit\n' \
  | env/bin/python oyo/tests/chat.py customer --trace
```

Expected: the customer bot says the property is **overbooked** (the reason the PM actually
gave) and offers an alternate property. Slots show `pm_outcome_source=live_report`.

### E2 — A PM conversation *saves* a booking that was scripted to fail (601010)

```bash
rm -f oyo/data/runtime_state.json
printf 'hello\n601010\nwe are overbooked\nokay, we will honor the booking\n/quit\n' \
  | env/bin/python oyo/tests/chat.py pm
printf 'confirm my booking\n601010\nKavita Joshi\nplease confirm with the property\n/quit\n' \
  | env/bin/python oyo/tests/chat.py customer --trace
```

601010's scripted outcome is a refusal, but the live report says
`honored / penalty_warning_accepted`, so the customer now hears
"I have successfully confirmed your booking with the property."

### E3 — PM unreachable → stock bot confirms → customer confirmed (601003)

```bash
rm -f oyo/data/runtime_state.json
printf 'hello\n601003\nyes, the booking will be honoured\n/quit\n' \
  | env/bin/python oyo/tests/chat.py stock
printf 'check-in confirmation\n601003\nArjun Mehta\nconfirm with the property\n/quit\n' \
  | env/bin/python oyo/tests/chat.py customer --trace
```

Expected: "unable to reach the property manager" → "Our internal team has validated your
reservation". Slots: `pm_call_status=no_answer`, `stock_status=confirmed`.

### E4 — PM unreachable → stock cannot confirm → shift offered (601011)

```bash
rm -f oyo/data/runtime_state.json
printf 'hello\n601011\nno, we cannot confirm, no inventory\n/quit\n' \
  | env/bin/python oyo/tests/chat.py stock
printf 'check-in confirmation\n601011\nSanjay Gupta\nconfirm with the property\n/quit\n' \
  | env/bin/python oyo/tests/chat.py customer --trace
```

All four are automated as scenarios `E1`–`E4` in the suite.

---

## 7. Mock API reference

```
oyo/
  api/main.py     FastAPI app — reads oyo/data/*.json, no hardcoded payloads
  data/           static scenario data + runtime_state.json (written at runtime)
  setup/          the scripts that configured the tenant (idempotent, rerunnable)
  tests/          chat.py (interactive) · run_chat_scenarios.py (34 scenarios)
  run.sh          starts the service on 127.0.0.1:9021
```

Base URL `http://127.0.0.1:9021/api/v1`. Data files are re-read per request, so editing a
JSON file takes effect immediately — no restart.

| Endpoint | Payload | Returns | Source file |
|---|---|---|---|
| `GET /health` | — | `{status, service, at}` | — |
| `POST /customers/verify` | `{booking_id, guest_name?, caller_phone?, hotel_name?, checkin_date?}` | 200 `{verified:true, matched_on}` · 401 mismatch · 404 unknown | `customers.json` |
| `GET /bookings/{id}` | — | full booking record | `bookings.json` |
| `POST /bookings/{id}/voucher` | `{email?}` (falls back to the booking's email) | `{sent:true, voucher_id, email}` · 422 no valid address | `bookings.json` |
| `GET /properties/{pid}/occupancy` | — | `{total_rooms, occupied_rooms, occupancy_pct, has_availability}` | `properties.json` |
| `GET /properties/{pid}/status` | — | `{operational_status, under_maintenance, hold_reasons}` | `properties.json` |
| `GET /properties/{pid}/pricing?booking_id=` | — | `{arr_7day, complimentary_amount, booking_rate, rate_vs_arr}` | `properties.json` + `bookings.json` |
| `GET /properties/{pid}/alternates` | — | `{count, alternates[], top_alternate_name}` · 404 none | `properties.json` |
| `POST /bookings/{id}/complimentary` | `{}` | `{added:true, amount}` | `properties.json` |
| `POST /calls/property-manager` | `{booking_id}` | `{call_status, booking_honored, deny_reason, resolution, source}` | live report → `pm_call_outcomes.json` |
| `POST /calls/stock-team` | `{booking_id}` | `{call_status, stock_status, source}` | live report → `stock_team_outcomes.json` |
| `POST /verification-reports?channel=pm\|stock&outcome=honored\|not_honored` | `{booking_id, deny_reason?, resolution?}` | `{recorded:true, report_id}` | writes `runtime_state.json` |
| `GET /verification-reports` | — | `{reports[]}` | `runtime_state.json` |
| `POST /bookings/{id}/shift` | `{}` | `{shifted:true, shift_id, new_property_name}` · 409 no alternate | `properties.json` |
| `POST /crm/dispositions` | full call state | `{recorded:true, disposition_id}` | writes `runtime_state.json` |
| `GET /crm/dispositions` | — | `{dispositions[]}` | `runtime_state.json` |
| `POST /ivr/transfer` | `{queue?, booking_id?}` | `{transferred:true, transfer_id, queue}` | writes `runtime_state.json` |

### Ready-to-run examples

```bash
B=http://127.0.0.1:9021/api/v1

curl -s -X POST $B/customers/verify -H 'Content-Type: application/json' \
  -d '{"booking_id":"601001","guest_name":"Rahul Sharma"}' | jq
curl -s $B/bookings/601002 | jq '{booking_status, cancelled_on, cancelled_by}'
curl -s $B/properties/P104/occupancy | jq
curl -s "$B/properties/P109/pricing?booking_id=601009" | jq
curl -s -X POST $B/calls/property-manager -H 'Content-Type: application/json' \
  -d '{"booking_id":"601004"}' | jq
curl -s -X POST $B/bookings/601004/shift -H 'Content-Type: application/json' -d '{}' | jq
```

### Booking scenario map (`data/bookings.json`)

| Booking | Guest | Property | Scenario |
|---|---|---|---|
| 601001 | Rahul Sharma | P101 | confirmed · PM confirms · voucher with email · details Q&A |
| 601002 | Priya Verma | P102 | cancelled by system → dispute → transfer |
| 601003 | Arjun Mehta | P103 | PM no answer → stock team confirms |
| 601004 | Sneha Iyer | P104 (100% full) | genuinely overbooked → shift |
| 601005 | Vikram Singh | P105 (69%) | "overbooked" but available → penalty advisory → honored |
| 601006 | Ananya Das | P106 (maintenance) | maintenance + alternate room → honored |
| 601007 | Rohan Kapoor | P107 (maintenance) | maintenance, no room → shift |
| 601008 | Meera Nair | P108 (ARR 1800) | price denial, rate 1950 ≥ ARR → honored |
| 601009 | Aditya Rao | P109 (ARR 1750) | price denial, rate 1400 < ARR → +₹350 → honored |
| 601010 | Kavita Joshi | P110 | price denial, refuses compensation → shift |
| 601011 | Sanjay Gupta | P111 | PM no answer + stock unavailable → shift |
| 601012 | Farhan Ali | P101 | **no email on file** (voucher asks for one) |
| 601013 | Nisha Reddy | P107 | cancelled by the customer → polite close |
| 999999 | — | — | unknown booking → verification failure |

Verification accepts the booking ID plus **any one** matching detail: guest name,
registered phone, hotel name, or check-in date.

---

## 8. Verification and inspection

```bash
# What the outbound bots reported
curl -s http://127.0.0.1:9021/api/v1/verification-reports \
  | jq '.reports[] | {channel, outcome, booking_id, deny_reason, resolution}'

# CRM disposition of the last customer call (full slot state)
curl -s http://127.0.0.1:9021/api/v1/crm/dispositions | jq '.dispositions[-1]'

# Config in the DB
mysql -h 127.0.0.1 -P 3306 -u webuser -p'8hyjnx^' voice_bot -e "
SELECT name, version, status, JSON_LENGTH(nodes) nodes FROM workflows
WHERE tenant_id='tn_de5cc992b1e9' AND is_deleted=0;"
```

In every `/testing/chat` response check: `route` (`workflow` / `handoff` / `chat`),
`workflow.status` (`collecting` / `done` / `handoff`), `workflow.nodeTrace` (which nodes
ran), and `workflow.slots` (what the APIs returned).

### Automated suite

```bash
rm -f oyo/data/runtime_state.json
env/bin/python oyo/tests/run_chat_scenarios.py            # all 34
env/bin/python oyo/tests/run_chat_scenarios.py "06 "      # one scenario
env/bin/python oyo/tests/run_chat_scenarios.py "E1"       # one E2E flow
```

Expected: `34 passed, 0 failed`. The suite resets the mock state before each E2E flow and
leaves a clean slate at the end.

---

## 9. Known deviations and gotchas

**The runtime-context fact set no longer matches the mock booking.** The customer bot's
runtime context currently holds `booking_id 123456`, `mobile 8080813352`, check-in
2026-09-20 — while the mock's booking 601001 is 2026-08-20. The workflow uses the mock;
only free-form booking-detail questions (C16) use the runtime context, so the two answer
with different dates. Align them in Studio → the bot's runtime context, or with:

```bash
env/bin/python oyo/setup/04_intents_context.py     # resets it to booking 601001's facts
```

**Live reports beat scripted outcomes, and they persist.** Run order matters. Delete
`oyo/data/runtime_state.json` to get the scripted behaviour back.

**Workflow session state is in memory.** The Postgres LangGraph checkpointer is
unavailable on this machine, so the API falls back to an in-memory saver: restarting the
backend on 9001 loses in-flight conversations (finished ones are unaffected).

**Bots are in `draft` status.** That is fine for `/testing/chat` and Studio testing;
publish them from the Publish tab before any real traffic.

**Intent samples match as substrings** — never add a sample like `hi` or `yes` (they match
inside "which" / "yesterday"). Intent-node edge tokens pick the longest literal match, so
denial tokens must be longer than affirmative substrings ("we cannot" beats "we can").

**Sensitive data:** the mock's `.env` allowlist entry
`API_CONNECT_ALLOWED_HOSTS=127.0.0.1,localhost` is what lets the tenant's API connections
reach the local mock through the SSRF guard. Remove it when pointing the connections at
real OYO endpoints.

---

## 10. Clean-start checklist

```bash
cd /var/www/html/python/EchoSphere

# 1. infra
redis-cli ping                                     # PONG

# 2. backend API
env/bin/uvicorn backend.main:app --port 9001 &
curl -s http://127.0.0.1:9001/api/health

# 3. OYO mock
./oyo/run.sh &
curl -s http://127.0.0.1:9021/api/v1/health

# 4. clean slate
rm -f oyo/data/runtime_state.json

# 5. test a bot by hand
env/bin/python oyo/tests/chat.py customer --trace

# 6. full regression
env/bin/python oyo/tests/run_chat_scenarios.py

# 7. inspect what was recorded
curl -s http://127.0.0.1:9021/api/v1/verification-reports | jq '.reports | length'
curl -s http://127.0.0.1:9021/api/v1/crm/dispositions   | jq '.dispositions | length'

# 8. shut down
pkill -f "oyo.api.main"                            # mock
pkill -f "backend.main:app"                        # backend API (kills this shell's match too —
                                                   # prefer: kill <pid from ss -tlnp | grep 9001>)
```
