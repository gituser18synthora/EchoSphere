# Backend API — Bot Studio authoring and testing

This document describes the 32 Platform API operations implemented by
`backend/routers/prompts.py`, `intents.py`, `workflows.py`, `releases.py`, and
`testing.py`. The base URL is `http://localhost:9001`; every path below already
includes `/api/v1`.

Send `Authorization: Bearer <ACCESS_TOKEN>` on every request. A normal read or
test operation accepts any authenticated user with access to the resource's
tenant. Mutations require the permission or role stated below. Tenant-scoped
lookups deliberately return `404` for a resource owned by another tenant.

Success responses use `{ "success": true, "data": ... }`. Validation failures
are `422`; authentication failures are `401`; insufficient permission is `403`.
Examples use camelCase. The request models also accept their snake_case field
names where an alias is defined.

Related documents: [API index](README.md), [Bots, voice settings, and
channels](BACKEND_BOTS.md), and [runtime context and integrations](BACKEND_RUNTIME_CONTEXT.md).

## Shared response schemas

### Prompt

```json
{
  "id": "<PROMPT_ID>",
  "botId": "<BOT_ID>",
  "type": "system",
  "name": "System prompt",
  "description": "Primary runtime instructions",
  "variables": ["customer_name"],
  "state": "published",
  "activeVersion": 2,
  "publishedVersion": 2,
  "approvedBy": "Approver name",
  "approvedAt": "2026-08-07T10:00:00Z",
  "publishedAt": "2026-08-07T10:05:00Z",
  "versions": [
    {
      "version": 2,
      "editedBy": "Editor name",
      "editedAt": "2026-08-07T09:55:00Z",
      "note": "Tighten verification rules",
      "variants": [],
      "promptMode": "full",
      "structuredConfig": null,
      "fullPrompt": "You are ...",
      "compiledPrompt": "You are ...",
      "modelCompatibility": []
    }
  ]
}
```

### Intent

```json
{
  "id": "<INTENT_ID>", "botId": "<BOT_ID>", "name": "promise_to_pay",
  "code": "promise_to_pay", "category": "collections", "description": "...",
  "samples": ["I will pay Friday", "Friday ko payment karunga", "I promise to pay"],
  "languages": ["en-IN", "hi-IN"], "confidenceThreshold": 0.7,
  "avgConfidence30d": 0.81, "route": "workflow:promise_to_pay",
  "entities": ["payment_date"], "optionalEntities": ["amount"],
  "workflowId": "<WORKFLOW_ID>", "apiConnectionId": null, "kbIds": [],
  "priority": 100, "fallbackBehavior": "clarify", "handoffEnabled": false,
  "status": "active", "version": 1, "testPass": 0, "testTotal": 0,
  "updatedAt": "2026-08-07T10:00:00Z"
}
```

### Entity

```json
{
  "id": "<ENTITY_ID>", "name": "payment_date", "code": "payment_date",
  "description": "Promised payment date", "kind": "custom", "dataType": "date",
  "languages": ["en-IN", "hi-IN"], "synonyms": {}, "allowedValues": [],
  "regexPattern": "", "validationRules": {}, "normalizationRules": {},
  "maskingEnabled": false, "requireConfirmation": true, "retentionDays": 90,
  "example": "2026-08-15", "pii": false, "status": "active", "usedBy": [],
  "updatedAt": "2026-08-07T10:00:00Z"
}
```

### Workflow, release, and scenario

```json
{
  "workflow": {
    "id": "<WORKFLOW_ID>", "botId": "<BOT_ID>", "name": "Main journey",
    "version": 3, "status": "approved", "nodes": [], "edges": [], "issues": [],
    "updatedAt": "2026-08-07T10:00:00Z", "updatedBy": "Editor name"
  },
  "release": {
    "id": "<RELEASE_ID>", "botId": "<BOT_ID>", "version": "v1.2.0",
    "stage": "review", "notes": "Release notes", "requestedBy": "Admin",
    "approvedBy": null, "scheduledFor": null, "publishedAt": null,
    "checklist": [], "diff": []
  },
  "scenario": {
    "id": "<SCENARIO_ID>", "botId": "<BOT_ID>", "name": "Happy path",
    "suite": "General", "steps": 4, "lastRun": null
  }
}
```

