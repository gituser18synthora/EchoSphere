# Backend API — Runtime context, customer data, and API integrations

This document describes the 22 Platform API operations implemented by
`backend/routers/runtime_context.py`, `customer_context.py`, `apis.py`, the
platform-template route, and the knowledge-gap route. Base URL:
`http://localhost:9001`; paths already include `/api/v1`.

Every request requires `Authorization: Bearer <ACCESS_TOKEN>`. Read/validation
operations accept an authenticated user with tenant access unless a stricter
role is stated. Success responses use `{ "success": true, "data": ... }`.
Authentication, permission, validation, and hidden cross-tenant failures are
normally `401`, `403`, `422`, and `404`, respectively.

Examples use camelCase; aliased Pydantic fields also accept snake_case.
Full phone/account values and secret values are write-only. Responses mask
sensitive context and never expand `secret://...` references.

Related documents: [API index](README.md), [Bot Studio authoring and
testing](BACKEND_BOT_STUDIO.md), [Knowledge and RAG](BACKEND_KNOWLEDGE.md), and
[security](../SECURITY.md).

## Generic runtime-context schema

Runtime context is tenant-defined and domain-independent. A bot owns at most
one `RuntimeContextSchema`; optional `RuntimeContextRecord` rows provide stored
per-customer values. Live resolution precedence is `system < session <
api|test|record < workflow`.

### Field-definition contract

Each item in `fields` is an object. These are the keys actually consumed by
the runtime:

| Key | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `key` | string | yes | Identifier beginning with a letter, followed by letters/digits/underscore; max 64; unique. |
| `label` | string | no | Display metadata; stored unchanged. |
| `type` | string | no | `string`; allowed `string`, `number`, `integer`, `boolean`, `date`, `object`, `array`. |
| `required` | boolean | no | Falsey when omitted. Missing required values produce a validation error. |
| `sensitive` | boolean | no | Falsey when omitted; sensitive values are masked before entering prompt/traces. |
| `maskKeep` | integer | no | Masking metadata consumed when building context. |
| `description` | string | no | Stored metadata/prompt context. |
| `example` | any JSON value | no | Stored metadata. |

Types are checked without coercion. `true` is not a number; an ISO date is a
`YYYY-MM-DD` string; object/array only validate the container type. JSON `null`
is treated as absent. When `allowAdditional` is false, undeclared keys fail.
Secret-like keys such as account, card, password, token, OTP, CVV, SSN,
Aadhaar, and PAN are defensively masked even if `sensitive` was omitted.

### Runtime-context response schemas

```json
{
  "schema": {
    "id": "<SCHEMA_ID>", "botId": "<BOT_ID>", "name": "User details",
    "sourceMode": "api", "apiConnectionId": "<CONN_ID>",
    "responsePath": "data.customer",
    "fields": [{"key":"customer_name","type":"string","required":true}],
    "allowAdditional": false, "testPayload": null,
    "missingValuePolicy": "Ask instead of guessing.", "domainPolicy": "generic",
    "status": "active", "configured": true
  },
  "record": {
    "id": "<RECORD_ID>", "botId": "<BOT_ID>", "customerRef": "CRM-1001",
    "phoneMasked": "XXXXXX1234", "data": {"customer_name":"Example Customer"},
    "callState": {}, "updatedAt": "2026-08-07T10:00:00Z"
  }
}
```

### Get runtime-context config

`GET /api/v1/bots/{bot_id}/runtime-context`

Purpose: load the bot's schema/editor state. Auth: authenticated tenant member.
Path `bot_id` is required string. No query parameters or body.

```bash
curl -H "Authorization: Bearer <ACCESS_TOKEN>" \
  http://localhost:9001/api/v1/bots/<BOT_ID>/runtime-context
```

`200` returns the schema shape above. If no row exists it returns a usable
default with `id:null`, `sourceMode:"manual"`, `fields:[]`,
`allowAdditional:true`, `domainPolicy:"generic"`, and `configured:false`.
`404` means the bot is absent or inaccessible.

### Save runtime-context config

