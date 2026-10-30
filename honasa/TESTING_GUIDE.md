# Honasa Customer Care Bot — Testing & Usage Guide

Verified against the live configuration on **2026-08-27**. Automated suite: **36/36 pass**
(`env/bin/python honasa/tests/run_chat_scenarios.py`).

Tenant `tn_620d5400d462` (industry `ecommerce`) · service login
`honasa.config@honasa.com` / `Demo@2026!`

| Item | Value |
|---|---|
| Bot | **Honasa Customer Care** — `bot_71194477c0eb` (published, readiness 7/7) |
| Workflow | `Honasa order support journey` — `wf_c449f1421055`, 45 nodes / 91 edges, approved |
| Guardrail profile | `ecommerce_support` (profanity de-escalation + card/OTP/PIN request block + 4 mandatory) |
| Voice | Sarvam `saaras:v3` STT (auto language detect) · Sarvam `bulbul:v3` TTS **Shreya** · `gpt-4o-mini` |
| Languages | **en-IN (default) + hi-IN** — reply language follows the caller |
| Scope | POC categories **1. Order/Information** + **2. Return/Replacement** only |

Out of scope by design: cancellation, general/product queries, CSAT. Cancellation and
unresolved-complaint requests transfer to a human; anything else gets a polite scope
decline.

---

## 1. Services and startup order

| Order | Service | Port | Needed for | Start |
|---|---|---|---|---|
| 1 | MySQL | 3306 | all config | system service (already running) |
| 2 | Redis | 6379 | multi-turn session state | system service (already running) |
| 3 | Backend API | 9001 | `/testing/chat`, Studio APIs | `env/bin/uvicorn backend.main:app --port 9001` |
| 4 | **Honasa mock API** | **9022** | every workflow API node | `./honasa/run.sh` |
| 5 | Frontend (Studio) | 5199 | UI testing | `node node_modules/vite/bin/vite.js` |
| 6 | Voice worker | 9002 | live voice calls only | `env/bin/uvicorn voice_runtime.app:app --port 9002` |
| 7 | Ingestion worker | — | only when re-indexing the KB | `env/bin/python -m backend.workers.ingestion` |

Steps 1–4 are the minimum for text testing. **The mock must be up before you talk to the
bot** — order lookup, tracking link, returns and escalations all call it live; a down mock
routes every API node down its failure edge.

### Health checks

```bash
redis-cli ping                                    # PONG
curl -s http://127.0.0.1:9001/api/health          # {"status":"up"}
curl -s http://127.0.0.1:9022/api/v1/health       # {"status":"ok","service":"honasa-mock"}
```

### Stop / restart / reset the mock

```bash
pkill -f "honasa.api.main"                        # stop
./honasa/run.sh &                                 # start
rm -f honasa/data/runtime_state.json              # reset recorded requests/escalations
```

---

## 2. How to talk to the bot

### Interactive CLI (easiest)

```bash
env/bin/python honasa/tests/chat.py --trace
```

`--trace` prints route, workflow status, node trace and slots after each turn.
Blank line or Ctrl-C exits.

### Raw HTTP

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:9001/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"honasa.config@honasa.com","password":"Demo@2026!"}' | jq -r .data.token)

curl -s -X POST http://127.0.0.1:9001/api/v1/bots/bot_71194477c0eb/testing/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"where is my order?","sessionId":"demo1"}' \
  | jq '{reply:.data.reply, route:.data.route, status:.data.workflow.status,
         nodes:.data.workflow.nodeTrace, slots:.data.workflow.slots}'
```

Reuse the same `sessionId` for every turn of one conversation; a new `sessionId` starts a
fresh call.

### Studio UI (and live voice)

`http://127.0.0.1:5199/t/bots/bot_71194477c0eb/testing` (login
`honasa.config@honasa.com` / `Demo@2026!`). Other tabs: `/prompts`, `/voice`, `/intents`,
`/apis`, `/workflows`. Live voice testing additionally needs the voice worker (9002) —
verified working: greeting speaks in Shreya's voice over the browser WS channel.

---

## 3. Test data map (`honasa/data/orders.json`)

