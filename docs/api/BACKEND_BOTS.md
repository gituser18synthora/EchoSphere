# EchoSphere Backend API — Bots & Studio Configuration

This document covers the bot-configuration surface of the EchoSphere backend (FastAPI):
VoiceBot CRUD, voice settings, channels, prompts, intents & entities, workflows, releases,
runtime context, customer collection contexts, testing (scenarios / chat tester / simulator),
API connections, platform templates, and knowledge gaps.

**Base URL:** `http://localhost:9001` — every route below is mounted under the `/api/v1` prefix.

**Authentication:** JWT bearer token on every endpoint unless explicitly marked public:

```
Authorization: Bearer <ACCESS_TOKEN>
```

- `401` — missing/invalid/expired token, deactivated account, or a token issued before the
  user's last password change.
- `403` — authenticated but lacking the required role/permission.
- Roles: `super_admin`, `tenant_admin`, `tenant_user`. "Tenant admin required" below means
  the endpoint uses `require_tenant_admin` (allows `super_admin` OR `tenant_admin`).
  "Permission: `x`" means `require_permission(...)` — the user's role must hold at least one
  of the listed permission codes (role-permission seeds; super admins are seeded with every
  permission, there is no implicit bypass in code).

**Tenant scoping:** tenant roles are always scoped to their own tenant — a client-supplied
`tenantId` is honored only for super admins (who MUST pass it on tenant-scoped list
endpoints, else `400 "tenant_id is required for platform administrators."`). Direct-by-id
access to another tenant's row returns **404, never 403** (existence is not leaked).

**Response envelope** (all JSON endpoints):

```json
{ "success": true, "data": { }, "meta": { } }          // success; meta optional
{ "success": false, "message": "…", "errors": [ { "field": "…", "message": "…" } ] }  // error
```

Paginated lists return `data` as an array plus
`meta: { "page": 1, "pageSize": 50, "total": 123, "totalPages": 3 }`.

**Shared pagination query params** (endpoints marked *paginated*):

| Param | Type | Default | Notes |
|---|---|---|---|
| `page` | int | `1` | ≥ 1 |
| `pageSize` | int | `50` | 1–200 |
| `search` | string | — | max 200 chars; per-endpoint match columns noted below |
| `sortBy` | string | — | max 50 chars; **accepted but ignored** by the endpoints in this document (ordering is fixed per endpoint) |
| `sortDir` | string | `desc` | `asc` \| `desc`; also ignored here |

**Field naming:** request bodies are Pydantic models with `populate_by_name = True` and
camelCase aliases — every aliased field accepts **either** the camelCase alias (e.g.
`useCase`, `sttProvider`) **or** the snake_case Python name (`use_case`, `stt_provider`).
Responses are always camelCase. Models noted with *extra: forbid* reject unknown keys with 422;
all others silently ignore unknown keys.

**Soft delete:** every `DELETE` in this document archives (soft-deletes) the row and returns
`200` with `{"archived": true, ...}` or `{"deleted": true}` — never `204`. All of them accept
`?hard=true` (boolean, default `false`); when `ALLOW_HARD_DELETE` is off (the default) that
returns `403 "Permanent deletion is disabled in the development environment."`. Note: even
when the guard passes, the handlers still only soft-delete — `hard=true` never physically
removes the row.

**Secrets are never stored raw.** Channel credential fields accept only `env:VAR_NAME`
references; API-connection credentials accept only `secret://…` references. Values are
resolved server-side from the environment at use time and never echoed back.

Placeholders used below: `<ACCESS_TOKEN>`, `<BOT_ID>`, `<TENANT_ID>`, `<PROMPT_ID>`,
`<INTENT_ID>`, `<ENTITY_ID>`, `<WORKFLOW_ID>`, `<RELEASE_ID>`, `<RECORD_ID>`, `<CONTEXT_ID>`,
`<CONN_ID>`, `<CHANNEL_ID>`, `<USER_ID>`, `<VOICE_ID>`, `<KB_ID>`.

---

## Table of contents