## Prompts

Prompt types are `system`, `greeting`, `fallback`, `escalation`, `closing`,
`reprompt`, and `hold`. Authoring modes are `structured` and `full`.

`VariantPayload` is used by create/version requests:

| Field | Type | Required | Validation |
| --- | --- | --- | --- |
| `language` | string | yes | At most 15 characters; must be an enabled language assigned to the tenant for a newly added variant. |
| `content` | string | yes | At most 4,000 characters. |

Non-system prompts require at least one variant and cannot repeat a language.
System prompts instead use `structuredConfig` or `fullPrompt`, which the shared
prompt compiler validates before persistence.

### List bot prompts

`GET /api/v1/bots/{bot_id}/prompts`

Lists non-archived prompts in creation order. Auth: authenticated tenant member.
Path: `bot_id` (string, required). No query parameters or body.

```bash
curl -H "Authorization: Bearer <ACCESS_TOKEN>" \
  http://localhost:9001/api/v1/bots/<BOT_ID>/prompts
```

`200` returns a Prompt array. `404` means the bot is absent or inaccessible.

### Create prompt

`POST /api/v1/bots/{bot_id}/prompts`

Creates version 1 in `draft`. Auth: `manage_prompts` or `prompts.manage`.
Path: `bot_id`. No query parameters.

```json
{
  "type": "greeting",
  "promptMode": "structured",
  "name": "Hindi greeting",
  "description": "First turn",
  "variables": ["customer_name"],
  "variants": [{"language": "hi-IN", "content": "Namaste {customer_name}."}],
  "structuredConfig": null,
  "fullPrompt": null,
  "note": "Initial version"
}
```

| Field | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `type` | string | yes | Prompt-type enum above. |
| `promptMode` | string | no | `structured`; `structured` or `full`. |
| `name` | string | yes | 1–200; trimmed and unique within the bot. |
| `description` | string | no | `""`; at most 500. |
| `variables` | string[] | no | `[]`; compiler-discovered variables are appended. |
| `variants` | VariantPayload[] | no | `[]`; required in practice for non-system types. |
| `structuredConfig` | object/null | no | `null`; validated by the shared compiler in structured mode. |
| `fullPrompt` | string/null | no | `null`; validated in full mode. |
| `note` | string | no | `Initial version`; at most 500. |

`201` returns the Prompt schema. Important errors: `409` duplicate name; `422`
invalid compiler input, missing/duplicate variant language, or unassigned language.

### Add prompt version

`POST /api/v1/prompts/{prompt_id}/versions`

Adds and activates the next version. Auth: `manage_prompts` or `prompts.manage`.
Path: `prompt_id`. No query parameters.

```json
{
  "note": "Add a shorter greeting",
  "promptMode": "structured",
  "variants": [{"language": "hi-IN", "content": "Namaste {customer_name}."}],
  "structuredConfig": null,
  "fullPrompt": null,
  "submitForApproval": true
}
```

| Field | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `note` | string | no | `""`; max 500. |
| `promptMode` | string/null | no | Inferred from payload, then previous version; enum above. |
| `variants` | VariantPayload[] | no | `[]`; language rules above. |
| `structuredConfig` | object/null | no | Compiler input. |
| `fullPrompt` | string/null | no | Compiler input. |
| `submitForApproval` | boolean | no | `true`; sets state to `pending_approval`, otherwise `draft`. |

`201` returns the full Prompt with the new version. `404` unknown prompt;
`422` invalid language/content/config.

### Compile preview (stateless)

`POST /api/v1/prompts/compile-preview`

Compiles unsaved authoring data. Auth: authenticated user. No path/query params.

```json
{
  "promptMode": "full",
  "structuredConfig": null,
  "fullPrompt": "Address {customer_name} politely.",
  "testContext": {"customer_name": "Example Customer"}
}
```

All fields are optional: `promptMode` defaults to `structured` and accepts the
two mode values; config/prompt/context default to `null`.

```json
{
  "success": true,
  "data": {
    "compiled": "Address {customer_name} politely.", "valid": true, "errors": [],
    "characterCount": 35, "tokenEstimate": 9, "variables": ["customer_name"],
    "render": {"rendered": "Address Example Customer politely.", "missing": []}
  }
}
```