`PUT /api/v1/bots/{bot_id}/runtime-context`

Purpose: create or replace the schema configuration. Auth: tenant admin
(`super_admin` or `tenant_admin`). Path `bot_id`; no query parameters.

```json
{
  "name":"User details",
  "sourceMode":"api",
  "apiConnectionId":"<CONN_ID>",
  "responsePath":"data.customer",
  "fields":[
    {"key":"customer_name","label":"Customer name","type":"string","required":true,"sensitive":false},
    {"key":"account_number","label":"Account","type":"string","required":false,"sensitive":true,"maskKeep":4}
  ],
  "allowAdditional":false,
  "testPayload":{"customer_name":"Example Customer","account_number":"00001234"},
  "missingValuePolicy":"Ask the caller; never invent a value.",
  "domainPolicy":"generic"
}
```

| Field | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `name` | string | no | `User details`; max 200. |
| `sourceMode` | string | no | `manual`; `manual` or `api`. |
| `apiConnectionId` | string/null | no | Required when source is `api`; connection must belong to tenant and either bot/global scope. |
| `responsePath` | string/null | no | Max 200; dot path into API response, or entire response when empty/null. |
| `fields` | object[] | no | `[]`; field-definition contract above. |
| `allowAdditional` | boolean | no | `true`. |
| `testPayload` | object/null | no | `null`; validated against `fields`. |
| `missingValuePolicy` | string/null | no | Max 500. |
| `domainPolicy` | string | no | `generic`; `generic` or `collections`. |

`200` returns the stored schema. Errors: `404` bot; `422` invalid field
definition/source/domain/API reference/test payload.

### Validate context payload

`POST /api/v1/bots/{bot_id}/runtime-context/validate`

Purpose: validate an API/manual payload and show the exact masked,
source-tagged context a call would receive. Auth: authenticated member. Path
`bot_id`; no query params.

```json
{
  "payload":{"customer_name":"Example Customer","account_number":"00001234"},
  "fields":null,
  "allowAdditional":false
}
```

| Field | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `payload` | object | no | `{}`. |
| `fields` | object[]/null | no | Saved fields when null; otherwise validates unsaved editor definitions. |
| `allowAdditional` | boolean/null | no | Saved value, or true when no saved schema. |

```json
{
  "success":true,
  "data":{"valid":true,"errors":[],"effective":[{"key":"customer_name","value":"Example Customer","source":"api","sensitive":false},{"key":"account_number","value":"••••1234","source":"api","sensitive":true}],"missingRequired":[],"declaredMissing":[],"promptSection":"..."}
}
```

`200` is returned for contract mismatches; inspect `valid/errors`. Request-model
errors are `422`; unknown bot is `404`.

### List context records

`GET /api/v1/bots/{bot_id}/runtime-context/records`

Purpose: paginated masked records for a bot. Auth: authenticated member. Path
`bot_id`. No body.

| Query | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `page` | integer | no | 1; at least 1. |
| `pageSize` | integer | no | 50; 1–200. |
| `search` | string/null | no | Max 200; matches `customerRef`. |
| `sortBy` | string/null | no | Max 50; accepted but ignored. |
| `sortDir` | string | no | `desc`; `asc`/`desc`, accepted but ignored. |

`200` returns a Record array plus
`meta:{page,pageSize,total,totalPages}`. Values marked sensitive by the schema
are masked. `404` bot.

### Create context record

`POST /api/v1/bots/{bot_id}/runtime-context/records`

Purpose: store one typed customer payload. Auth: tenant admin. Path `bot_id`;
no query params.

```json
{"customerRef":"CRM-1001","phone":"+919999991234","data":{"customer_name":"Example Customer"}}
```

| Field | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `customerRef` | string/null | no | Max 80. |
| `phone` | string/null | no | E.164-ish: optional `+`, digits/spaces/hyphens, 7–19 trailing characters. |
| `data` | object | no | `{}`; validated without coercion against the saved schema. |

`201` returns the masked Record. Errors: `404` bot; `422` body/schema mismatch.