Lookup accepts the **order ID** or the **registered mobile number** (most recent order on
that number wins). Dates are stored as day offsets and materialized at read time, so
"delivered 2 days ago" is always true.

| Order | Customer | Phone | Status | Use it to test |
|---|---|---|---|---|
| 7001001 | Rekha Nair | 9876501001 | delivered 2d ago | happy return · damaged · amount ₹698 · ₹50 discount |
| 7001002 | Arjun Patel | 9876501002 | shipped, ETA +2d | ETA · tracking link · ₹60 cashback |
| 7001003 | Sana Khan | 9876501003 | out for delivery | status today · phone lookup |
| 7001004 | Vikram Rao | 9876501004 | processing | **no ETA / tracking not live** (honest answers) |
| 7001005 | Meera Joshi | 9876501005 | delivered 12d ago | **return + quality windows closed** → agent |
| 7001006 | Rohit Sen | 9876501006 | delivered 3d ago | **refund in process** (₹499, credit ~+4d) |
| 7001007 | Divya Iyer | 9876501007 | delivered 1d ago, COD | no refund in process · defective/expired flow |
| 7001008 | Kabir Malhotra | 9876501008 | delivered 4d ago | **non-returnable category** (hygiene) |
| 7001009 | Anita Desai | 9876509999 | shipped, ETA +1d | phone with **two** orders — latest wins |
| 7001010 | Anita Desai | 9876509999 | delivered 10d ago | (older order on the same phone) |
| 7001011 | Farhan Sheikh | 9876501011 | delivered 2d ago | wrong-item flow |
| 7001012 | Priyanka Ghosh | 9876501012 | delivered 5d ago | 3 items — missing-item flow |
| 1234567 | — | — | — | unknown order → retry → escalation |

---

## 4. Bot test cases — English & Hindi

Every flow opens the same way (the lookup gate — every FAQ row starts here):

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `where is my order?` | `मेरा ऑर्डर कहाँ है` | "Sure — could you please share your order ID, or the mobile number registered with the order?" |
| `7001002` | `7001002` | "Thank you! … I've found your order …" + asks: order status & delivery details, or return/replacement help |

APIs fired: **Honasa Order Lookup**. Node trace: `n_ask_order → n_api_lookup → n_hub`.
Slots now hold `customer_verified=true`, `order_status`, `order_amount_inr`,
`refund_status`, `return_eligible`, …

Hindi turns get Hindi replies (generated — wording varies, meaning stays). Below, only
the turns **after** the lookup are listed unless noted.

### A. Order / Information

**A1 — Order status + ETA (7001002)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `when will it arrive?` | `डिलीवरी कब होगी?` | Status *shipped* + the expected delivery date, spoken as words ("…expected to arrive on the twenty-ninth of August") |

**A2 — Broad "where is my order" (7001003)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `where is my order right now?` | `मेरा ऑर्डर अभी कहाँ है?` | "out for delivery" + today's expected delivery |

**A3 — Order amount / discount / cashback (7001001, 7001002)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `what was the order amount?` | `ऑर्डर कितने का था?` | "six hundred ninety-eight rupees" (7001001) |
| `did I get any discount?` | `क्या मुझे डिस्काउंट मिला था?` | ₹50 discount confirmed (7001001) |
| `did I get cashback on my order?` | `कैशबैक मिला था क्या?` | ₹60 cashback (7001002) |

**A4 — Refund status (7001006 = in process, 7001007 = none)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `where is my refund?` | `मेरा रिफंड कहाँ है?` | 7001006: refund of ₹499 in process, expected credit date, original payment method |
| `what is my refund status?` | `रिफंड का स्टेटस क्या है?` | 7001007: "no refund is in process on this order" — never invented |

**A5 — Tracking link over WhatsApp (7001002 = live, 7001004 = not live)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `send me the tracking link` | `ट्रैकिंग लिंक भेज दो` | 7001002: "sent the tracking link on WhatsApp to your registered number" · fires **Honasa Send Tracking Link** |
| *(same on 7001004)* | *(same)* | "live tracking isn't available for this order yet — it becomes active once the order is shipped" |

Verify: `curl -s http://127.0.0.1:9022/api/v1/state | jq '.tracking_links[-1]'`