`200` is returned even when compilation is invalid; inspect `valid` and
`errors`. Invalid Pydantic field types/modes return `422`.

### Render saved prompt preview

`POST /api/v1/prompts/{prompt_id}/render-preview`

Renders a saved version with sample context. Auth: authenticated user. Path:
`prompt_id`. No query parameters.

```json
{"version": 2, "testContext": {"customer_name": "Example Customer"}}
```

`version` is optional/null and must be at least 1; omitted selects
`activeVersion`. `testContext` is optional and defaults to `{}`.

```json
{"success":true,"data":{"promptVersion":2,"promptMode":"full","rendered":"...","missing":[]}}
```

Errors: `404` prompt; `422` unknown version or version without renderable content.

### Duplicate prompt

`POST /api/v1/prompts/{prompt_id}/duplicate`

Copies the active version to a new draft named `… (copy)`. Auth:
`manage_prompts` or `prompts.manage`. Path: `prompt_id`. No body/query params.

```bash
curl -X POST -H "Authorization: Bearer <ACCESS_TOKEN>" \
  http://localhost:9001/api/v1/prompts/<PROMPT_ID>/duplicate
```

`201` returns the cloned Prompt; `404` unknown/inaccessible prompt.

### Update prompt and lifecycle

`PATCH /api/v1/prompts/{prompt_id}`

Updates metadata, state, or active version. Auth: at least one of
`manage_prompts`, `approve_prompts`, `publish_prompts`, `prompts.manage`;
approval/rejection specifically needs `approve_prompts` or `prompts.manage`,
and publishing or moving a published version needs `publish_prompts` or
`prompts.manage`. Path: `prompt_id`; no query params.

```json
{"state":"published","activeVersion":2}
```

| Field | Type | Required | Validation |
| --- | --- | --- | --- |
| `name` | string/null | no | Max 200. |
| `description` | string/null | no | Max 500. |
| `variables` | string[]/null | no | Replaces the list. |
| `state` | string/null | no | `draft`, `pending_approval`, `approved`, `rejected`, `published`, `archived`. |
| `activeVersion` | integer/null | no | At least 1 and must exist. |

`200` returns the Prompt. Errors: `403` state-specific permission; `404`
prompt; `422` invalid/unknown version.

### Archive prompt

`DELETE /api/v1/prompts/{prompt_id}`

Soft-archives a prompt. Auth: `manage_prompts` or `prompts.manage`. Path:
`prompt_id`. Query `hard` is boolean, optional, default `false`; when true it
is gated by `ALLOW_HARD_DELETE` but the handler still performs a soft delete.

`200`: `{"success":true,"data":{"archived":true,"id":"<PROMPT_ID>"}}`.
Errors: `403` hard-delete disabled; `404` prompt.

### Test prompt

`POST /api/v1/prompts/{prompt_id}/test`

Runs the saved prompt through the real router, optional centralized knowledge
retrieval, and configured LLM. It is text-only and executes no tools. Auth:
`manage_prompts` or `prompts.manage`. Path: `prompt_id`; no query params.

```json
{"message":"What is the payment due date?","language":"en-IN","version":2,"useKnowledge":true}
```

| Field | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `message` | string | yes | 1–2,000. |
| `language` | string | no | `en-US`; max 15. |
| `version` | integer/null | no | Active version; at least 1. |
| `useKnowledge` | boolean | no | `true`. |

```json
{
  "success": true,
  "data": {
    "promptVersion": 2, "language": "en-IN", "route": "knowledge",
    "matchedIntent": null, "intentConfidence": 0.0, "usedKnowledgeBase": true,
    "sources": [{"documentName":"Policy.pdf","score":0.82,"text":"..."}],
    "response": "...", "latencyMs": 640,
    "tokens": {"input": 210, "output": 55}, "provider": "openai", "error": null
  }
}
```

`200` includes a safe `error` string when the provider call fails. `404`
prompt/bot; `422` unknown or empty version.

## Intents

### List bot intents

`GET /api/v1/bots/{bot_id}/intents`

Lists active and disabled non-archived definitions ordered by priority and
creation time. Auth: authenticated member. Path `bot_id`; no query/body.
`200` returns an Intent array; `404` bot.

### Create intent

`POST /api/v1/bots/{bot_id}/intents`