### Update context record

`PATCH /api/v1/runtime-context-records/{record_id}`

Purpose: replace record data and optionally change identifiers. Auth: tenant
admin. Path `record_id`; no query params. Body is the same `RecordPayload` as
create; `data` defaults to `{}` and therefore replaces stored data even when
omitted.

```json
{"customerRef":"CRM-1001","phone":"+919999991234","data":{"customer_name":"Updated Customer"}}
```

`200` returns masked Record. Errors: `404` record/bot; `422` invalid body/data.

### Delete context record

`DELETE /api/v1/runtime-context-records/{record_id}`

Purpose: soft-delete a record. Auth: tenant admin. Path `record_id`. Optional
boolean query `hard=false` invokes the hard-delete guard but still soft-deletes.
No body. `200`: `{"success":true,"data":{"deleted":true}}`.
Errors: `403` hard delete disabled; `404` record.

## Legacy collections customer context

The `customer-contexts` APIs are the compatibility shape for loan-collection
bots. They remain live when a bot has no generic runtime-context schema.

### Customer-context request schema

All fields are optional and unknown keys are rejected.

| Field | Type | Validation |
| --- | --- | --- |
| `customerRef` | string/null | Max 80. |
| `phone` | string/null | E.164-ish pattern described above. |
| `customerName`, `dcsName`, `lenderName` | string/null | Max 150 each. |
| `loanAccountNumber` | string/null | 4–40; write-only. |
| `preferredLanguage` | string/null | `xx` or `xx-YY`. |
| `overdueAmount`, `totalOutstanding`, `minimumPayable`, `penalCharges` | decimal/null | At least 0. |
| `daysOverdue` | integer/null | 0–36,500. |
| `dueDate`, `previousPromiseDate` | date/null | ISO `YYYY-MM-DD`. |
| `partialPaymentAllowed` | boolean/null | — |
| `paymentMethods` | string[]/null | Max 10 items; blanks removed, each max 40. |
| `securePaymentLinkAvailable` | boolean/null | — |
| `activeOffers` | object[]/null | Max 10 items. |
| `offerTerms` | string/null | Max 4,000. |
| `creditReportingStatus` | string/null | Max 120. |
| `callbackNumber` | string/null | E.164-ish pattern; masked on response. |
| `grievanceContact` | string/null | Max 150. |
| `paymentStatus` | string/null | `pending`, `partial`, `completed`, `disputed`, `unknown`. |
| `customerVerified`, `recordingNoticeRequired`, `complaintPending`, `accountDisputed` | boolean/null | — |

Responses contain: `id`, `tenantId`, `botId`, `customerRef`, `phoneMasked`,
`customerName`, `dcsName`, `lenderName`, `loanAccountMasked`,
`preferredLanguage`, the four amount fields, `daysOverdue`, `dueDate`,
`previousPromiseDate`, `partialPaymentAllowed`, `paymentMethods`,
`securePaymentLinkAvailable`, `activeOffers`, `offerTerms`,
`creditReportingStatus`, masked `callbackNumber`, `grievanceContact`,
`paymentStatus`, `customerVerified`, `recordingNoticeRequired`,
`complaintPending`, `accountDisputed`, `callbackRequested`,
`callbackRequestedAt`, `lastCallId`, `lastDisposition`, `isFinalTranscript`,
`interruptionDetected`, and `updatedAt`.

### List customer contexts

`GET /api/v1/bots/{bot_id}/customer-contexts`

Purpose: paginated masked collection accounts. Auth: authenticated member.
Path `bot_id`; no body. Query `page=1`, `pageSize=50` (1–200), `search` max
200 (matches customer name/reference), `sortBy` max 50, `sortDir=desc`
(`asc|desc`). Sorting fields are accepted but ignored; ordering is updated time
descending. `200` returns array + pagination meta; `404` bot.

### Lookup customer context by phone

`GET /api/v1/bots/{bot_id}/customer-contexts/lookup`

Purpose: show the context that call-time trailing-digit matching will load.
Auth: authenticated member. Path `bot_id`. Required query `phone` uses the
E.164-ish pattern; no body.