**A6 — ETA genuinely unavailable (7001004)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `when will it be delivered?` | `कब तक मिलेगा?` | Order is *processing*, the date is **not available yet** + offer of a support executive — the bot never guesses a date |

**A7 — Lookup by phone (at the order-ID question)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `my registered number is 9876501003` | `मेरा नंबर 9876501003 है` | Sana Khan's order 7001003 found |
| `9876509999` | `9876509999` | Latest order 7001009 picked (two orders on this number) |

**A8 — Spoken digits (voice-style dictation)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `seven zero zero one zero zero two` | `सात शून्य शून्य एक शून्य शून्य दो`* | Digits assembled → order 7001002 found (*Hindi digit words also normalize; Devanagari numerals work too) |

### B. Return / Replacement

**B1 — Change-of-mind return, eligible (7001001)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `I want to return my product` *(turn 1)* | `मुझे प्रोडक्ट रिटर्न करना है` | order-ID question |
| `7001001` | `7001001` | order found + options |
| `I just don't need it anymore` | `अब नहीं चाहिए` | "eligible for return under the seven-day policy — shall I raise the return request now?" |
| `yes please` | `हाँ, कर दो` | Return raised + **"return link will be shared over WhatsApp"** · fires **Honasa Return Request** |

Verify: `curl -s http://127.0.0.1:9022/api/v1/state | jq '.resolution_requests[-1]'` →
`issue_type=no_longer_needed, resolution=return, whatsapp_link_sent=true`.

**B2 — Return window closed (7001005) → agent**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `I don't need it anymore` | `अब नहीं चाहिए` | Not eligible — the seven-day window has closed + "connect you to a support executive?" |
| `yes, connect me` | `हाँ, कनेक्ट कर दो` | Escalation ticket + "Please stay on the line…" · `workflow.status=handoff` |

**B3 — Non-returnable category (7001008)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `can I return this product?` | `क्या मैं इसे रिटर्न कर सकता हूँ?` | Not returnable — hygiene category; policy explained, no exception promised |
| `no, it's okay` | `नहीं, ठीक है` | polite close |

**B4 — Eligibility question only (7001007)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `can I return this?` | `क्या ये रिटर्न हो सकता है?` | Yes — eligible, days left in the window stated from real facts |
| `no, I was just asking` | `नहीं, बस पूछ रहा था` | no return raised, conversation continues |

**B5 — Damaged product → replacement (7001011)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `I received a damaged product` *(turn 1)* | `मुझे खराब प्रोडक्ट मिला` | order-ID question |
| `7001011` | `7001011` | order found |
| `the face wash arrived damaged` | `प्रोडक्ट टूटा हुआ आया है` | apology + "which product was damaged and what does the damage look like?" |
| `the tube is torn and it leaked` | `ट्यूब फटी हुई है और लीक हो रही है` | "replacement, or return it for a refund?" |
| `replacement please` | `नया भेज दो` | Replacement raised + WhatsApp next-steps link · fires **Honasa Damaged Replacement** |

**B6 — Damaged product → refund (7001001)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| *(same path, then)* `I'd like a refund, return it` | `रिफंड चाहिए, पैसे वापस कर दो` | Return raised (issue `damaged`, resolution `return`) + WhatsApp link |

**B7 — Wrong item (7001011)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `I received the wrong product` *(turn 1)* | `मुझे गलत प्रोडक्ट मिला` | order-ID question → order found |
| `you sent the wrong item` | `गलत आइटम आया है` | "what did you receive, and what had you ordered?" |
| `got a serum instead of the face wash` | `फेस वॉश की जगह सीरम आ गया` | correct-product replacement or refund choice |
| `send the correct product` | `सही वाला भेज दो` | Replacement raised · fires **Honasa Wrong Item Replacement** |

**B8 — Missing / incomplete item (7001012)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `an item is missing from my order` *(turn 1)* | `ऑर्डर में आइटम मिसिंग है` | order-ID question → order found |
| `one item is not in the box` | `एक आइटम कम निकला` | "which item is missing?" |
| `the aloe vera gel is missing` | `एलोवेरा जेल नहीं आया` | "send the missing item, or refund for it?" |
| `please send the missing item` | `आइटम भेज दो` | Missing-item replacement raised · fires **Honasa Missing Item Replacement** |