Auth: `manage_intents` or `bots.manage`. Path `bot_id`; no query params.

```json
{
  "name": "promise_to_pay", "code": "promise_to_pay", "category": "collections",
  "description": "Caller commits to pay", "samples": ["I will pay Friday", "I promise to pay", "Friday ko bhar dunga"],
  "languages": ["en-IN", "hi-IN"], "confidenceThreshold": 0.7,
  "route": "workflow:promise_to_pay", "entities": ["payment_date"],
  "optionalEntities": ["amount"], "workflowId": "<WORKFLOW_ID>",
  "apiConnectionId": null, "kbIds": [], "priority": 100,
  "fallbackBehavior": "clarify", "handoffEnabled": false, "status": "active"
}
```

| Field | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `name` | string | yes | 1–100; unique within bot. |
| `code` | string/null | no | Derived from name; max 80 and unique within bot. |
| `category` | string/null | no | Max 80. |
| `description` | string | no | `""`; max 500. |
| `samples` | string[] | no | `[]`; blanks removed, case/space-normalized duplicates rejected. Fewer than 3 forces `needs_samples`. |
| `languages` | string[] | no | `[]`. |
| `confidenceThreshold` | number | no | `0.7`; 0–1. |
| `route` | string | no | `""`; max 200. |
| `entities`, `optionalEntities` | string[] | no | `[]`; every named entity must exist in the tenant. |
| `workflowId`, `apiConnectionId` | string/null | no | Must reference same-tenant resources. |
| `kbIds` | string[] | no | `[]`; must reference accessible tenant/global sources. |
| `priority` | integer | no | `100`; 0–1,000. |
| `fallbackBehavior` | string/null | no | `clarify`, `handoff`, or `llm`. |
| `handoffEnabled` | boolean | no | `false`. |
| `status` | string | no | `active`; `active`, `needs_samples`, `disabled`. |

`201` returns Intent. Errors: `409` duplicate name/code; `422` duplicate samples
or invalid entity/workflow/API/KB references; `404` bot.

### Update intent

`PATCH /api/v1/intents/{intent_id}`

Partial update. Auth: `manage_intents` or `bots.manage`. Path `intent_id`; no
query params. The body accepts every create field as optional; update `status`
also accepts `archived`. `name`, `code`, and all numeric/string limits remain
the same. Omitted fields are unchanged; explicit JSON `null` is also ignored
for most fields by the implementation.

```json
{"samples":["I will pay Friday","I promise to pay","Friday ko bhar dunga","Tomorrow payment karunga"],"status":"active"}
```

`200` returns Intent and increments `version` when a mutable field changes.
Errors: `404`; `409` duplicate name; `422` invalid references/samples.

### Duplicate intent

`POST /api/v1/intents/{intent_id}/duplicate`

Auth: `manage_intents` or `bots.manage`. Path `intent_id`; no body/query.
`201` returns a version-1 clone with `status: "disabled"`, a unique copy name,
and a `_copy` code; `404` unknown intent.

### Archive intent

`DELETE /api/v1/intents/{intent_id}`

Auth: `manage_intents` or `bots.manage`. Path `intent_id`. Optional boolean
query `hard=false` has the shared hard-delete guard; deletion remains soft.
`200`: `{"success":true,"data":{"archived":true,"id":"<INTENT_ID>"}}`.
Errors: `403` hard-delete guard; `404`.

### Test intent routing

`POST /api/v1/bots/{bot_id}/intents/test`

Runs the real deterministic turn router and entity extractor without changing
state. Auth: authenticated member. Path `bot_id`; no query params.

```json
{"utterance":"I can pay on Friday","language":"en-IN"}
```

`utterance` is required (1–1,000); `language` is optional (`en-US`, max 15).

```json
{
  "success":true,
  "data":{"utterance":"I can pay on Friday","language":"en-IN","route":"workflow","action":"promise_to_pay","matchedIntent":"promise_to_pay","confidence":0.86,"reason":"training phrase match","consideredKb":false,"workflowId":"<WORKFLOW_ID>","apiConnectionId":null,"fallbackBehavior":"clarify","entities":[]}
}
```

`200` returns the routing trace; `404` bot.

## Entities