1. [VoiceBots](#voicebots)
   - [List bots](#list-bots) · [Get bot](#get-bot) · [Create bot](#create-bot) · [Update bot](#update-bot) · [Archive bot](#archive-bot)
2. [Voice settings](#voice-settings)
   - [Get voice settings](#get-voice-settings) · [Update voice settings](#update-voice-settings)
3. [Channels](#channels)
   - [List bot channels](#list-bot-channels) · [Get channel](#get-channel) · [Configure channel](#configure-channel-upsert) · [Activate](#activate-channel) · [Deactivate](#deactivate-channel) · [Archive channel](#archive-channel) · [Test channel](#test-channel-connection) · [Platform channel summary](#platform-channel-summary) · [WhatsApp webhook verify](#whatsapp-webhook-verification-meta-handshake) · [WhatsApp webhook events](#whatsapp-webhook-inbound-events)
4. [Prompts](#prompts)
   - [List prompts](#list-bot-prompts) · [Create prompt](#create-prompt) · [Add version](#add-prompt-version) · [Compile preview](#compile-preview-stateless) · [Render preview](#render-saved-version-preview) · [Duplicate](#duplicate-prompt) · [Update / lifecycle](#update-prompt--lifecycle) · [Archive](#archive-prompt) · [Test prompt](#test-prompt)
5. [Intents](#intents)
   - [List](#list-bot-intents) · [Create](#create-intent) · [Update](#update-intent) · [Duplicate](#duplicate-intent) · [Archive](#archive-intent) · [Test utterance](#test-intents-routing-console)
6. [Entities](#entities)
   - [List](#list-entities) · [Create](#create-entity) · [Update](#update-entity) · [Duplicate](#duplicate-entity) · [Archive](#archive-entity) · [Test](#test-entity-extraction)
7. [Workflows](#workflows)
   - [Get bot workflow](#get-bot-workflow) · [List workflows](#list-workflows) · [Save bot workflow](#save-bot-workflow)
8. [Releases](#releases)
   - [List](#list-releases) · [Create](#create-release) · [Change stage](#change-release-stage)
9. [Runtime context](#runtime-context)
   - [Get config](#get-runtime-context-config) · [Save config](#save-runtime-context-config) · [Validate payload](#validate-context-payload) · [List records](#list-context-records) · [Create record](#create-context-record) · [Update record](#update-context-record) · [Delete record](#delete-context-record)
10. [Customer contexts (collections)](#customer-contexts-collections)
    - [List](#list-customer-contexts) · [Lookup by phone](#lookup-customer-context-by-phone) · [Get](#get-customer-context) · [Create](#create-customer-context) · [Update](#update-customer-context) · [Update call state](#update-call-state) · [Delete](#delete-customer-context)
11. [Testing](#testing)
    - [List scenarios](#list-test-scenarios) · [Create scenario](#create-test-scenario) · [Run suite](#run-regression-suite) · [Chat tester](#chat-tester) · [Full turn simulator](#full-turn-simulator)
12. [API connections](#api-connections)
    - [List](#list-api-connections) · [Create](#create-api-connection) · [Update](#update-api-connection) · [Duplicate](#duplicate-api-connection) · [Archive](#archive-api-connection) · [Test](#test-api-connection)
13. [Templates](#templates)
14. [Knowledge gaps](#knowledge-gaps)
15. [Findings / behavior notes](#findings--behavior-notes)

---

## VoiceBots

Router: `backend/routers/bots.py`. Bot rows are tenant-owned; every by-id access checks
tenant membership (404 across tenants).

### List bots
`GET /api/v1/bots`

List VoiceBots, tenant-scoped and paginated. **Auth:** any authenticated user.
Super admins without `tenantId` get the platform-wide view (all tenants).

Query params: shared pagination params (`search` matches `name` / `useCase` with `LIKE`;
ordering fixed `created_at ASC`), plus:

| Param | Type | Default | Description |
|---|---|---|---|
| `tenantId` | string | — | Super admin: target tenant (omit for all tenants). Tenant roles: must equal own tenant or be omitted (else 403). |
| `status` | string | — | Exact-match filter on bot status (`draft`, `in_review`, `approved`, `published`, `rolled_back`, `archived`). |

**Response 200** — paginated array of bots:

```json
{
  "success": true,
  "data": [
    {
      "id": "<BOT_ID>",
      "tenantId": "<TENANT_ID>",
      "name": "Collections Bot",
      "useCase": "Loan collections",
      "description": "",
      "languages": ["hi-IN", "en-IN"],
      "status": "published",
      "version": "v1.2.0",
      "liveVersion": "v1.2.0",
      "owner": "Asha Rao",
      "health": "healthy",
      "containment": 82,
      "callsToday": 14,
      "callsMonth": 310,
      "avgCostPerCall": 0.0421,
      "csat": 4.4,
      "channels": ["voice", "whatsapp"],
      "voiceId": "<VOICE_ID>",
      "updatedAt": "2026-08-07T09:12:44Z",
      "publishedAt": "2026-08-01T10:00:00Z",
      "readiness": [
        { "id": "r1", "label": "Knowledge sources indexed", "done": true, "studioTab": "knowledge" },
        { "id": "r2", "label": "Voice selected & tuned", "done": true, "studioTab": "voice" },
        { "id": "r3", "label": "Core prompts approved", "done": true, "studioTab": "prompts" },
        { "id": "r4", "label": "Intents validated", "done": false, "studioTab": "intents" },
        { "id": "r5", "label": "Workflow published", "done": true, "studioTab": "workflows" },
        { "id": "r6", "label": "Channel connected", "done": true, "studioTab": "channels" },
        { "id": "r7", "label": "Regression suite passing", "done": false, "studioTab": "testing" }
      ]
    }
  ],
  "meta": { "page": 1, "pageSize": 50, "total": 1, "totalPages": 1 }
}
```

`callsToday` / `callsMonth` / `avgCostPerCall` come from the `usage_records` metering rollup
(month-to-date AI cost per answered call); `channels` lists channel types with status
`live` / `configured` / `testing`; `owner` is the resolved owner user's display name (`"—"`
when unset).

### Get bot
`GET /api/v1/bots/{bot_id}`

Fetch one bot. **Auth:** any authenticated user (tenant access enforced).
Path param: `bot_id`. **Response 200** — `data` is a single bot object (same shape as the
list item above). **404** — unknown/archived bot or another tenant's bot.

### Create bot
`POST /api/v1/bots`

Create a draft VoiceBot, its 7 default readiness items and a starter workflow
(`start → Greeting → end`). **Auth:** tenant admin (role `super_admin` or `tenant_admin`).

```json
{
  "name": "Collections Bot",
  "useCase": "Loan collections",
  "description": "DPD 0-30 outreach",
  "languages": ["hi-IN", "en-IN"],
  "tenantId": "<TENANT_ID>"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | 1–200 chars. |
| `useCase` (`use_case`) | string | no | Default `""`, max 200. |
| `description` | string | no | Default `""`, max 2000. |
| `languages` | string[] | no | Locale codes. Every code must be an **enabled** platform language (422 otherwise; duplicates deduped; empty list → 422 "At least one supported language is required."). Omitted/`null` → the current enabled platform default language. |
| `tenantId` (`tenant_id`) | string | no | Super admin: required target tenant (400 when omitted). Tenant admin: own tenant is used; supplying a different one → 403. 404 if the tenant does not exist. |

**Response 201** — the created bot (status `draft`, version `v0.1.0`, `health: "neutral"`,
owner = the caller). Audited as "Created VoiceBot".

Errors: `422` unknown/disabled language; `404` unknown tenant.

### Update bot
`PATCH /api/v1/bots/{bot_id}`

Partial update of bot metadata, status, languages, voice, owner and readiness flags.
**Auth:** tenant admin. Only fields present (non-null) are applied.

```json
{
  "name": "Collections Bot v2",
  "useCase": "Loan collections",
  "description": "…",
  "status": "published",
  "languages": ["hi-IN"],
  "voiceId": "<VOICE_ID>",
  "ownerUserId": "<USER_ID>",
  "readiness": { "r4": true, "r7": false }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | no | Max 200. |
| `useCase` (`use_case`) | string | no | Max 200. |
| `description` | string | no | Max 2000. |
| `status` | string | no | `draft` \| `in_review` \| `approved` \| `published` \| `rolled_back` \| `archived`. Transitioning to `published` stamps `publishedAt` and sets `liveVersion = version`. No transition matrix here (see [Releases](#releases) for the governed pipeline). |
| `languages` | string[] | no | Same validation as create; association rows are diffed (removed codes deleted, new ones added). |
| `voiceId` (`voice_id`) | string | no | Must be an existing voice profile (422 "Unknown voice profile."). Empty string clears the voice. Note: this PATCH checks existence only — the tenant-scope/active checks are on the voice-settings PUT. |
| `ownerUserId` (`owner_user_id`) | string | no | Must be a user of the same tenant (or a platform user with no tenant), else 422. |
| `readiness` | object | no | Map of readiness `item_key` (`r1`…`r7`) → boolean `done`. Unknown keys ignored. |

**Response 200** — the updated bot object. Audited.

### Archive bot
`DELETE /api/v1/bots/{bot_id}`

Soft-delete (archive) a bot. **Auth:** tenant admin.
Query: `hard` (bool, default `false`, see soft-delete note in the intro).

**Response 200:** `{"success": true, "data": {"archived": true, "id": "<BOT_ID>"}}`

---

## Voice settings

Per-bot STT/TTS/LLM provider configuration, delivery tuning, turn detection and the Goal
Engine policy. Router: `backend/routers/bots.py` (`VoiceBotSetting` row, one per bot).

### Get voice settings
`GET /api/v1/bots/{bot_id}/voice-settings`

Fetch (and lazily create with defaults if absent) the bot's voice settings.
**Auth:** any authenticated user (tenant access enforced).

**Response 200:**

```json
{
  "success": true,
  "data": {
    "botId": "<BOT_ID>",
    "voiceId": "<VOICE_ID>",
    "speed": 1.0,
    "pauseMs": 300,
    "empathy": 60,
    "energy": 50,
    "languageVoiceMap": {
      "default": "hi-IN",
      "hi-IN": { "provider": "sarvam", "model": "bulbul:v2", "voice": "anushka", "params": { "pitch": 0 } },
      "en-IN": "<VOICE_ID>"
    },
    "sttProvider": "deepgram",
    "sttModel": "flux-general-en",
    "sttLanguage": "hi-IN",
    "sttSettings": {
      "turn_detection": { "confidence": 0.6, "stop_secs": 0.2, "user_speech_timeout": 0.7 },
      "noise_gate": { "noise_margin_db": 8.0, "min_speech_ms": 120.0 }
    },
    "ttsProvider": "sarvam",
    "ttsModel": "bulbul:v2",
    "ttsVoice": "anushka",
    "ttsSettings": { "pitch": 0 },
    "llmProvider": "openai",
    "llmModel": "gpt-4o-mini",
    "llmSettings": { "temperature": 0.3 },
    "fallbackProvider": "elevenlabs",
    "fallbackModel": "eleven_flash_v2_5",
    "fallbackVoice": "<VOICE_ID>",
    "audioSettings": {},
    "goalPolicy": {}
  }
}
```

Read-side sanitization: legacy per-provider speed keys (`pace`, `speed`) are stripped from
`ttsSettings` and from each `languageVoiceMap` entry's `params` — the top-level `speed`
(delivery tuning) is the single canonical speed control.

### Update voice settings
`PUT /api/v1/bots/{bot_id}/voice-settings`

Merge-update the bot's voice settings. **Auth:** tenant admin.
Despite being a PUT, semantics are *merge*: only non-null fields in the body are persisted;
however validation always runs against the **effective** configuration (current row overlaid
with the update) against the DB provider catalog — frontend field-hiding is never trusted.

```json
{
  "voiceId": "<VOICE_ID>",
  "speed": 1.05,
  "pauseMs": 250,
  "empathy": 70,
  "energy": 55,
  "languageVoiceMap": {
    "default": "hi-IN",
    "hi-IN": { "provider": "sarvam", "model": "bulbul:v2", "voice": "anushka" }
  },
  "sttProvider": "deepgram",
  "sttModel": "flux-general-en",
  "sttLanguage": "hi-IN",
  "sttSettings": { "turn_detection": { "user_speech_timeout": 0.7 } },
  "ttsProvider": "sarvam",
  "ttsModel": "bulbul:v2",
  "ttsVoice": "anushka",
  "ttsSettings": {},
  "llmProvider": "openai",
  "llmModel": "gpt-4o-mini",
  "llmSettings": { "temperature": 0.3 },
  "fallbackProvider": "elevenlabs",
  "fallbackModel": "eleven_flash_v2_5",
  "fallbackVoice": "<VOICE_ID>",
  "audioSettings": {},
  "goalPolicy": { "role": "collections agent", "goals": [{ "id": "primary", "description": "secure a payment or promise-to-pay" }] }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `voiceId` (`voice_id`) | string | no | Voice profile id. Must exist, not be deleted, belong to the platform or **this bot's tenant** (another tenant's clone → 422 "Unknown voice profile."), and be `active` (inactive → 422 with a `voiceId` field error). Empty string clears; also mirrors onto the bot row and marks readiness `r2` done. |
| `speed` | float | no | 0.5–2.0. Canonical speaking speed (delivery tuning). |
| `pauseMs` (`pause_ms`) | int | no | 0–5000. |
| `empathy` | int | no | 0–100. |
| `energy` | int | no | 0–100. |
| `languageVoiceMap` (`language_voice_map`) | object | no | Per-locale voice selection. Keys are locale codes plus the special key `"default"` (the bot's default locale string). Values: either a legacy voice-profile id string, or `{ "provider": string, "model": string, "voice": string, "params"?: object }`. Legacy speed keys inside `params` are stripped on save. Validated against the DB voice catalog. |
| `sttProvider` / `sttModel` / `sttLanguage` | string | no | Max 40 / 80 / 15 chars. Provider must exist in the runtime registry (422 "Unknown stt provider '…'.") **and** in the DB catalog; model must belong to the provider; language must be an active platform language (a previously-persisted, unchanged language that was later disabled is grandfathered with a warning; a *new* selection of it is rejected). |
| `sttSettings` (`stt_settings`) | object | no | Provider parameters validated against the model's `params_schema`, **plus** two platform-owned sections validated against shared runtime bounds (see tables below): `turn_detection`, `noise_gate`. |
| `ttsProvider` / `ttsModel` / `ttsVoice` | string | no | Max 40 / 80 / 80. Same registry + catalog validation; voice must belong to the model. |
| `ttsSettings` (`tts_settings`) | object | no | Provider params; `pace` / `speed` keys stripped before validation and persistence. |
| `llmProvider` / `llmModel` | string | no | Max 40 / 80. Registry + catalog validation. |
| `llmSettings` (`llm_settings`) | object | no | Provider params plus platform LLM keys with bounds enforcement (e.g. token/temperature limits per catalog schema). |
| `fallbackProvider` / `fallbackModel` / `fallbackVoice` | string | no | Fallback **TTS** engine (validated as a TTS provider). Max 40 / 80 / 80. |
| `audioSettings` (`audio_settings`) | object | no | Free-form audio pipeline settings. |
| `goalPolicy` (`goal_policy`) | object | no | Goal Engine configuration; must parse into `BotGoalPolicy` (422 with a `goalPolicy` field error otherwise). Send `{}` to clear back to the derived default. See schema below. |

**`sttSettings.turn_detection`** — platform-owned end-of-turn timing. All keys optional
numbers; unknown keys are rejected. Bounds (defaults differ per transport — browser /
telephony — and are applied at runtime):

| Key | Bounds | Purpose |
|---|---|---|
| `confidence` | 0.3–0.95 | VAD confidence threshold. |
| `start_secs` | 0.1–1.0 | Speech-start window. |
| `stop_secs` | 0.1–2.0 | Speech-stop window. |
| `min_volume` | 0.0–1.0 | Minimum VAD volume. |
| `barge_in_min_words` | 0–10 | Words STT must transcribe before the caller can interrupt the bot mid-reply (0 = any voice activity interrupts). |
| `user_speech_timeout` | 0.2–3.0 | Pause (s) after an incomplete utterance before the turn closes. |
| `finalize_grace` | 0.0–1.5 | Debounce for straggler STT finals. |
| `finalize_settle` | 0.0–1.0 | Staleness of the newest final that skips the debounce. |
| `complete_endpoint` | 0.1–1.5 | Endpoint (s) for utterances that read as finished thoughts. |
| `short_reply_endpoint` | 0.0–1.0 | Endpoint for self-contained short replies ("haan", "ok"). |

**`sttSettings.noise_gate`** — energy gate in front of the VAD (per-transport defaults):

| Key | Bounds |
|---|---|
| `enabled` | 0–1 |
| `noise_margin_db` | 3–24 |
| `min_speech_ms` | 40–500 |
| `echo_min_speech_ms` | 40–800 |
| `hangover_ms` | 100–1500 |
| `preroll_ms` | 0–600 |
| `echo_margin_db` | 0–24 |
| `echo_tail_ms` | 0–1500 |
| `min_threshold_dbfs` | −70 to −20 |

**`goalPolicy`** (`shared/orchestration/goal_engine.py::BotGoalPolicy`; extra keys ignored;
every field accepts camelCase alias or snake_case):

| Field | Type | Default | Description |
|---|---|---|---|
| `role` | string | `""` | Who the bot is. |
| `domain` | string | `""` | Business domain label. |
| `goals` | object[] | `[]` | Each `{ "id": "primary", "description": "", "completion": "" }`. |
| `allowedTopics` | string[] | `[]` | In-scope topics. |
| `restrictedTopics` | string[] | `[]` | Off-limits topics. |
| `identity` | object | `{}` | `{ "requireConfirmation": bool (false), "subject": "the registered customer", "maxAttempts": 1–6 (3) }`. |
| `slots` | object[] | `[]` | Each `{ "name" (≤64), "description", "pattern" (regex or null), "required": bool }`. |
| `toolRules` | string[] | `[]` | Tool usage rules. |
| `escalation` | object | `{}` | `{ "triggers": string[] }`. |
| `completionCriteria` | string[] | `[]` | When the call is done. |
| `tone` | string | `""` | Style instruction. |
| `nextActions` | string[] | `[]` | Extra Next-Best-Action names on top of the platform vocabulary. |
| `outOfScope` | string | `""` | Instruction for redirecting off-goal requests. |
| `safety` | string[] | `[]` | Safety rules. |
| `source` | string | `"derived"` | `configured` \| `derived`. |

**Response 200** — the full serialized settings (same shape as GET). Non-fatal catalog
issues (e.g. missing provider credentials, grandfathered disabled language) are returned as
`meta: { "warnings": ["…"] }`. On success the bot-config cache is invalidated so live
sessions pick the change up immediately.

Errors: `422 "Voice settings are invalid."` with a string list in `errors` (catalog
validation), `422 "Unknown \<kind\> provider '…'."`, `422` invalid `goalPolicy`, `404` bot.

---

## Channels

Per-bot deployment channels — `voice`, `whatsapp`, `web`, `mobile`, `sms` — with provider
configuration, real connection tests, traffic gating and webhooks.
Router: `backend/routers/channels.py`.

Security model:

- **Reads:** any authenticated tenant member. **Mutations:** permission `manage_channels`.
- **Secret-reference model:** credential fields (`authTokenReference`, `apiKeyReference`,
  `webhookSecretReference`) accept only `env:VAR_NAME` references (regex
  `^env:[A-Za-z_][A-Za-z0-9_]*$`); raw secrets are rejected with 422
  ("… must be an environment reference like env:VAR_NAME — raw secrets are never stored").
  On reads, any secret-looking value that is *not* an `env:` reference is defensively masked
  as `••••••••`.
- **Status is server-derived:** saving a config sets `configured`; a connection test promotes
  to `live` (if enabled) or demotes to `failed`. Clients can never claim a channel is live.
  Unconfigured types appear in the list as virtual `not_configured` rows.
- **`enabled` gates traffic:** telephony/WhatsApp webhooks reject disabled channels.

Channel serializer (used by every channel endpoint that returns a channel):

```json
{
  "id": "<CHANNEL_ID>",
  "type": "voice",
  "botId": "<BOT_ID>",
  "status": "configured",
  "enabled": true,
  "detail": "+14155550119 · twilio",
  "workflow": "Collections journey",
  "lastTest": { "at": "2026-08-07T10:11:12.000000+00:00Z", "ok": true, "message": "All checks passed.", "checks": [ { "name": "Configuration valid", "ok": true, "message": "" } ] },
  "config": { "phoneNumber": "+14155550119", "telephonyProvider": "twilio", "publicWsBase": "wss://media.example.com", "authTokenReference": "env:TWILIO_AUTH_TOKEN", "language": "hi-IN", "voiceId": "" },
  "updatedAt": "2026-08-07T10:11:12Z",
  "binding": {
    "tenantId": "<TENANT_ID>",
    "botId": "<BOT_ID>",
    "botName": "Collections Bot",
    "botStatus": "published",
    "publishedVersion": "v1.2.0",
    "systemPromptPublished": true,
    "knowledgeBases": 2,
    "language": "hi-IN",
    "voiceId": "<VOICE_ID>",
    "sttProvider": "deepgram",
    "ttsProvider": "sarvam",
    "llmProvider": "openai"
  }
}
```

### List bot channels
`GET /api/v1/bots/{bot_id}/channels`

All five channel types for a bot, configured or not. **Auth:** any authenticated user.
**Response 200** — `data` is an array of exactly 5 entries in the order
`voice, whatsapp, web, mobile, sms`; unconfigured types are placeholders:

```json
{ "id": null, "type": "sms", "botId": "<BOT_ID>", "status": "not_configured", "enabled": false,
  "detail": "", "workflow": "—", "lastTest": null, "config": null, "updatedAt": null, "binding": { } }
```

### Get channel
`GET /api/v1/bots/{bot_id}/channels/{channel_type}`

One configured channel. **Auth:** any authenticated user.
Path params: `bot_id`, `channel_type` ∈ `voice|whatsapp|web|mobile|sms` (unknown type → 404
"Channel type not found."). **Response 200** — serialized channel. **404** — not configured.

### Configure channel (upsert)
`PUT /api/v1/bots/{bot_id}/channels/{channel_type}`

Create or update a channel's provider configuration. **Auth:** permission `manage_channels`.
Saving always sets status `configured` (a later test promotes/demotes it). Re-configuring an
archived channel revives the archived row (the `(bot, type)` pair is unique).

```json
{ "config": { "phoneNumber": "+14155550119", "telephonyProvider": "twilio",
              "publicWsBase": "wss://media.example.com",
              "authTokenReference": "env:TWILIO_AUTH_TOKEN" },
  "workflowName": "Collections journey" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `config` | object | yes (defaults to `{}` → 422 for required per-type fields) | Provider config validated against the per-type schema below (**extra: forbid** — unknown keys → 422 "Channel configuration is invalid."). |
| `workflowName` (`workflow_name`) | string | no | Display label of the linked journey, max 200. |

**Per-type `config` schemas** (all accept camelCase alias or snake_case):

`voice`:

| Field | Type | Required | Validation |
|---|---|---|---|
| `phoneNumber` | string | yes | 7–30 chars; must normalize to E.164 (`+14155550119`). Claims the number in the routing table — 409 if it belongs to another bot/tenant or is deactivated. Changing the number releases the previous one. |
| `telephonyProvider` | string | yes | `freeswitch` \| `twilio` \| `telnyx` \| `plivo` \| `exotel` \| `vaani`. |
| `publicWsBase` | string | for twilio/telnyx/plivo/exotel | `ws://` or `wss://` media-streaming base URL, max 300. |
| `authTokenReference` | string | for twilio | `env:VAR` reference, max 120. |
| `language` | string | no | Max 15, default `""`. |
| `voiceId` | string | no | Max 40, default `""`. |

`whatsapp`:

| Field | Type | Required | Validation |
|---|---|---|---|
| `whatsappNumber` | string | yes | E.164, 7–30 chars. |
| `provider` | string | no | `meta` (default) \| `twilio` \| `pinbot`. |
| `phoneNumberId` | string | for meta | Max 60. |
| `businessAccountId` | string | no | Max 60. |
| `apiKeyReference` | string | yes | `env:VAR`, max 120. |
| `webhookSecretReference` | string | for meta | `env:VAR`, max 120 (verify token / HMAC secret for the webhook below). |

`web`:

| Field | Type | Required | Validation |
|---|---|---|---|
| `allowedOrigins` | string[] | yes | 1–20 origins, each `http(s)://host[:port]`. |
| `widgetColor` | string | no | `#RRGGBB` hex. |
| `language` | string | no | Max 15. |

`mobile`:

| Field | Type | Required | Validation |
|---|---|---|---|
| `platform` | string | no | `ios` \| `android` \| `both` (default `both`). |
| `bundleIds` | string[] | yes | 1–20 reverse-DNS bundle ids. |
| `apiKeyReference` | string | no | `env:VAR`, max 120. |

`sms`:

| Field | Type | Required | Validation |
|---|---|---|---|
| `provider` | string | yes | `twilio` \| `plivo` \| `telnyx` \| `exotel`. |
| `senderId` | string | yes | 3–20 chars: 3–15 alphanumeric id or E.164 number. |
| `accountId` | string | for twilio | Account SID, max 60. |
| `apiKeyReference` | string | yes | `env:VAR`, max 120. |

**Response 200** — the serialized channel (`status: "configured"`). Audited (credential
reference changes are audited as "Updated channel credentials").
Errors: `404` bot / unknown type; `422` schema violations; `409` phone number conflicts.

### Activate channel
`POST /api/v1/bots/{bot_id}/channels/{channel_type}/activate`

Enable traffic on a configured channel. **Auth:** permission `manage_channels`. No body.
For `voice` / `whatsapp` / `sms` the bot must be `published`
(422 "Publish the bot before activating this channel — …"). 422 if the channel has no
config yet. **Response 200** — serialized channel with `enabled: true`.

### Deactivate channel
`POST /api/v1/bots/{bot_id}/channels/{channel_type}/deactivate`

Disable traffic. **Auth:** permission `manage_channels`. No body. Sets `enabled: false`
and demotes status `live` → `configured`. **Response 200** — serialized channel.

### Archive channel
`DELETE /api/v1/bots/{bot_id}/channels/{channel_type}`

Soft-delete a channel and (for `voice`) release its phone number back to the pool.
**Auth:** permission `manage_channels`. **Response 200:** `{"archived": true}`.

### Test channel connection
`POST /api/v1/bots/{bot_id}/channels/{channel_type}/test`

Run real, provider-aware connectivity checks and persist the result on the channel.
**Auth:** permission `manage_channels`. No body.

Checks by type (each check is `{name, ok, message}`; no fabricated successes):

- all: stored config re-validated against the current schema;
- voice/whatsapp/sms: bot has a published release;
- voice: phone-number routing mapping, voice-runtime `/health`, FreeSWITCH event socket
  (freeswitch) or Twilio auth-token reference resolution (twilio);
- whatsapp: API-key/webhook-secret references resolve; live `GET graph.facebook.com/v20.0/{phoneNumberId}` for meta;
- sms: API-key reference resolves; live Twilio account fetch for twilio;
- web/mobile: voice-runtime reachability; mobile also checks the SDK key reference.

On success status becomes `live` (if `enabled`, else `configured`); on failure `failed`.
`lastTest` is stored (`{at, ok, message, checks}`). **Response 200** — the serialized channel.

### Platform channel summary
`GET /api/v1/channels/summary`

Platform-wide per-type status counts. **Auth:** super admin only (403 otherwise).

**Response 200:**

```json
{ "success": true, "data": [
  { "type": "voice", "live": 3, "testing": 0, "failed": 1, "configured": 2 },
  { "type": "whatsapp", "live": 1, "testing": 0, "failed": 0, "configured": 0 },
  { "type": "web", "live": 0, "testing": 0, "failed": 0, "configured": 1 },
  { "type": "mobile", "live": 0, "testing": 0, "failed": 0, "configured": 0 },
  { "type": "sms", "live": 0, "testing": 0, "failed": 0, "configured": 0 }
] }
```

### WhatsApp webhook verification (Meta handshake)
`GET /api/v1/channels/whatsapp/webhook/{channel_id}`

**Public — no bearer token.** Meta webhook subscription handshake: echoes the challenge only
when the verify token matches the channel's configured secret (constant-time compare against
the value resolved from the channel's `webhookSecretReference`).

Path param: `channel_id` — the WhatsApp channel's id.
Query params (Meta's literal dotted names):

| Param | Type | Required | Description |
|---|---|---|---|
| `hub.mode` | string | yes (default `""`) | Must be `subscribe`. |
| `hub.verify_token` | string | yes (default `""`) | Must equal the resolved webhook secret. |
| `hub.challenge` | string | yes (default `""`) | Echoed back on success. |

**Response 200** — `text/plain` body containing the challenge string (no JSON envelope).
Errors: `404 "Unknown channel."` (missing/archived/non-whatsapp/unconfigured channel),
`503 "Channel webhook secret is not configured."`, `403 "Verification failed."`.

### WhatsApp webhook (inbound events)
`POST /api/v1/channels/whatsapp/webhook/{channel_id}`

**Public — no bearer token; HMAC-signature verified.** Inbound WhatsApp events (Meta Cloud
API style). Verification pipeline, in order:

1. Channel must exist, be a configured WhatsApp channel (else `404 "Unknown channel."`) and
   have a resolvable webhook secret (else `503`).
2. Header `X-Hub-Signature-256: sha256=<hex>` must be the HMAC-SHA256 of the **raw** request
   body keyed with the resolved secret (missing/mismatched → `403 "Missing signature header"`
   / `403 "Invalid webhook signature"`).
3. Replay protection: the signature is single-use within the freshness window (Redis-backed;
   replays → `403 "Webhook replay detected"`; on a Redis outage it fails **open** and logs).
4. Channel must be `enabled` and its bot `published` (else `403 "This channel is not
   accepting messages."`); dangling bot mapping → `404`.

Body: the raw Meta event JSON (opaque to verification; message processing is a separate
pipeline — events are acknowledged so the provider stops retrying).

**Response 200:** `{"success": true, "data": {"received": true}}`

<!-- PART2 -->