```bash
curl -H "Authorization: Bearer <ACCESS_TOKEN>" \
  'http://localhost:9001/api/v1/bots/<BOT_ID>/customer-contexts/lookup?phone=%2B919999991234'
```

`200` returns masked Customer Context; `404` bot/no match; `422` query pattern.
Implementation note: the explicit fewer-than-four-digits error path currently
constructs `ApiError` arguments in the wrong order; see the findings section.

### Get customer context

`GET /api/v1/customer-contexts/{context_id}`

Purpose: fetch one masked account. Auth: authenticated member. Path
`context_id`; no query/body. `200` returns Customer Context; `404` absent or
cross-tenant.

### Create customer context

`POST /api/v1/bots/{bot_id}/customer-contexts`

Purpose: create a legacy collection account. Auth: tenant admin. Path `bot_id`;
no query params. Body uses the full optional request schema above.

```json
{
  "customerRef":"CRM-1001","phone":"+919999991234","customerName":"Example Customer",
  "loanAccountNumber":"LOAN0001234","preferredLanguage":"hi-IN",
  "overdueAmount":12500.0,"daysOverdue":12,"paymentStatus":"pending",
  "customerVerified":false,"recordingNoticeRequired":true
}
```

`201` returns the masked response; `404` bot; `422` validation/unknown fields.

### Update customer context

`PATCH /api/v1/customer-contexts/{context_id}`

Purpose: partial update of the tenant-owned account fields. Auth: tenant admin.
Path `context_id`; no query params. Body is the same schema as create; omitted
fields remain unchanged and explicit null clears a field.

```json
{"overdueAmount":10000.0,"paymentStatus":"partial"}
```

`200` returns masked Customer Context; `404` unknown; `422` invalid body.

### Update customer call state

`PATCH /api/v1/customer-contexts/{context_id}/call-state`

Purpose: update the runtime-owned subset without granting account-edit access.
Auth: any authenticated user with tenant access. Path `context_id`; no query.
Unknown fields are rejected and at least one field must be supplied.

```json
{"customerVerified":true,"paymentStatus":"partial","lastCallId":"<CALL_ID>","lastDisposition":"promise_to_pay","isFinalTranscript":true,"interruptionDetected":false}
```

| Field | Type | Validation |
| --- | --- | --- |
| `customerVerified`, `accountDisputed`, `complaintPending`, `callbackRequested`, `isFinalTranscript`, `interruptionDetected` | boolean/null | Optional. |
| `paymentStatus` | string/null | Payment-status enum above. |
| `callbackRequestedAt` | ISO datetime/null | Auto-set to current time when callback becomes true and timestamp is omitted. |
| `lastCallId` | string/null | Max 64. |
| `lastDisposition` | string/null | Max 40. |

`200` returns masked Customer Context; `404` unknown; `422` model errors. The
empty-body branch intends to return `400` but currently constructs `ApiError`
arguments in the wrong order; see findings.

### Delete customer context

`DELETE /api/v1/customer-contexts/{context_id}`

Purpose: soft-delete an account. Auth: tenant admin. Path `context_id`. Query
`hard=false` optional boolean, guarded; no body. `200` returns
`{"deleted":true}`. Errors: `403` hard-delete disabled; `404` unknown.

## API connections

API connections are tenant-owned request templates. Raw secrets are forbidden;
`secretRef` stores only a `secret://...` name resolved server-side. Template
expressions may use `tenant_id`, `bot_id`, `call_id`, `session_id`, `user_id`,
`customer_phone`, `intent.code`, `intent.name`, and `entities.<name>`.

### API-connection response schema