Entity kinds are `system`, `custom`, `regex`, `api`. Data types are `text`,
`number`, `integer`, `decimal`, `date`, `date_range`, `time`, `duration`,
`currency`, `percentage`, `phone`, `email`, `account_number`, `policy_number`,
`claim_number`, `card_last4`, `person_name`, `location`, `product`, `list`,
`regex`, and `api`.

### List entities

`GET /api/v1/entities`

Auth: authenticated user. Query `tenantId` is optional for tenant users and
must resolve to their own tenant; platform admins must provide it. No body.
`200` returns an Entity array. Errors: `400` missing platform-admin tenant;
`403` cross-tenant request.

### Create entity

`POST /api/v1/entities`

Auth: `manage_entities` or `bots.manage`. No path/query params.

```json
{
  "name":"payment_date","code":"payment_date","description":"Promised payment date",
  "kind":"custom","dataType":"date","languages":["en-IN","hi-IN"],
  "synonyms":{},"allowedValues":[],"regexPattern":null,
  "validationRules":{},"normalizationRules":{},"maskingEnabled":false,
  "requireConfirmation":true,"retentionDays":90,"example":"2026-08-15",
  "pii":false,"tenantId":"<TENANT_ID>"
}
```

| Field | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `name` | string | yes | 1–100, tenant-unique. Names suggesting CVV/CVC/OTP/password/passcode/card PIN are prohibited. |
| `code` | string/null | no | Derived from name; max 80. |
| `description` | string | no | `""`; max 500. |
| `kind` | string | no | `custom`; kind enum above. `regex` requires `regexPattern`. |
| `dataType` | string | no | `text`; data-type enum above. |
| `languages` | string[] | no | `[]`. |
| `synonyms` | object<string,string[]> | no | `{}`. |
| `allowedValues` | string[] | no | `[]`. |
| `regexPattern` | string/null | no | Max 500 and must compile. |
| `validationRules`, `normalizationRules` | object | no | `{}`. |
| `maskingEnabled`, `requireConfirmation`, `pii` | boolean | no | `false`; PII forces masking on create. |
| `retentionDays` | integer/null | no | 0–3,650. |
| `example` | string | no | `""`; max 300. |
| `tenantId` | string/null | no | Super admin target; tenant users resolve to own tenant. |

`201` returns Entity. Errors: `409` duplicate name; `422` prohibited name,
invalid/missing regex; tenant-resolution `400/403` as above.

### Update entity

`PATCH /api/v1/entities/{entity_id}`

Auth: `manage_entities` or `bots.manage`. Path `entity_id`; no query params.
All create fields except `code`/`tenantId` are optional; `status` additionally
accepts `active`, `disabled`, `archived`. Same bounds apply.

```json
{"requireConfirmation":true,"maskingEnabled":true,"status":"active"}
```

`200` returns Entity. Errors: `404`; `409` duplicate new name; `422` prohibited
name or invalid regex.

### Duplicate entity

`POST /api/v1/entities/{entity_id}/duplicate`

Auth: `manage_entities` or `bots.manage`. Path `entity_id`; no body/query.
`201` returns a uniquely named clone with `status: "disabled"`; `404` unknown.

### Archive entity

`DELETE /api/v1/entities/{entity_id}`

Auth: `manage_entities` or `bots.manage`. Path `entity_id`. Query
`hard=false` is optional boolean and guarded. `200` returns
`{"archived":true,"id":"<ENTITY_ID>"}`. `409` if an intent still uses it;
`403` hard-delete disabled; `404` unknown.

### Test entity extraction

`POST /api/v1/entities/{entity_id}/test`

Auth: authenticated user. Path `entity_id`; no query params. Body field `text`
is required string, 1–1,000 characters.

```json
{"text":"I will pay on 15 August"}
```

`200` returns the extractor result (matched value, normalized value, validity,
and implementation-specific match detail); `404` unknown entity; `422` invalid body.

## Workflows

The executable node kinds are `start`, `message`, `ask`, `intent`, `condition`,
`api`, `knowledge`, `handover`, and `end`.

### Get bot workflow

`GET /api/v1/bots/{bot_id}/workflow`

Returns the highest-version non-archived workflow. Auth: authenticated member.
Path `bot_id`; no query/body. `200` returns Workflow; `404` bot/workflow.

