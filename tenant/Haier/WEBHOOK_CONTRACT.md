# Haier rejected-WO bot — dialer webhook contract and CRM hooks

Bot `bot_aba8f9101217` · tenant `tn_fb39b67f5361` · DID `+918047133633` (FreeSWITCH outbound).

## 1. What the dialer must send per call

The outbound call start carries the per-call values in the telephony webhook's
`variables` object (platform contract: max 20 keys, key pattern `[A-Za-z0-9_.-]{1,40}`,
scalar values, each stringified and truncated to 200 characters; invalid entries
are silently dropped). The bot reads them as `{{placeholders}}` in its script, prefills
them into the flow's disposition slots, and passes them to the CRM hooks.

| Key | Required | Format / allowed values | Used for |
|---|---|---|---|
| `customer_name` | yes | First name or "Name Surname" | Greeting "Kya meri baat {customer_name} ji se…", default address "{customer_name} ji", closing |
| `customer_gender` | optional | `M` or `F` | Only when present the bot may say Sir / Ma'am (and then never switches). Absent → "{customer_name} ji" |
| `wo_number` | yes | e.g. `WO2601204174136` | Disposition, CRM hook payloads |
| `product_name` | yes | AC / Refrigerator / Washing Machine / Freezer / TV / … | Purpose line "aapke {product_name} ki {l3_tagging} request…" |
| `l3_tagging` | yes | Installation / Service / Complaint / Demo / … | Purpose line |
| `reject_reason` | yes | EXACTLY one of the 8 labels below (case-insensitive substring match) or `Other` | Purpose line and selection of the reason-based probing branch |
| `request_date` | yes | `YYYY-MM-DD` (WO registration date) | Shown to the LLM for the 5-day context; recorded in the disposition |
| `helpline_number` | yes | Haier toll-free number, written digit-grouped for TTS, e.g. `1800 419 xxxx` | Every "nayi request raise karwa sakte hain" advice line |

Reject-reason labels and the substring the flow keys on:

| Label (send this) | Matched on |
|---|---|
| `Bill not available` | `bill` |
| `Consumer Will Call Back` | `call back` or `callback` |
| `No Response` | `no response` |
| `Product Not Delivered` | `not delivered` |
| `E-Com DOA Case` | `doa` or `damage` |
| `Refuse to Pay` | `refuse` |
| `Unit OK/No Defect Found` | `unit ok` or `no defect` |
| `Not Haier Product` | `not haier` |
| anything else / `Other` | generic path: technician check → service requirement |

Example (Vaani/FreeSWITCH webhook body fragment):

```json
{
  "To": "+919xxxxxxxxx",
  "botId": "bot_aba8f9101217",
  "callId": "haier-rej-20260929-000123",
  "variables": {
    "customer_name": "Vinay",
    "customer_gender": "M",
    "wo_number": "WO2601184168338",
    "product_name": "AC",
    "l3_tagging": "Service",
    "reject_reason": "Consumer Will Call Back",
    "request_date": "2026-09-29",
    "helpline_number": "1800 419 xxxx"
  }
}
```

Missing values never crash the call: the placeholder is dropped from the spoken line
(e.g. "aap helpline number par call karke…") and the disposition field stays empty.

## 2. CRM hooks the flow calls (placeholder endpoints today)

Three tenant API connections exist with the reserved host
`https://crm-integration.haier.example/api/v1` (DNS can never resolve → the flow takes
its `failure` edge, still speaks the script line, and records `hook_status = failed`).
When Haier shares the real endpoints, only `url` + auth on each connection change.

All three are `POST`, JSON, state-changing, `timeoutMs 6000`, `retries 1`. The body is the
connection's `bodyTemplate` constants plus every scalar flow slot plus these call-context
values: `wo_number, customer_name, product_name, l3_tagging, reject_reason, request_date`,
plus runtime metadata (tenant/bot/session ids) for correlation.

### 2.1 `Haier Re-dispatch Work Order` — Action 1
`POST /work-orders/redispatch`
Request (relevant fields): `wo_number`, `action: "RE_DISPATCH"`, `reject_reason`,
`technician_contacted (yes|no|unknown)`, `service_required (yes|no|later)`,
`required_date_text`, `within_5_days (yes|no)`, `charges_accepted (yes|no)`, `resolution`
(`tech_completed` when a Haier technician already fixed it and the WO should close as completed).
Expected response: `{"status": "REDISPATCHED", "wo_number": "...", "dispatch_reference": "RD-..."}`
→ mapped to slots `hook_result`, `dispatch_reference`.

### 2.2 `Haier Cancel Work Order` — Action 2
`POST /work-orders/cancel`
Request: `wo_number`, `action: "CANCEL"`, `cancel_sub_reason` one of
`future_date | gift | resale | not_haier | product_not_delivered | ecom_doa | unit_ok | refuse_to_pay | no_requirement | wrong_number | unclear`,
`required_date_text`, `expected_delivery_text`, `service_required`.
Expected response: `{"status": "CANCELLED", "wo_number": "...", "cancel_reference": "CN-..."}`
→ `hook_result`, `cancel_reference`.

### 2.3 `Haier Set Do Not Call`
`POST /customers/do-not-call`
Request: `wo_number`, `customer_name`, `dnc_requested: true`.
Expected response: `{"status": "DNC_SET"}` → `dnc_hook_result`.
Note: the platform also marks the dialed number do-not-call itself when the caller uses an
explicit stop-calling phrase; this hook additionally informs Haier's CRM.

## 3. Disposition object (post-call structured summary)

Derived from the flow's slots by the platform's structured-summary feature
(`goal_policy.summaryFields`), visible in Conversations → "Structured summary" and the
conversation memory JSON:

```json
{
  "wo_number": "WO2601184168338",
  "customer_reached": "Yes",
  "technician_contacted": "yes | no | unknown",
  "reject_reason_confirmed": "Consumer Will Call Back",
  "service_required": "yes | no | later",
  "required_date_text": "ek month ke baad",
  "within_5_days": "yes | no | unknown",
  "action": "RE_DISPATCH | CANCEL | NO_RESPONSE | ESCALATE | CALLBACK",
  "cancel_sub_reason": "future_date | gift | resale | not_haier | product_not_delivered | ecom_doa | unit_ok | refuse_to_pay | no_requirement | wrong_number | unclear | null",
  "dnc_requested": "Yes | No",
  "hook_status": "ok | failed",
  "customer_sentiment": "neutral | irritated | angry | positive   (post-call analyst)",
  "notes": "free text, Hinglish OK                             (post-call analyst)"
}
```

`NO_RESPONSE` is filled by the post-call analyst when the flow never started (nobody
answered / IVR only); every other value comes from the flow itself.