**B9 — Defective / expired (7001007)**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `I received an expired product` *(turn 1)* | `प्रोडक्ट एक्सपायर हो गया है` | order-ID question → order found |
| `it is past its expiry date` | `एक्सपायरी डेट निकल गई है` | "which product, and not working or past expiry?" |
| `the moisturizer expired last month` | `मॉइस्चराइज़र पिछले महीने एक्सपायर हो गया` | replacement or refund choice |
| `replacement` | `रिप्लेसमेंट` | Replacement raised · fires **Honasa Defective Replacement** |

**B10 — Quality window closed (7001005, damaged 12d after delivery) → agent**

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| *(damaged path, then)* `replacement please` | `नया भेज दो` | API rejects (`quality_window_closed`) → "I couldn't raise this request… connect you to a support executive?" |
| `yes please connect me` | `हाँ, एजेंट से बात कराओ` | escalation + handover |

### C. Robustness

**C1 — Wrong then correct order ID**

| Say | Expect |
|---|---|
| `1234567` *(at the ID question)* | "couldn't find an order… double-check and share the order ID" |
| `sorry, it is 7001003` | order found, flow continues normally |

**C2 — Unknown ID twice → escalation**

| Say | Expect |
|---|---|
| `1234567` then `9999999` | "still unable to locate this order… connect you to a support executive?" → `yes` → ticket + handover |

**C3 — Request changes mid-call (7001001)**

| Say | Expect |
|---|---|
| `when was it delivered?` | delivered date answered |
| `actually, I want to return it` | straight into the return triage — no re-asking of the order ID |

**C4 — Off-script question mid-flow**

| Say | Expect |
|---|---|
| `which one is faster?` *(at the replacement/refund choice)* | LLM answers in context, the flow stays at the same step; `replacement` afterwards still completes |

**C5 — Repeated question** — ask `what is my order amount?` twice → same grounded answer
both times, no confusion.

### D. Guard rails / out of scope (must NOT enter the order workflow)

| Say (EN) | Say (HI) | Expect |
|---|---|---|
| `I want to cancel my order` | `ऑर्डर कैंसिल करना है` | `route=handoff` — transfer offer, cancellation flow does NOT exist |
| `my issue is not resolved, I want to complain` | `शिकायत दर्ज करनी है` | `route=handoff` |
| `please connect me to an agent` | `एजेंट से बात करनी है` | `route=handoff` |
| `can you recommend a good sunscreen?` | `कोई अच्छा सनस्क्रीन बताओ` | polite scope decline — order/return help offered instead |
| `what is your return policy?` | `रिटर्न पॉलिसी क्या है?` | answered from the knowledge base — "within 7 days of delivery…" |
| `I don't need a refund, just tell me where my order is` | — | stays in the ORDER flow (stray "refund" never hijacks routing) |
| share a card number / OTP | — | guardrail blocks the turn (`payment_collection_restriction` + PII redaction) |

---

## 5. Mock API reference (port 9022)

```
honasa/
  api/main.py     FastAPI app — reads honasa/data/orders.json, no hardcoded payloads
  data/           orders.json (day-offset dates) + runtime_state.json (written at runtime)
  setup/          00–05 configuration scripts (idempotent, rerunnable)
  tests/          chat.py (interactive) · run_chat_scenarios.py (36 scenarios)
  run.sh          starts the service on 127.0.0.1:9022
```

Base URL `http://127.0.0.1:9022/api/v1`. Data files are re-read per request — edits apply
without a restart.

| Endpoint | Method | Payload | Returns |
|---|---|---|---|
| `/health` | GET | — | `{status, service, at}` |
| `/orders/lookup` | POST | `{order_ref}` or `{order_ref2}` (retry wins) — order ID **or** 10-digit phone, whole utterances OK | 200 full flat order view (`verified:true`, status, ETA, amounts, refund, eligibility) · 404 unknown |
| `/orders/{id}/tracking-link` | POST | `{}` | 200 `{sent:true, channel:"whatsapp", whatsapp_number_masked}` · 409 tracking not live · 404 |
| `/orders/{id}/returns` | POST | `{issue_type, resolution, details?}` | 201-style `{created:true, request_id, whatsapp_link_sent:true, …}` · 409 ineligible/window closed/not delivered · 422 bad enum · 404 |
| `/orders/{id}/returns` | GET | — | `{requests[]}` for that order |
| `/support/escalations` | POST | `{order_id?, queue?, …any slot state}` | `{created:true, ticket_id, queue}` |
| `/state` | GET | — | full runtime state (requests, links, escalations) |