### List workflows

`GET /api/v1/workflows`

Auth: authenticated user. Query `tenantId` optional for tenant users and
required for a platform admin; no body. `200` returns Workflow array. Tenant
resolution errors: `400/403`.

### Save bot workflow

`PUT /api/v1/bots/{bot_id}/workflow`

Creates or merge-updates the bot workflow and increments its version. Auth:
tenant admin (`super_admin` or `tenant_admin`). Path `bot_id`; no query params.

```json
{
  "name":"Main journey",
  "nodes":[
    {"id":"start","kind":"start","config":{}},
    {"id":"hello","kind":"message","config":{"message":"Hello"}},
    {"id":"done","kind":"end","config":{}}
  ],
  "edges":[{"id":"e1","from":"start","to":"hello"},{"id":"e2","from":"hello","to":"done"}],
  "issues":[],
  "status":"pending_approval"
}
```

| Field | Type | Required | Validation |
| --- | --- | --- | --- |
| `name` | string/null | no | Max 200. |
| `nodes` | object[]/null | no | IDs required and unique; one start when non-empty; kind must be supported. |
| `edges` | object[]/null | no | `from`/`to` must identify existing nodes. |
| `issues` | object[]/null | no | Accepted for compatibility but ignored; server recomputes issues. |
| `status` | string/null | no | `draft`, `pending_approval`, `approved`. |

Structural corruption returns `422`. Reachability, dead ends, missing condition
variables, and incomplete branches are stored as `issues` rather than always
blocking a draft. `200` returns Workflow; `404` bot.

## Releases

### List releases

`GET /api/v1/bots/{bot_id}/releases`

Auth: authenticated member. Path `bot_id`; no query/body. `200` returns Release
array newest first; `404` bot.

### Create release

`POST /api/v1/bots/{bot_id}/releases`

Creates a release in `review` with a server-built readiness checklist. Auth:
tenant admin. Path `bot_id`; no query params.

```json
{"version":"v1.2.0","notes":"Prompt and workflow update","diff":[],"scheduledFor":null}
```

| Field | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `version` | string | yes | 1–20. |
| `notes` | string | no | `""`; max 2,000. |
| `diff` | object[] | no | `[]`. |
| `scheduledFor` | ISO-8601 datetime/null | no | `null`. |

`201` returns Release; `404` bot; `422` invalid body.

### Change release stage

`PATCH /api/v1/releases/{release_id}`

Auth: tenant admin. Path `release_id`; no query params. Body has one required
`stage`: `draft`, `review`, `approved`, `published`, or `rolled_back`.

```json
{"stage":"approved"}
```

Allowed transitions are `draft→review`, `review→approved|draft`,
`approved→published|draft`, and `published→rolled_back`. Publishing rebuilds
the checklist and returns `422` unless every item passes. `200` returns Release;
`404` unknown; `422` invalid transition/incomplete checklist.

## Testing APIs

### List test scenarios

`GET /api/v1/bots/{bot_id}/scenarios`

Auth: authenticated member. Path `bot_id`; no query/body. `200` returns Scenario
array; `404` bot.

### Create test scenario

`POST /api/v1/bots/{bot_id}/scenarios`

Auth: tenant admin. Path `bot_id`; no query params.

```json
{"name":"Promise-to-pay happy path","suite":"Collections regression","steps":4}
```

`name` is required string 1–200; `suite` is optional string default `General`,
max 100; `steps` is optional integer default 1, range 1–100. `201` returns
Scenario; `404` bot; `422` invalid body.

### Run regression suite

`POST /api/v1/bots/{bot_id}/scenarios/run`

Marks every saved scenario as executed and preserves its prior pass/fail data;
this route does not attach a live call engine. Auth: tenant admin. Path
`bot_id`; no body/query.

```json
{"success":true,"data":{"passed":3,"failed":1,"total":4,"at":"2026-08-07T10:00:00Z"}}
```

`200` returns the aggregate. `404` if the bot or scenario set does not exist.

### Chat tester

`POST /api/v1/bots/{bot_id}/testing/chat`

Runs one text turn through the real router/workflow/RAG/LLM stack. Auth:
authenticated member. Path `bot_id`; no query params. Redis retains the active
workflow marker for 1,800 seconds; pass the returned `sessionId` for later turns.