```json
{
  "id":"<CONN_ID>","botId":"<BOT_ID>","name":"Customer lookup",
  "description":"CRM lookup","method":"GET","url":"https://api.example.test/customers/{{customer_phone}}",
  "authType":"bearer","secretRef":"secret://crm_api_token","headers":{},
  "queryParams":{},"pathParams":{},"bodyTemplate":null,"requestSchema":null,
  "responseSchema":null,"successCondition":"status < 400",
  "successMessage":"Lookup succeeded","failureMessage":"Lookup failed",
  "errorMapping":{},"sensitiveMasks":["Authorization"],"allowedIntents":[],
  "allowedWorkflows":[],"isStateChanging":false,"requireConfirmation":false,
  "timeoutMs":4000,"retries":1,"responseMapping":[],"status":"untested",
  "lastTestedAt":null,"lastLatencyMs":null,"version":1,
  "updatedAt":"2026-08-07T10:00:00Z"
}
```

### API-connection create schema

| Field | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `name` | string | yes | 1–200. |
| `description` | string | no | `""`; max 500. |
| `method` | string | no | `GET`; `GET`, `POST`, `PUT`, `PATCH`, `DELETE`. |
| `url` | string | yes | 8–500; request execution additionally enforces SSRF policy. |
| `authType` | string | no | `none`; `none`, `api_key`, `oauth2`, `bearer`, `basic`. |
| `secretRef` | string/null | no | Max 300; when set must start `secret://`. |
| `headers`, `queryParams`, `pathParams` | object<string,string> | no | `{}`. |
| `bodyTemplate`, `requestSchema`, `responseSchema` | object/null | no | `null`. |
| `successCondition` | string/null | no | Max 200; tester evaluates only restricted `status <op> NNN`. |
| `successMessage`, `failureMessage` | string/null | no | Max 500 each. |
| `errorMapping` | object | no | `{}`. |
| `sensitiveMasks` | string[] | no | `[]`; names of headers to mask in test trace. |
| `allowedIntents`, `allowedWorkflows` | string[] | no | `[]`; same-tenant resource IDs. |
| `isStateChanging`, `requireConfirmation` | boolean | no | `false`. |
| `timeoutMs` | integer | no | 4,000; 100–60,000. |
| `retries` | integer | no | 1; 0–5. |
| `responseMapping` | object[] | no | `[]`. |
| `botId` | string/null | no | Optional bot scope; otherwise tenant-wide. |

### List API connections

`GET /api/v1/api-connections`

Purpose: list tenant connections in creation order. Auth: authenticated user.
Optional query `botId` filters to one bot; optional `tenantId` resolves the
tenant (required for platform admins). No body. `200` returns the response
schema array. Errors: `400` missing platform-admin tenant; `403` cross-tenant.

### Create API connection

`POST /api/v1/api-connections`

Auth: `manage_api_connections` or `integrations.manage`. No path/query params.
Body uses every create-schema field above.

```json
{
  "name":"Customer lookup","description":"CRM lookup","method":"GET",
  "url":"https://api.example.test/customers/{{customer_phone}}","authType":"bearer",
  "secretRef":"secret://crm_api_token","headers":{},"queryParams":{},"pathParams":{},
  "bodyTemplate":null,"requestSchema":null,"responseSchema":null,
  "successCondition":"status < 400","successMessage":"Lookup succeeded",
  "failureMessage":"Lookup failed","errorMapping":{},"sensitiveMasks":["Authorization"],
  "allowedIntents":[],"allowedWorkflows":[],"isStateChanging":false,
  "requireConfirmation":false,"timeoutMs":4000,"retries":1,"responseMapping":[],
  "botId":"<BOT_ID>"
}
```

`201` returns API Connection. Errors: `404` bot; `422` raw secret, unknown
template variable, or invalid intent/workflow association; tenant resolution
may return `400/403`.

### Test API connection

`POST /api/v1/api-connections/{conn_id}/test`

Purpose: issue an SSRF-guarded outbound test using masked trace output. Auth:
`test_api_connections`, `manage_api_connections`, or `integrations.manage`.
Path `conn_id`; no query params. Body is optional.

```json
{"testValues":{"customer_phone":"+919999991234","entities.account_id":"ACC-1001"}}
```

`testValues` is an optional `object<string,string>` default `{}` and overrides
the built-in placeholder sample values for the test only.