`issue_type` ∈ `no_longer_needed · damaged · wrong_item · missing_item · defective_expired`
· `resolution` ∈ `return · replacement`. Server-side rules: change-of-mind needs
`return_eligible` (delivered + returnable category + ≤7 days); quality issues need a
delivered order within 7 days of delivery.

### Ready-to-run curls (every POST with data)

```bash
B=http://127.0.0.1:9022/api/v1

# health
curl -s $B/health | jq

# lookup by order ID (utterance-style input also works)
curl -s -X POST $B/orders/lookup -H 'Content-Type: application/json' \
  -d '{"order_ref":"7001001"}' | jq
curl -s -X POST $B/orders/lookup -H 'Content-Type: application/json' \
  -d '{"order_ref":"my order id is 7001006"}' | jq '{order_id, refund_status, refund_amount_inr, refund_expected_by}'

# lookup by registered phone (latest of two orders wins)
curl -s -X POST $B/orders/lookup -H 'Content-Type: application/json' \
  -d '{"order_ref":"9876509999"}' | jq '{order_id, multiple_orders_on_phone, orders_on_phone}'

# retry semantics: order_ref2 beats a failed order_ref
curl -s -X POST $B/orders/lookup -H 'Content-Type: application/json' \
  -d '{"order_ref":"1234567","order_ref2":"7001003"}' | jq '{order_id, order_status}'

# unknown → 404
curl -s -X POST $B/orders/lookup -H 'Content-Type: application/json' \
  -d '{"order_ref":"9999999"}' | jq

# tracking link (7001002 = live → 200; 7001004 = processing → 409)
curl -s -X POST $B/orders/7001002/tracking-link -H 'Content-Type: application/json' -d '{}' | jq
curl -s -X POST $B/orders/7001004/tracking-link -H 'Content-Type: application/json' -d '{}' | jq

# change-of-mind return (eligible → created)
curl -s -X POST $B/orders/7001001/returns -H 'Content-Type: application/json' \
  -d '{"issue_type":"no_longer_needed","resolution":"return","details":"customer no longer needs it"}' | jq

# change-of-mind on a window-closed order → 409
curl -s -X POST $B/orders/7001005/returns -H 'Content-Type: application/json' \
  -d '{"issue_type":"no_longer_needed","resolution":"return"}' | jq

# damaged → replacement
curl -s -X POST $B/orders/7001011/returns -H 'Content-Type: application/json' \
  -d '{"issue_type":"damaged","resolution":"replacement","details":"tube torn and leaking"}' | jq

# wrong item → return with refund
curl -s -X POST $B/orders/7001011/returns -H 'Content-Type: application/json' \
  -d '{"issue_type":"wrong_item","resolution":"return","details":"received serum instead of face wash"}' | jq

# missing item → send the item
curl -s -X POST $B/orders/7001012/returns -H 'Content-Type: application/json' \
  -d '{"issue_type":"missing_item","resolution":"replacement","details":"aloe vera gel missing"}' | jq

# defective/expired → replacement
curl -s -X POST $B/orders/7001007/returns -H 'Content-Type: application/json' \
  -d '{"issue_type":"defective_expired","resolution":"replacement","details":"expired last month"}' | jq

# quality complaint outside the 7-day window → 409
curl -s -X POST $B/orders/7001005/returns -H 'Content-Type: application/json' \
  -d '{"issue_type":"damaged","resolution":"replacement","details":"crushed box"}' | jq

# support escalation
curl -s -X POST $B/support/escalations -H 'Content-Type: application/json' \
  -d '{"order_id":"7001005","queue":"customer_support","reason":"return window dispute"}' | jq

# inspect / reset everything recorded
curl -s $B/state | jq
curl -s $B/orders/7001001/returns | jq
rm -f honasa/data/runtime_state.json
```