```json
{
  "message":"I can pay on Friday",
  "sessionId":"test_session_1",
  "messages":[{"role":"assistant","content":"When can you pay?"}],
  "language":"en-IN"
}
```

| Field | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `message` | string | yes | 1–1,000. |
| `sessionId` | string/null | no | Generated if absent; max 64. |
| `messages` | object[] | no | `[]`, max 40; each `role` is `user`/`assistant`, `content` 1–4,000. |
| `language` | string/null | no | Bot/default language when absent; max 15. |

```json
{
  "success":true,
  "data":{"sessionId":"test_session_1","route":"workflow","action":"promise_to_pay","matchedIntent":"promise_to_pay","confidence":0.86,"reason":"...","reply":"What date will you pay?","done":false,"language":"en-IN","latencyMs":42,"at":"2026-08-07T10:00:00Z","activeWorkflow":"promise_to_pay","workflow":{"name":"promise_to_pay","source":"database","status":"running","workflowId":"<WORKFLOW_ID>","nodeTrace":[],"slots":{},"offScript":false,"signal":null,"done":false}}
}
```

`200` returns the turn trace; provider failures degrade to a canned safe reply.
`404` bot; `422` invalid body.

### Full turn simulator

`POST /api/v1/bots/{bot_id}/testing/simulate`

Executes one full runtime turn with real routing, runtime context, hybrid intent
classification, policy, workflow, and LLM. Audio is omitted and outbound tools
use `mockToolResults`. Auth: authenticated member. Path `bot_id`; no query params.

```json
{
  "message":"I paid yesterday",
  "messages":[],
  "promptId":"<PROMPT_ID>",
  "promptVersion":2,
  "contextSource":"manual",
  "contextPayload":{"customer_name":"Example Customer"},
  "language":"en-IN",
  "isFinal":true,
  "interrupted":false,
  "mockToolResults":{"check_payment":{"paid":true}},
  "sessionId":"sim_session_1"
}
```

| Field | Type | Required | Default / validation |
| --- | --- | --- | --- |
| `message` | string | yes | 1–2,000. |
| `messages` | object[] | no | `[]`; max 40. Runtime expects `{role,content}` items. |
| `promptId` | string/null | no | Published system prompt when absent; selected prompt must belong to bot. |
| `promptVersion` | integer/null | no | Published/active version; at least 1. |
| `contextSource` | string | no | `saved`; `saved`, `manual`, `api_mock`, `none`. |
| `contextPayload` | object/null | no | Validated against the bot context schema for manual/API-mock modes. |
| `language` | string | no | `""`; max 15, then resolved/detected. |
| `isFinal` | boolean | no | `true`; partials are held and do not route. |
| `interrupted` | boolean | no | `false`. |
| `mockToolResults` | object | no | `{}`; maps tool name to mock response. |
| `sessionId` | string/null | no | Generated if absent; max 64. |

`200` returns a dynamic trace. Common fields are `rawTranscript`, `isFinal`,
`interrupted`, `botVersion`, `finalTranscript`, `runtimeContext`, prompt
provenance, rendered prompt, `route`, `intent`, `signal`, `routerDecision`,
`policy`, `tool`, `workflow`, `response`, `language`, `sessionId`, `provider`,
`latencyMs`, and `disposition`. A partial transcript returns the smaller shape:

```json
{"success":true,"data":{"rawTranscript":"I pa","isFinal":false,"interrupted":false,"botVersion":"v1.2.0","finalTranscript":null,"heldForFinal":true,"route":null,"response":null,"note":"Partial transcript ...","latencyMs":0}}
```

Errors: `404` bot/prompt; `422` invalid prompt version, runtime-context payload,
or request fields. Tool HTTP is never performed by this endpoint.

## Implementation notes and compatibility

- Prompt, intent, entity, workflow, release, and scenario responses are built
  by `backend/serializers.py`, not by echoing request models.
- Prompt tests and both Testing Studio endpoints can call billable providers.
- Workflow `issues` submitted by a client are ignored; server validation is the
  authoritative source.
- `PATCH` models mostly ignore explicit `null` values because handlers apply
  only non-null fields. To clear a field, use an endpoint-specific supported
  empty value or update the underlying authoring object as documented.