```json
{
  "success":true,
  "data":{"ok":true,"latencyMs":128,"status":200,"contentType":"application/json","body":"{...}","truncated":false,"error":null,"redirectedTo":null,"headersSent":{"Authorization":"••••••••"},"userMessage":"Lookup succeeded"}
}
```

`200` is returned even for a tested upstream failure; inspect `ok`, `status`,
and `error`. The connection row becomes `healthy` or `failing`. Errors before
the outbound request include `404` connection and `403` permission/SSRF policy.

### Update API connection

`PATCH /api/v1/api-connections/{conn_id}`

Auth: `manage_api_connections` or `integrations.manage`. Path `conn_id`; no
query params. Every create field except `botId` is optional; `status` is also
accepted with `healthy`, `degraded`, `failing`, `untested`, or `disabled`.
Same validation applies. Explicit null is ignored for handler-applied fields.

```json
{"timeoutMs":2500,"retries":0,"status":"healthy"}
```

`200` returns API Connection and increments `version` on change. Errors: `404`;
`422` raw secret, invalid variable/reference/value.

### Duplicate API connection

`POST /api/v1/api-connections/{conn_id}/duplicate`

Auth: `manage_api_connections` or `integrations.manage`. Path `conn_id`; no
body/query. `201` returns a unique-name version-1 clone with
`status:"untested"`; `404` unknown.

### Archive API connection

`DELETE /api/v1/api-connections/{conn_id}`

Auth: `manage_api_connections` or `integrations.manage`. Path `conn_id`.
Optional boolean query `hard=false` is guarded; no body. `200` returns
`{"archived":true,"id":"<CONN_ID>"}`. Errors: `409` when any intent still
references it; `403` hard delete disabled; `404` unknown.

## Platform templates

### List templates

`GET /api/v1/templates`

Purpose: retrieve governance-library templates of one kind. Auth: super admin
only. Required query `kind` is string max 40; no body.

```bash
curl -H "Authorization: Bearer <ACCESS_TOKEN>" \
  'http://localhost:9001/api/v1/templates?kind=journey'
```

`200` returns an array. Stable fields are `id`, `kind`, `name`, `description`,
and `status`; remaining fields are the stored `PlatformTemplate.payload` and
therefore vary by `kind`.

```json
{"success":true,"data":[{"id":"<TEMPLATE_ID>","kind":"journey","name":"Collections starter","description":"...","status":"active","nodes":[],"edges":[]}]}
```

Errors: `403` non-super-admin; `422` missing/oversize kind. Because payload is
operator-defined JSON, the implementation has no more specific static response
schema to document.

## Knowledge gaps

### List knowledge gaps

`GET /api/v1/knowledge-gaps`

Purpose: top unanswered/repeated questions for a tenant, sorted by frequency
and capped at 50. Auth: authenticated user. Optional query `tenantId` resolves
the tenant (required for a platform admin); optional `botId` filters a bot. No
body.

```json
{"success":true,"data":[{"id":"<GAP_ID>","question":"Can I change my due date?","frequency":12,"lastAsked":"2026-08-07T10:00:00Z","suggestedSource":"Repayment policy"}]}
```

`200` returns objects with exactly `id`, `question`, `frequency`, `lastAsked`,
and `suggestedSource`. Tenant resolution errors are `400/403`.

## Code/API inconsistencies found while tracing

- In `lookup_customer_context`, `ApiError(400, "phone must contain at least 4
  digits")` reverses the constructor's `(message, status_code)` parameters.
  The normal query regex makes this branch difficult to reach, but if reached
  it cannot produce the intended safe `400` response.
- `update_call_state` has the same reversed-argument bug for an empty update:
  `ApiError(400, "no call-state fields provided")`.
- `GET /api/v1/templates` deliberately merges arbitrary database payload keys
  into each response. Its contract cannot be fully typed until template kinds
  receive discriminated response models.
- `PATCH /api/v1/runtime-context-records/{record_id}` uses a model whose `data`
  defaults to `{}` and the handler always assigns it. Omitting `data` therefore
  clears the record data, despite PATCH-style naming.