### Platform API connections (Studio → APIs tab)

| Connection | Mock endpoint | Fired by |
|---|---|---|
| Honasa Order Lookup | `POST /orders/lookup` | first + retry lookup nodes (maps 28 fields → slots, sets `customer_verified`) |
| Honasa Send Tracking Link | `POST /orders/{order_id}/tracking-link` | "send the tracking link" branch |
| Honasa Return Request | `POST /orders/{order_id}/returns` (pins `no_longer_needed/return`) | change-of-mind confirm |
| Honasa Damaged Replacement / Damaged Return | 〃 (pins `damaged` + choice) | damaged path |
| Honasa Wrong Item Replacement / Return | 〃 (`wrong_item`) | wrong-item path |
| Honasa Missing Item Replacement / Return | 〃 (`missing_item`) | missing-item path |
| Honasa Defective Replacement / Return | 〃 (`defective_expired`) | defective/expired path |
| Honasa Support Escalation | `POST /support/escalations` | every agent-offer "yes" (works without a verified caller) |

State-changing connections require the caller to be verified (`customer_verified` from a
successful lookup) — the platform's tool gate blocks them otherwise.

---

## 6. Verification and inspection

```bash
# What the bot recorded on the mock
curl -s http://127.0.0.1:9022/api/v1/state \
  | jq '{returns: .resolution_requests, links: .tracking_links, tickets: .escalations}'

# Config in the DB
mysql -h 127.0.0.1 -P 3306 -u webuser -p'8hyjnx^' voice_bot -e "
SELECT name, version, status, JSON_LENGTH(nodes) nodes FROM workflows
WHERE tenant_id='tn_620d5400d462' AND is_deleted=0;"
```

In every `/testing/chat` response check: `route` (`workflow` / `chat` / `knowledge` /
`handoff`), `workflow.status` (`collecting` / `done` / `handoff`), `workflow.nodeTrace`,
and `workflow.slots` (what the lookup mapped).

> **Note — `route=chat` after the lookup is normal.** Once the order is verified, a fact
> question that names a verified fact ("what was the amount?") is answered by the LLM
> straight from the verified facts (the platform's verified-context route). Same facts,
> same grounding; the workflow stays paused and the next actionable turn resumes it.

### Automated suite

```bash
env/bin/python honasa/tests/run_chat_scenarios.py            # all 36
env/bin/python honasa/tests/run_chat_scenarios.py "11 "      # one scenario
```

Expected: `36 passed, 0 failed`. The suite resets the mock state at start and end.

---

## 7. Gotchas

- **Mock down = failure edges.** If lookups suddenly say "couldn't find", check
  `curl -s http://127.0.0.1:9022/api/v1/health` first.
- **Live requests persist** in `honasa/data/runtime_state.json` — delete it between manual
  demo runs for a clean slate.
- **One order per call.** The session keeps the first looked-up order's facts; for a
  different order the bot offers a fresh call/agent (by design — mapped slots never
  overwrite).
- **Workflow session state is in memory** — restarting the backend on 9001 loses
  in-flight conversations.
- **Grounded wording varies.** Fixed/failure lines are verbatim; success/fact lines are
  LLM-worded from verified facts, so assert meaning (the suite pins branches via
  nodeTrace/slots instead of exact phrasing).
- `API_CONNECT_ALLOWED_HOSTS=127.0.0.1,localhost` in `.env` is what lets the tenant's API
  connections reach the local mock through the SSRF guard — point the connections at real
  Honasa endpoints when they exist and remove the entry.

---

## 8. Clean-start checklist

```bash
cd /var/www/html/python/EchoSphere

redis-cli ping                                     # 1. infra — PONG
env/bin/uvicorn backend.main:app --port 9001 &     # 2. backend API
./honasa/run.sh &                                  # 3. Honasa mock
rm -f honasa/data/runtime_state.json               # 4. clean slate
env/bin/python honasa/tests/chat.py --trace        # 5. talk to the bot
env/bin/python honasa/tests/run_chat_scenarios.py  # 6. full regression (36/36)
curl -s http://127.0.0.1:9022/api/v1/state | jq    # 7. inspect what was recorded
pkill -f "honasa.api.main"                         # 8. stop the mock
```
