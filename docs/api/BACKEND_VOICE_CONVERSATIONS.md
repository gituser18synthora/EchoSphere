# EchoSphere Backend API — Voice, Providers, Conversations & Telephony

Documentation for the voice-provider catalog, voice cloning, voice sessions,
conversation review, and telephony surface of the EchoSphere control-plane API
(FastAPI). The current routers, schemas, services, ORM models, and serializers
are the source of truth.

**Base URL:** `http://localhost:9001` — all routes below are prefixed with `/api/v1`.

**Authentication:** JWT bearer token on every endpoint except the telephony webhook (which is HMAC-signed, see its section):

```
Authorization: Bearer <ACCESS_TOKEN>
```

**Response envelope** (`backend/core/responses.py`):

```json
// success
{ "success": true, "data": <payload>, "meta": { "page": 1, "pageSize": 50, "total": 120, "totalPages": 3 } }

// error (backend/shared/errors.py)
{ "success": false, "message": "Voice name is required.", "errors": [ { "field": "name", "message": "Voice name is required." } ] }
```

`meta` is present only on paginated lists; `errors` only when field-level detail exists. Validation failures return `422` with `message: "Validation failed."` and one `errors[]` entry per offending field.

**Roles & permissions:** `get_current_user` = any authenticated user. `require_tenant_member` = roles `super_admin | tenant_admin | tenant_user`. `require_tenant_admin` = `super_admin | tenant_admin`. `require_super_admin` = `super_admin` only. `require_permission("a", "b")` passes when the user's role holds **at least one** of the listed permission codes. Tenant-scoped rows are guarded with `assert_tenant_access` — cross-tenant access returns **404** (never 403) so existence is not leaked. Super admins may target any tenant via an explicit `tenantId` parameter; other roles get 403 if they pass a foreign `tenantId`, and for super admins `tenantId` is **required** on tenant-scoped list endpoints (400 otherwise).

**Placeholders used below:** `<ACCESS_TOKEN>`, `<TENANT_ID>` (e.g. `tn_…`), `<BOT_ID>` (`bot_…`), `<CONVERSATION_ID>` (`cv_…`), `<SESSION_ID>` (`vs_…`), `<VOICE_ID>` (`vp_…`), `<AUDIO_ID>` (`vca_…`), `<NUMBER_ID>` (`pn_…`), phone numbers as `+91XXXXXXXXXX`.

---

## Table of contents

1. [Provider catalog](#provider-catalog)
   - [List providers by capability](#list-providers-by-capability)
   - [Voice-runtime provider catalog (studio)](#voice-runtime-provider-catalog-studio)
   - [List models of a provider](#list-models-of-a-provider)
   - [List languages of a provider model](#list-languages-of-a-provider-model)
   - [List TTS voices of a provider](#list-tts-voices-of-a-provider)
   - [Generate a TTS preview](#generate-a-tts-preview)
   - [Test a provider connection](#test-a-provider-connection)
   - [Validate a bot voice configuration](#validate-a-bot-voice-configuration)
   - [Sync ElevenLabs account voices](#sync-elevenlabs-account-voices)
2. [Platform models & voice catalog](#platform-models--voice-catalog)
   - [List approved models](#list-approved-models)
   - [Update approved-model status](#update-approved-model-status)
   - [List voice profiles](#list-voice-profiles)
3. [Voice cloning (ElevenLabs IVC)](#voice-cloning-elevenlabs-ivc)
   - [Voice-clone configuration & capability](#voice-clone-configuration--capability)
   - [List voice clones](#list-voice-clones)
   - [Create a voice clone](#create-a-voice-clone)
   - [Get a voice clone](#get-a-voice-clone)
   - [Update voice-clone metadata](#update-voice-clone-metadata)
   - [Set voice-clone status](#set-voice-clone-status)
   - [Delete a voice clone](#delete-a-voice-clone)
   - [Stream a clone's source-audio sample](#stream-a-clones-source-audio-sample)
4. [Voice sessions](#voice-sessions)
   - [Create a voice session](#create-a-voice-session)
5. [Conversations](#conversations)
   - [List conversations](#list-conversations)
   - [Create a conversation](#create-a-conversation)
   - [Get a conversation (detail + transcript + AI summary)](#get-a-conversation-detail--transcript--ai-summary)
   - [Update a conversation (review fields)](#update-a-conversation-review-fields)
   - [Stream the call recording](#stream-the-call-recording)
   - [Export the transcript (CSV/XLSX)](#export-the-transcript-csvxlsx)
6. [Telephony](#telephony)
   - [Inbound-call webhook](#inbound-call-webhook)
7. [Phone numbers & SIP trunks](#phone-numbers--sip-trunks)
   - [List phone numbers](#list-phone-numbers)
   - [Create a phone number](#create-a-phone-number)
   - [Update a phone number](#update-a-phone-number)
   - [Activate a phone number](#activate-a-phone-number)
   - [Deactivate a phone number](#deactivate-a-phone-number)
   - [List SIP trunks](#list-sip-trunks)
8. [Inconsistencies & gotchas](#inconsistencies--gotchas)

---

## Provider catalog

Everything under `/providers` is served from the database catalog (`provider_defs`, `provider_models`, `voice_profiles`, `supported_languages`) — never from hardcoded lists. Only active rows are returned. API keys are resolved server-side from `env:` secret references and never appear in responses or errors.

### List providers by capability

`GET /api/v1/providers/catalog`

Providers grouped by capability, for configuration UIs. Auth: JWT bearer. Permission: role `super_admin | tenant_admin | tenant_user` (`require_tenant_member`); no permission code.

Query params:

| Param | Type | Required | Description |
|---|---|---|---|
| `capability` | string | no | One of `stt`, `tts`, `llm`, `embedding`. Restricts the result to that capability. **Any other value (or omission) silently returns all four capabilities** — an unknown capability is not rejected here. |

Response `200`:

```json
{
  "success": true,
  "data": {
    "stt":  [ { "code": "sarvam", "name": "Sarvam AI", "capability": "stt", "description": "…", "requiresApiKey": true, "hasCredentials": true, "supportsCloning": false } ],
    "tts":  [ { "code": "elevenlabs", "name": "ElevenLabs", "capability": "tts", "description": "…", "requiresApiKey": true, "hasCredentials": true, "supportsCloning": true } ],
    "llm":  [ { "code": "openai", "name": "OpenAI", "capability": "llm", "description": "…", "requiresApiKey": true, "hasCredentials": true, "supportsCloning": false } ],
    "embedding": [ ]
  }
}
```

`supportsCloning` is `true` only for TTS providers with a public voice-cloning API (config-driven, currently ElevenLabs and the dev `mock` provider). `hasCredentials` reflects whether the referenced env secret resolves — the key itself is never returned.

### Voice-runtime provider catalog (studio)

`GET /api/v1/providers/voice-catalog`

Lightweight STT/TTS/LLM catalog for the studio configuration UI: provider codes that are active in `provider_defs` **and** have a registered runtime adapter (the `mock` provider is excluded), plus platform defaults and the supported telephony providers. Auth: JWT bearer. Permission: `require_tenant_member` (no permission code). No parameters.

Response `200`:

```json
{
  "success": true,
  "data": {
    "providers": { "stt": ["sarvam", "deepgram"], "tts": ["sarvam", "elevenlabs"], "llm": ["openai"] },
    "defaults": {
      "stt": { "provider": "sarvam", "model": "saaras:v3" },
      "tts": { "provider": "sarvam", "model": "bulbul:v3", "voice": "shubh" },
      "llm": { "provider": "openai", "model": "gpt-4o-mini" }
    },
    "telephonyProviders": ["freeswitch", "twilio", "telnyx", "plivo", "exotel", "vaani"]
  }
}
```

Note: despite the similar name this is a **different** endpoint (and shape) from `GET /providers/catalog` above; it lives in `backend/routers/telephony.py` and returns codes only, not full provider objects.

### List models of a provider

`GET /api/v1/providers/{capability}/{code}/models`

Active models of one provider. Auth: JWT bearer. Permission: `require_tenant_member`.

Path params:

| Param | Type | Description |
|---|---|---|
| `capability` | string | `stt` \| `tts` \| `llm` \| `embedding`. Unknown value → `422 "Unknown capability."` |
| `code` | string | Provider code, e.g. `sarvam`, `elevenlabs`, `openai`, `deepgram`. Unknown → `404`. |

Response `200`:

```json
{
  "success": true,
  "data": [
    {
      "code": "bulbul:v3",
      "displayName": "Bulbul v3 (streaming)",
      "description": "…",
      "provider": "sarvam",
      "capability": "tts",
      "languages": ["hi-IN", "en-IN"],
      "codecs": ["linear16"],
      "sampleRates": [8000, 16000, 22050, 24000],
      "streaming": true,
      "paramsSchema": { },
      "isDefault": true
    }
  ]
}
```

Errors: `422` unknown capability, `404` unknown provider.

### List languages of a provider model

`GET /api/v1/providers/{capability}/{code}/models/{model}/languages`

Platform languages supported by one model. Auth: JWT bearer. Permission: `require_tenant_member`.

Path params: `capability` (`stt|tts|llm|embedding`, else `422`), `code` (provider code), `model` (model code; unknown pair → `404 "Provider model not found."`).

Response `200`:

```json
{
  "success": true,
  "data": {
    "languages": [ { "code": "hi-IN", "name": "Hindi", "nativeName": "हिन्दी" } ],
    "supportsAutoDetect": true,
    "languageAgnostic": false
  }
}
```

`supportsAutoDetect` is `true` when the model's language list contains the sentinel `"unknown"`. `languageAgnostic` is `true` when the model declares no language list at all (works for any language).

### List TTS voices of a provider

`GET /api/v1/providers/tts/{code}/voices`

Active voices of one TTS provider, tenant-scoped: platform voices (`tenant_id` NULL) plus the caller's own cloned voices. Super admins see all tenants' voices. Auth: JWT bearer. Permission: `require_tenant_member`.

Path params: `code` — TTS provider code (unknown → `404`).

Query params (all optional, all default `null` = no filter):

| Param | Type | Description |
|---|---|---|
| `model` | string | Keep only voices whose `modelCodes` contains this model (voices with an empty `modelCodes` list always pass). |
| `language` | string | Keep only voices whose `languages` contains this code (empty `languages` = language-agnostic, always passes). |
| `gender` | string | Exact match on the voice's gender (`male`/`female`/`neutral`). |

Response `200`:

```json
{
  "success": true,
  "data": [
    {
      "id": "vp_XXXXXXXXXXXX",
      "name": "Shubh",
      "gender": "male",
      "provider": "sarvam",
      "providerVoiceId": "shubh",
      "languages": ["hi-IN", "en-IN"],
      "modelCodes": ["bulbul:v3"],
      "locale": "en-IN",
      "premium": false,
      "isDefault": true,
      "status": "active",
      "providerSettings": { },
      "sampleText": "Hello! …",
      "source": "platform"
    }
  ]
}
```

Note: this serializer is a **subset** of the one used by `GET /voices` (no `tenantId`, `accent`, `styles`, `usageCount`, …).

### Generate a TTS preview

`POST /api/v1/providers/tts-preview`

Synthesize a short sample with a real provider call and return it as base64 WAV. Uses the same delivery-parameter mapping as live calls, so a preview sounds like the runtime. Auth: JWT bearer. Permission: `manage_voices` **or** `bots.manage`. Audit-logged; for non-mock providers with a tenant user, the characters are **billed** as a `tts` usage event (`usage_metadata.kind = "tts_preview"`).

Request:

```json
{
  "provider": "sarvam",
  "model": "bulbul:v3",
  "voice": "shubh",
  "language": "hi-IN",
  "text": "नमस्ते, मैं आपकी कैसे मदद कर सकती हूँ?",
  "params": { },
  "speed": 1.0,
  "pauseMs": 250,
  "energy": 60
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `provider` | string | yes | TTS provider code. Unknown → `404`. Preview is implemented for `sarvam`, `elevenlabs` and `mock` only; anything else → `422 "Preview is not supported for provider '…'."`. |
| `model` | string | yes | Must belong to the provider (else `422`). Non-streaming ElevenLabs models (Eleven v3) are synthesized over REST instead of WebSocket. |
| `voice` | string | yes | Catalog voice id (`vp_…`), voice name, or raw provider wire id. A value that resolves to **another tenant's** cloned voice returns `404` without confirming existence. |
| `language` | string | yes | Language code passed to the provider. |
| `text` | string | yes | 1–500 characters. |
| `params` | object | no (default `{}`) | Raw provider params; merged through the shared delivery mapping. |
| `speed` | number | no | 0.5–2.0. Canonical delivery speed (overrides legacy pace/speed params). |
| `pauseMs` | int | no | 0–5000. Inter-sentence pause; when > 0 the text is split into sentences and deterministic silence is inserted between segments (never before the first / after the last). Accepted as `pauseMs` or `pause_ms`. |
| `energy` | int | no | 0–100. Mapped only onto controls the selected model documents. |

Response `200`:

```json
{
  "success": true,
  "data": {
    "audioBase64": "UklGRi…",
    "mimeType": "audio/wav",
    "sampleRate": 24000,
    "ttfaMs": 412.7,
    "totalMs": 1873.4,
    "provider": "sarvam",
    "voice": "shubh"
  }
}
```

Sample rate is 24000 when the model supports it, otherwise 16000. Errors: `404` unknown provider, `422` model/provider mismatch, missing API key, or unsupported provider; `502` provider failure (`"<Provider> preview failed: <category>."`) or empty audio (`"The provider returned no audio."`). Provider timeout is 15 s.

### Test a provider connection

`POST /api/v1/providers/test`

Real connectivity/credential check against the configured provider (no fake success). Auth: JWT bearer. Permission: `manage_voices` **or** `bots.manage`. Audit-logged.

Request:

```json
{ "capability": "tts", "provider": "sarvam", "model": "bulbul:v3", "voice": "shubh", "language": "hi-IN" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `capability` | string | yes | `stt` \| `tts` \| `llm` \| `embedding` (else `422 "Unknown capability."`). |
| `provider` | string | yes | Provider code (unknown → `404`). |
| `model` | string | no | Validated against the catalog (`422` if it belongs to another provider). Current platform defaults: Sarvam STT `saaras:v3`, Sarvam TTS `bulbul:v3`. |
| `voice` | string | no | ElevenLabs only: verifies the voice exists on the connected account (catalog ids are mapped to wire ids first). |
| `language` | string | no | Accepted but not used by any current test. |

What is tested (8 s timeout): Sarvam STT/TTS — WebSocket handshake; ElevenLabs — `GET /v2/voices` (optionally filtered to the given voice); Deepgram STT — token introspection (`/v1/auth/token`, deliberately not a billable Flux handshake); OpenAI — `GET /v1/models[/{model}]`. `mock` short-circuits to success. Other providers return `ok:false, error:"unsupported"`.

Response `200` (always 200 for a completed test — failures are in the body):

```json
{ "success": true, "data": { "ok": true, "latencyMs": 254.3 } }
```

Failure shapes: `{ "ok": false, "error": "credentials_missing" | "auth" | "voice_unavailable" | "invalid_model" | "timeout" | "upstream" | "connection" | "unsupported", "message": "…" }`. HTTP errors: `404` unknown provider, `422` unknown capability / wrong model.

### Validate a bot voice configuration

`POST /api/v1/providers/validate-config`

Validate a complete (camelCase) voice-settings payload against the DB catalog before saving. This is the server-side enforcement point; frontend field-hiding is advisory only. Auth: JWT bearer. Permission: `require_tenant_member`; the bot must belong to the caller's tenant (super admins exempt) — otherwise `404`.

Request:

```json
{
  "botId": "<BOT_ID>",
  "config": {
    "sttProvider": "sarvam", "sttModel": "saaras:v3", "sttLanguage": "hi-IN", "sttSettings": { },
    "llmProvider": "openai", "llmModel": "gpt-4o-mini", "llmSettings": { },
    "ttsProvider": "sarvam", "ttsModel": "bulbul:v3", "ttsVoice": "shubh", "ttsSettings": { },
    "languageVoiceMap": { }, "fallbackProvider": null, "fallbackModel": null, "fallbackVoice": null,
    "audioSettings": { }
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `botId` | string | yes | Bot id (alias; snake_case `bot_id` also accepted). Unknown/deleted → `404`. |
| `config` | object | yes | Any subset of the keys shown above; unknown keys are ignored. Voice lookups are scoped to the bot's tenant (platform voices + own clones). |

Response `200`:

```json
{ "success": true, "data": { "valid": true, "errors": [], "warnings": ["…"] } }
```

`errors`/`warnings` are human-readable strings; `valid` is `errors == []`.

### Sync ElevenLabs account voices

`POST /api/v1/providers/elevenlabs/sync-voices`

Reconcile catalog voices with the connected ElevenLabs account: voices missing from the account are marked `unavailable`, previously unavailable ones found again are restored to `active`; account voices not in the catalog are reported. Never deletes, never renames. Auth: JWT bearer. Permission: `manage_voices` **or** `manage_master_data`. No request body. Audit-logged.

Response `200`:

```json
{
  "success": true,
  "data": {
    "accountVoices": 42,
    "markedUnavailable": ["Old Voice"],
    "restored": [],
    "notInCatalog": [ { "voiceId": "eleven_voice_id", "name": "New Account Voice" } ]
  }
}
```

Errors: `404` ElevenLabs provider not in catalog, `422` no API key configured, `502` ElevenLabs rejected the key.

---

## Platform models & voice catalog

### List approved models

`GET /api/v1/models`

AI-governance list of approved/testing/deprecated models. Auth: JWT bearer. Permission: role `super_admin` only. No parameters.

Response `200`:

```json
{
  "success": true,
  "data": [
    { "id": "mdl_XXXXXXXXXXXX", "name": "gpt-4o-mini", "provider": "openai", "purpose": "dialogue",
      "status": "approved", "tenantsUsing": 4, "costPer1k": 0.00015, "latencyP50": 350 }
  ]
}
```

### Update approved-model status

`PATCH /api/v1/models/{model_id}`

Change a model's governance status. Auth: JWT bearer. Permission: role `super_admin`. Audit-logged.

Path params: `model_id` — approved-model id (unknown/deleted → `404`).

Request:

```json
{ "status": "approved" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `status` | string | yes | `approved` \| `testing` \| `deprecated` (pattern-validated, else `422`). |

Response `200`: the updated model in the same shape as the list.

### List voice profiles

`GET /api/v1/voices`

Full platform voice catalog (tenant-facing, read-only; management lives in the Super Admin master-data API). Tenant isolation: platform voices (`tenantId` null) are shared; cloned voices are visible only to the owning tenant; super admins see everything. Auth: JWT bearer. Permission: any authenticated user (`get_current_user`).

Query params (all optional):

| Param | Type | Default | Description |
|---|---|---|---|
| `provider` | string | — | Exact provider code. |
| `language` | string | — | Voice's `languages` list must contain this code (filtered in Python over the JSON column). |
| `locale` | string | — | Exact locale, e.g. `en-IN`. |
| `gender` | string | — | Exact gender. |
| `source` | string | — | `platform` or `cloned`. |
| `search` | string (≤100) | — | `LIKE` match on name or accent. |
| `includeInactive` | boolean | `false` | When `false`, only `status == "active"` voices are returned. |

Response `200` (full `serialize_voice` shape — richer than `/providers/tts/{code}/voices`):

```json
{
  "success": true,
  "data": [
    {
      "id": "vp_XXXXXXXXXXXX", "tenantId": null, "source": "platform", "cloneMetadata": { },
      "name": "Shubh", "gender": "male", "languages": ["hi-IN", "en-IN"], "locale": "en-IN",
      "accent": "", "styles": [], "description": "", "latencyMs": 300, "premium": false,
      "sample": "Hello! …", "provider": "sarvam", "providerVoiceId": "shubh",
      "speakingRate": 1.0, "pitch": 0, "isDefault": true, "status": "active", "sortOrder": 10,
      "modelCodes": ["bulbul:v3"], "providerSettings": { "pace": 1.0 }, "usageCount": 0,
      "updatedAt": "2026-08-01T10:00:00Z"
    }
  ]
}
```

---

## Voice cloning (ElevenLabs IVC)

Cloned voices are tenant-owned `voice_profiles` rows (`tenant_id` set, `source: "cloned"`). All provider calls happen server-side; API keys never reach the client. Source audio samples are persisted under `VOICE_CLONE_AUDIO_DIR` with one `voice_clone_audio` row each, replayable via the audio endpoint. Once created, a clone is an ordinary catalog voice (bot selection, validation, preview and runtime TTS all resolve it through the normal paths; character-based TTS metering applies unchanged). Clone **creation** is not billed (ElevenLabs IVC is plan-gated by voice slots, not per-clone charges) — it is audit-logged only.

**The flow:** `GET /voice-clones/config` (which providers can clone + upload constraints) → `POST /voice-clones` multipart with 1–10 audio samples → ElevenLabs `POST /v1/voices/add` creates the provider voice → local `voice_profiles` + `voice_clone_audio` rows are committed together. If persistence fails after the provider call, the provider voice is deleted again (no orphan slots). Clones are created language-agnostic (`languages: []` — ElevenLabs clones are multilingual) and mapped to **all** of the provider's TTS models.

### Voice-clone configuration & capability

`GET /api/v1/voice-clones/config`

Cloning capability per TTS provider plus upload constraints (single source of truth for the frontend). Auth: JWT bearer. Permission: `require_tenant_member`. No parameters.

Response `200`:

```json
{
  "success": true,
  "data": {
    "providers": [
      {
        "code": "elevenlabs", "name": "ElevenLabs", "supportsCloning": true, "hasCredentials": true,
        "cloneParams": [
          { "name": "description", "type": "string", "label": "Description", "help": "…", "maxLength": 500, "optional": true },
          { "name": "removeBackgroundNoise", "type": "boolean", "label": "Remove background noise", "help": "…", "default": false, "optional": true }
        ],
        "reason": null
      },
      { "code": "sarvam", "name": "Sarvam AI", "supportsCloning": false, "cloneParams": [],
        "hasCredentials": true,
        "reason": "Sarvam offers voice cloning only inside Sarvam Studio (in-browser recording, beta) — its public API has no voice-cloning endpoint." }
    ],
    "allowedExtensions": ["aac", "flac", "m4a", "mp3", "ogg", "opus", "wav", "webm"],
    "accept": ".aac,.flac,.m4a,.mp3,.ogg,.opus,.wav,.webm",
    "maxFiles": 10,
    "maxFileMb": 10,
    "maxTotalMb": 30,
    "recording": { "minSeconds": 5, "recommendedMinSeconds": 30, "recommendedMaxSeconds": 40, "maxSeconds": 300 }
  }
}
```

### List voice clones

`GET /api/v1/voice-clones`

The tenant's cloned voices, newest first. Auth: JWT bearer. Permission: `require_tenant_member`.

Query params:

| Param | Type | Default | Description |
|---|---|---|---|
| `tenantId` | string | — | Target tenant. Required for super admins; other roles may only pass their own (else 403). |
| `includeInactive` | boolean | `true` | When `false`, only `status == "active"` clones. |

Response `200`: array of clone objects — full `serialize_voice` shape (see `GET /voices`) **plus**:

```json
{
  "usageCount": 2,
  "sourceAudio": [
    {
      "id": "vca_XXXXXXXXXXXX", "voiceId": "vp_XXXXXXXXXXXX", "originalFilename": "sample1.wav",
      "mimeType": "audio/wav", "sizeBytes": 480044, "durationSec": 32.4,
      "sourceType": "live_recording", "provider": "elevenlabs", "providerVoiceId": "eleven_voice_id",
      "status": "stored", "createdBy": "usr_XXXXXXXXXXXX", "createdAt": "2026-08-01T10:00:00Z",
      "url": "/api/v1/voice-clones/vp_XXXXXXXXXXXX/audio/vca_XXXXXXXXXXXX"
    }
  ]
}
```

`usageCount` = number of bot configurations referencing the voice (bot FK, engine columns, fallback voice, or language-voice map). `sourceAudio` is empty for clones created before source retention (UI shows "unavailable", never an error).

### Create a voice clone

`POST /api/v1/voice-clones` — **multipart/form-data**

Create an ElevenLabs Instant Voice Clone from uploaded samples or in-browser recordings. Auth: JWT bearer. Permission: `manage_voices`. Audit-logged (`voice.clone.create`).

Form fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `provider` | string | yes | TTS provider code. Must exist (`422` unknown), support cloning (`422` with a provider-specific reason, e.g. Sarvam's Studio-only note), and have an implemented cloning backend (`elevenlabs` or dev `mock`; `422` otherwise). |
| `name` | string | yes | Trimmed; 1–100 chars. Must be unique (case-insensitive) among the tenant's non-deleted clones for the provider (`422`). |
| `description` | string | no | ≤500 chars; also stored on the ElevenLabs voice. |
| `gender` | string | no | `male` \| `female` \| `neutral` (default `neutral`; anything else `422`). |
| `removeBackgroundNoise` | boolean | no (default `false`) | Runs ElevenLabs audio isolation on the samples before cloning. |
| `tenantId` | string | no | Target tenant (super admins; required for them). Tenant users may only pass their own. |
| `samplesMeta` | string (JSON) | no | JSON array aligned with `files` order: `[{"sourceType": "live_recording"|"file_upload", "durationSec": 31.2}]`. Advisory provenance only — leniently parsed, defaults applied for anything malformed; server-probed durations override it. |
| `files` | file[] | yes | 1–10 audio samples. Allowed extensions: `mp3, wav, m4a, flac, ogg, opus, webm, aac` (validated by extension **and** magic bytes where the container has a signature). ≤10 MB per file, ≤30 MB combined, non-empty. Probed duration (ffprobe / stdlib wave) must be ≥5 s (0.5 s tolerance) and ≤1800 s; 30–40 s per sample recommended. MediaRecorder webm/ogg blobs without duration headers are remuxed in place. |

```bash
curl -X POST http://localhost:9001/api/v1/voice-clones \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -F provider=elevenlabs \
  -F "name=Agent Priya" \
  -F "description=Warm collections voice" \
  -F gender=female \
  -F removeBackgroundNoise=false \
  -F 'samplesMeta=[{"sourceType":"live_recording","durationSec":33.5}]' \
  -F "files=@sample1.wav;type=audio/wav"
```

Behavior: samples are persisted (and duration-validated from the stored bytes) **before** the ElevenLabs call; a provider rejection removes the stored files again. On persistence failure after a successful provider call, the provider voice is deleted to avoid orphaned voice slots (a failed cleanup is logged as an orphan).

Response `201`: the clone object (same shape as the list, `sourceAudio` populated). `cloneMetadata` records `{"kind": "instant", "requiresVerification": false, "removeBackgroundNoise": false, "samples": [{"fileName", "sizeBytes", "sourceType", "durationSec", "audioId"}]}`.

Key errors: `422` validation (name/gender/description/files/provider/duplicate/missing API key); mapped ElevenLabs failures — `422` for scoped keys missing `create_instant_voice_clone` permission, `voice_limit_reached`, plan without IVC, or provider input rejections; `502` bad key / unreachable / other provider failures; `500` storage or persistence failure.

### Get a voice clone

`GET /api/v1/voice-clones/{voice_id}`

One clone with its source audio. Auth: JWT bearer. Permission: `require_tenant_member`. Visibility: owning tenant or super admin; any other tenant's clone (and any non-clone voice id) → `404`, never `403`.

Path params: `voice_id` — voice-profile id (`vp_…`) with `source == "cloned"`.

Response `200`: the clone object (see list).

### Update voice-clone metadata

`PATCH /api/v1/voice-clones/{voice_id}`

Local metadata only — the provider voice is never renamed. Auth: JWT bearer. Permission: `manage_voices`. Audit-logged; invalidates cached bot runtime configs.

Request (all fields optional; only provided fields change):

```json
{ "name": "Agent Priya v2", "description": "…", "gender": "female", "locale": "en-IN", "sampleText": "Hello, this is Priya." }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | no | 1–100 chars, trimmed, non-empty; duplicate name among the tenant's clones for the provider → `422`. |
| `description` | string | no | ≤500 chars; empty string clears it. |
| `gender` | string | no | `male` \| `female` \| `neutral` (else `422`). |
| `locale` | string | no | Empty string clears it. |
| `sampleText` | string | no | ≤500 chars; empty string clears it. |

Response `200`: the updated clone object. Errors: `404` not found / foreign tenant, `422` validation.

### Set voice-clone status

`POST /api/v1/voice-clones/{voice_id}/status`

Activate/deactivate/archive a clone. A deactivated voice stops resolving in cached runtime configs (cache invalidated). Auth: JWT bearer. Permission: `manage_voices`. Audit-logged.

Request:

```json
{ "status": "inactive" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `status` | string | yes | `active` \| `inactive` \| `archived` (else `422`). |

Response `200`: the updated clone object.

### Delete a voice clone

`DELETE /api/v1/voice-clones/{voice_id}`

Deletes the **provider** voice first (the local row is the only pointer to it), then soft-deletes the local voice + audio rows and removes the stored files (best-effort, after commit). Auth: JWT bearer. Permission: `manage_voices`. Audit-logged.

Response `200`:

```json
{ "success": true, "data": { "deleted": true, "providerDeleted": true } }
```

Errors: `404` not found / foreign tenant; `409` the clone is referenced by bot configuration(s) (`"'…' is used by N bot configuration(s). Deactivate or archive it instead…"`); `422` no cloning integration or no API key for the provider (archive instead); `502` the provider did not confirm deletion — the local record is kept.

### Stream a clone's source-audio sample

`GET /api/v1/voice-clones/{voice_id}/audio/{audio_id}`

Replay exactly what a clone was built from. Auth: JWT bearer. Permission: `require_tenant_member`; same visibility as the clone (owning tenant / super admin), `404` otherwise. Storage paths are resolved server-side only.

Path params: `voice_id`, `audio_id` (must belong to that voice and tenant).

Query params:

| Param | Type | Default | Description |
|---|---|---|---|
| `download` | boolean | `false` | When `true`, adds `Content-Disposition: attachment` with a server-generated filename `voice-source-<AUDIO_ID>.<ext>` (the original filename never reaches headers). |

Response `200`: the raw audio bytes (`Content-Type` from the stored mime, fallback `application/octet-stream`). Errors: `404` clone/sample/file not found.

---

## Voice sessions

### Create a voice session

`POST /api/v1/voice-sessions`

Issue a trusted session for the realtime **voice worker** (a separate process). The API authenticates the user, verifies bot ownership, then writes the tenant/bot mapping into Redis; the worker accepts the WebSocket using only that mapping — clients can never supply tenant identity directly. Auth: JWT bearer. Permission: any authenticated user (`get_current_user`); the bot must be in the caller's tenant (super admins exempt), else `404`. Audit-logged (`voice.session.create`).

Request:

```json
{
  "botId": "<BOT_ID>",
  "channel": "browser",
  "variables": { "customer_name": "Asha", "due_amount": "4500" },
  "customerContextId": null
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `botId` | string | yes | Bot id (alias; `bot_id` accepted). Unknown/deleted → `404`. |
| `channel` | string | no | `browser` (default) \| `phone` \| `sip` (pattern-validated). |
| `variables` | object<string,string> | no | Per-call test variables (browser test console) — same bounds as signed dialer variables: at most 20 entries; keys must match `^[A-Za-z0-9_.-]{1,40}$` (invalid key → `422`); values coerced to strings and truncated to 200 chars. |
| `customerContextId` | string | no | ≤40 chars. Pins the call to one `customer_contexts` row; must belong to the same tenant **and** bot, else `404 "Customer context not found."`. |

Response `201`:

```json
{
  "success": true,
  "data": {
    "sessionId": "vs_XXXXXXXXXXXX",
    "botId": "<BOT_ID>",
    "channel": "browser",
    "wsPath": "/ws/voice/vs_XXXXXXXXXXXX",
    "workerPort": 9002,
    "expiresInSeconds": 900
  }
}
```

The client connects the media WebSocket to the voice worker at `ws://<host>:<workerPort><wsPath>` (worker port default 9002; session TTL default 900 s, both from settings). The response deliberately contains no tenant id and no full URL — the worker resolves tenant/bot from Redis by `sessionId`.

---

## Conversations

Conversation **metadata** lives in MySQL (`conversation_sessions`); **transcripts** live in MongoDB (`conversation_transcripts`, one document per session — turn shapes vary and can grow large). Two writers exist: this API (documents keyed by the conversation row id, turns already in the UI shape) and the voice runtime (documents keyed by a `vs_*` session id, runtime turn shape, stamped with `control_plane_id`). Reads resolve either generation and normalize turns to the UI shape.

### List conversations

`GET /api/v1/conversations`

Paginated tenant conversation list (metadata only — no transcripts, no cost breakdown). Auth: JWT bearer. Permission: any authenticated user; tenant-scoped via `resolve_tenant_id`.

Query params (all optional unless noted):

| Param | Type | Default | Description |
|---|---|---|---|
| `tenantId` | string | — | **Required for super admins** (400 otherwise); other roles may only pass their own tenant (403 otherwise). |
| `botId` | string | — | Filter by bot. |
| `channel` | string | — | Filter by channel (stored values: `voice`, `whatsapp`, `web`, `mobile`; not enum-validated on the filter). |
| `sentiment` | string | — | Filter by sentiment (`positive`/`neutral`/`negative`; not enum-validated on the filter). |
| `contained` | boolean | — | Filter on containment (no escalation). |
| `flagged` | boolean | — | Filter on review flag. |
| `page` | int ≥1 | `1` | Page number. |
| `pageSize` | int 1–200 | `50` | Page size. |
| `search` | string ≤200 | — | `LIKE` match on masked caller, escalation reason, or conversation id. |
| `sortBy` | string ≤50 | — | **Accepted but ignored** — the list is always sorted by `startedAt` descending. |
| `sortDir` | string | `desc` | `asc` \| `desc` — **accepted but ignored** (see above). |

Response `200` (paginated envelope):

```json
{
  "success": true,
  "data": [
    {
      "id": "<CONVERSATION_ID>", "botId": "<BOT_ID>", "bot": "eDAS Collection Bot",
      "channel": "voice", "caller": "+91XXXXXXXXXX", "startedAt": "2026-08-06T09:15:00Z",
      "durationSec": 184, "sentiment": "neutral", "intents": ["payment_promise"],
      "contained": true, "escalationReason": null, "csat": null,
      "costUsd": 0.0132, "cost": null, "language": "hi-IN", "qaScore": null, "flagged": false,
      "disposition": "promise_to_pay", "promptId": "pr_XXXXXXXXXXXX", "promptVersion": "v3",
      "transcript": [], "recording": null, "summary": null
    }
  ],
  "meta": { "page": 1, "pageSize": 50, "total": 132, "totalPages": 3 }
}
```

On the list, `transcript` is always `[]` and `cost`, `recording`, `summary` are always `null` (populated on the detail view only). `costUsd` is the cached authoritative total in USD. `bot` falls back to `"—"` when the bot row is gone.

### Create a conversation

`POST /api/v1/conversations`

Insert a conversation record with an optional transcript (used by seeds/imports; live calls are written by the voice runtime). Metadata goes to MySQL, the transcript is upserted into Mongo keyed by the new conversation id. Auth: JWT bearer. Permission: any authenticated user; the bot must be in the caller's tenant (super admin exempt), else `404`.

Request:

```json
{
  "botId": "<BOT_ID>",
  "channel": "voice",
  "caller": "+91XXXXXXXXXX",
  "startedAt": "2026-08-06T09:15:00Z",
  "durationSec": 184,
  "sentiment": "neutral",
  "intents": ["payment_promise"],
  "contained": true,
  "escalationReason": null,
  "csat": 4,
  "costUsd": 0.0132,
  "language": "hi-IN",
  "qaScore": 92,
  "transcript": [
    { "turn": 1, "speaker": "bot", "text": "Namaste…", "intent": null, "confidence": null,
      "chunksUsed": null, "apiCalls": null, "promptVersion": "v3", "latencyMs": 850, "costUsd": null }
  ]
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `botId` | string | yes | Bot id. Determines the tenant. |
| `channel` | string | no | `voice` (default) \| `whatsapp` \| `web` \| `mobile`. |
| `caller` | string | no | Masked caller label, ≤50 chars, default `"•••"`. |
| `startedAt` | datetime | no | ISO 8601; defaults to now (UTC). |
| `durationSec` | int ≥0 | no | Default `0`. |
| `sentiment` | string | no | `positive` \| `neutral` (default) \| `negative`. |
| `intents` | string[] | no | Default `[]`. |
| `contained` | boolean | no | Default `true`. |
| `escalationReason` | string | no | Default `null`. |
| `csat` | int 1–5 | no | Default `null`. |
| `costUsd` | number ≥0 | no | Default `0`. |
| `language` | string ≤15 | no | Default `"en-US"`. |
| `qaScore` | int 0–100 | no | Default `null`. |
| `transcript` | TurnPayload[] | no | Default `[]`. Per turn: `turn` (int, required), `speaker` (`user`\|`bot`, required), `text` (≤8000, required), optional `intent`, `confidence` (0–1), `chunksUsed` (string[]), `apiCalls` (object[]), `promptVersion`, `latencyMs`, `costUsd`. |

Response `201`: the conversation object (detail shape, `transcript` echoed back, `recording`/`cost`/`summary` null, `flagged` false).

### Get a conversation (detail + transcript + AI summary)

`GET /api/v1/conversations/{conversation_id}`

Full detail: metadata, normalized transcript, recording descriptor, auditable cost breakdown and post-call AI summary. Auth: JWT bearer. Permission: any authenticated user; foreign tenant → `404`.

Path params: `conversation_id`. Query params:

| Param | Type | Default | Description |
|---|---|---|---|
| `currency` | string | base currency (USD) | Display currency for the cost breakdown; falls back to USD when no exchange rate is configured for it. |

Side effect: when the MySQL row predates the `session_id` column, the id is backfilled from the transcript document and **persisted** during this GET.

Response `200` — the list shape plus populated detail fields:

```json
{
  "success": true,
  "data": {
    "id": "<CONVERSATION_ID>", "botId": "<BOT_ID>", "bot": "eDAS Collection Bot",
    "channel": "voice", "caller": "+91XXXXXXXXXX", "startedAt": "2026-08-06T09:15:00Z",
    "durationSec": 184, "sentiment": "neutral", "intents": ["payment_promise"],
    "contained": true, "escalationReason": null, "csat": null, "costUsd": 0.0132,
    "language": "hi-IN", "qaScore": null, "flagged": false,
    "disposition": "promise_to_pay", "promptId": "pr_XXXXXXXXXXXX", "promptVersion": "v3",

    "transcript": [
      { "turn": 1, "speaker": "bot", "text": "Namaste…", "at": "2026-08-06T09:15:02.120Z",
        "route": "scripted", "chunksUsed": ["doc_ab12 · p3"], "latencyMs": 830 }
    ],

    "recording": {
      "url": "/api/v1/conversations/<CONVERSATION_ID>/recording",
      "mimeType": "audio/wav", "durationSec": 184.2, "sizeBytes": 2949644
    },

    "cost": {
      "sessionId": "vs_XXXXXXXXXXXX", "baseCurrency": "USD",
      "totalUsd": "0.013200", "displayCurrency": "INR", "displayTotal": "1.15", "displayRate": "87.10",
      "byCapability": { "stt": { "label": "Speech-to-text", "costUsd": "0.004000" } },
      "lines": [
        { "capability": "tts", "capabilityLabel": "Text-to-speech", "provider": "sarvam",
          "model": "bulbul:v3", "voice": "shubh", "component": "characters",
          "componentLabel": "Characters", "quantity": "812", "unit": "chars",
          "unitPrice": "0.000010", "rateCurrency": "INR", "fxRate": "0.01148",
          "costUsd": "0.008120", "priced": true, "note": null }
      ],
      "unpriced": [],
      "eventCount": 6,
      "highCost": false,
      "storedTotalUsd": "0.013200",
      "reconciled": true
    },

    "summary": {
      "status": "completed",
      "callOutcome": "promise_to_pay",
      "summary": "Customer confirmed identity and promised to pay…",
      "customerIntent": "will_pay_later",
      "customerSentiment": "cooperative",
      "customerCommitments": [
        { "type": "payment", "description": "Pay outstanding EMI", "amount": 4500,
          "currency": "INR", "dueDate": "2026-08-10", "status": "promised" }
      ],
      "objections": [], "importantFacts": [], "resolvedItems": [], "unresolvedItems": [],
      "missingSlots": [],
      "nextBestAction": { "action": "schedule_reminder_call", "reason": "…", "priority": "high", "recommendedAt": "2026-08-09T10:00:00Z" },
      "followUpRequired": true,
      "followUpAt": "2026-08-09T10:00:00Z",
      "confidence": 0.86,
      "generatedAt": "2026-08-06T09:20:11Z",
      "error": null
    }
  }
}
```

Transcript mapping (`backend/core/transcripts.py:ui_turns`): turns stored in the UI shape (`speaker` present) pass through with `turn` defaulted to their index+1; runtime-shaped turns (`{role, text, ts, route, kbUsed, kbSources, latencyMs}`) are converted — `role` → `speaker` (`bot` or `user`), `ts` → ISO `at`, `kbSources` → `chunksUsed` labels (`"<documentId> · p<page>"`), and `latencyMs` (number or per-stage dict, where the total or the stage sum is used) → integer `latencyMs`. Turns are sorted by `turn`.

Cost notes: `storedTotalUsd` is the cached list-view total; `reconciled` is `false` when it drifts from the recomputed event total by more than 0.000001 (events recorded after finalize, or a pricing backfill). The breakdown reproduces the **historical** pricing snapshots, not today's rate table.

AI summary (`summary`): present only when post-call analysis ran (tenant opt-in `call_summary_enabled`); `null` otherwise. `error` is populated only when `status == "failed"`.

Errors: `404` unknown conversation or foreign tenant.

### Update a conversation (review fields)

`PATCH /api/v1/conversations/{conversation_id}`

Review workflow: flag/unflag and QA-score a conversation. Auth: JWT bearer. Permission: role `super_admin | tenant_admin` (`require_tenant_admin`); foreign tenant → `404`. Audit-logged.

Request (both fields optional):

```json
{ "flagged": true, "qaScore": 85 }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `flagged` | boolean | no | Review flag. |
| `qaScore` | int | no | 0–100. |

Response `200`: the conversation in the **list** shape (no transcript/recording/cost/summary).

### Stream the call recording

`GET /api/v1/conversations/{conversation_id}/recording`

Stream the call audio file. Same visibility rules as the conversation; the file reference always comes from the transcript document — clients never pass paths (path-traversal-safe resolution under the recordings root). Auth: JWT bearer. Permission: any authenticated user; foreign tenant → `404`.

Query params:

| Param | Type | Default | Description |
|---|---|---|---|
| `download` | boolean | `false` | When `true`, adds an attachment filename `echosphere-call-<CONVERSATION_ID>.wav` (extension from the stored file). |

Response `200`: audio bytes (`Content-Type` from the stored mime, fallback `audio/wav`). Errors: `404` conversation or recording not found (also when the file has been removed from disk).

### Export the transcript (CSV/XLSX)

`GET /api/v1/conversations/{conversation_id}/transcript/export`

Download the normalized transcript as a spreadsheet. Lives in `backend/routers/exports.py`. Auth: JWT bearer. Permission: `conversations.view`. Foreign tenant → `404`. Deliberately usable for conversations of **deleted bots** (the bot name is only a label). Audit-logged (`conversation.transcript.export`).

Query params:

| Param | Type | Default | Description |
|---|---|---|---|
| `format` | string ≤8 | `csv` | `csv` \| `xlsx` (anything else → `422 "Unsupported export format '…'."`). |

Response `200`: file download (`Content-Disposition: attachment; filename="echosphere-transcript-<CONVERSATION_ID>-<YYYY-MM-DD>.csv"`, `X-Content-Type-Options: nosniff`). Columns per turn: `turn`, `speaker`, `text`, `intent`, `confidence`, `chunks_used` (joined with `; `), `api_calls` (`"name (ok, 120ms)"` items joined with `; `), `prompt_version`, `latency_ms`, `cost_usd`.

---

## Telephony

### Inbound-call webhook

`POST /api/v1/telephony/webhook/{provider}`

Answer an inbound call: verify the webhook signature → resolve the dialed number (+ optional `botId`) to a tenant/bot via the trusted routing map → issue a voice session → return the provider's connect payload pointing its media stream at the voice worker. The identical handler is also mounted at the **root path of the telephony gateway** (`python -m voice_runtime.gateway`, one public host:port for webhook + media WebSocket); this `/api/v1` mount is the historical path on the platform API.

**Auth: no JWT.** Two HMAC signature schemes, both keyed by the secret resolved from `TELEPHONY_WEBHOOK_SECRET` (setting `telephony_webhook_secret_reference`, default `env:TELEPHONY_WEBHOOK_SECRET`); `503` when the secret is not configured:

- **Twilio** (`provider=twilio`): Twilio's documented scheme — HMAC-SHA1 over `url + sorted(key+value)` of the POST form, base64, in `X-Twilio-Signature`. The env secret acts as the Twilio auth token.
- **Generic** (all other providers — Exotel/Plivo/Telnyx/Vaani/FreeSWITCH bridges): HMAC-SHA256 hex over `"<timestamp>.<raw body>"` in header `X-Webhook-Signature`, with `X-Webhook-Timestamp` (unix seconds). Timestamps more than **300 s** from server time are rejected.

**Idempotency / replay protection:** each accepted signature is single-use — stored in Redis via `SETNX` with a 600 s TTL (Twilio: signature + `CallSid`); a repeat returns `403 "Webhook replay detected"`. On a Redis outage the check **fails open** (webhook accepted, error logged loudly). All verification failures are `403` (missing headers, invalid timestamp, stale timestamp, bad signature, replay).

Path params:

| Param | Type | Description |
|---|---|---|
| `provider` | string | `freeswitch` \| `twilio` \| `telnyx` \| `plivo` \| `exotel` \| `vaani`. Anything else → `404 "Unsupported telephony provider '…'"`. |

Payload — Twilio sends its standard form (`To`, `From`, `CallSid` are read). Generic providers send JSON (form fallback):

```json
{
  "To": "+91XXXXXXXXXX",
  "From": "+91XXXXXXXXXX",
  "CallSid": "call-123",
  "botId": "<BOT_ID>",
  "variables": { "customer_name": "Asha", "loan_id": "LN-1042" }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `To` / `to` / `CallTo` / `called_number` | string | yes | Dialed number — the tenant/bot routing authority. Missing → `422 "Webhook payload missing the dialed number"`. |
| `From` / `from` / `caller_number` | string | no | Caller number (stored masked on the session). |
| `CallSid` / `callId` / `call_id` | string | no | Provider call id, truncated to 64 chars. |
| `botId` / `bot_id` | string | no | Per-campaign bot selection **within** the tenant that owns the dialed number (the number stays the tenant authority). Must match `^[A-Za-z0-9_-]{1,64}$` → else `422`. |
| `variables` | object | no | Dialer per-call variables. Sanitized even though the sender is HMAC-trusted: max 20 entries, keys `^[A-Za-z0-9_.-]{1,40}$` (invalid keys silently dropped), scalar values stringified and truncated to 200 chars. |

A bot whose voice channel is explicitly deactivated returns a sanitized `403 "This number is not accepting calls."` (bots without any voice ChannelConfig row are implicitly enabled — legacy numbers).

Response `200` — the raw provider connect payload (NOT the standard envelope), pointing at `ws(s)://<TELEPHONY_PUBLIC_WS_BASE>/ws/telephony/{provider}/<SESSION_ID>` (base from the `TELEPHONY_PUBLIC_WS_BASE` setting, else derived from the request URL):

- `twilio` → `application/xml`: `<Response><Connect><Stream url="wss://…"/></Connect></Response>`
- `plivo` → `application/xml`: `<Response><Stream keepCallAlive="true" bidirectional="true" contentType="audio/x-l16;rate=8000">wss://…</Stream></Response>`
- `exotel`, `vaani` → `application/json`: `{"url": "wss://…"}`
- `telnyx` → `application/json`: `{"stream_url": "wss://…", "stream_track": "inbound_track"}`
- `freeswitch` → `application/json`: `{"audio_stream_url": "wss://…", "audio_fork_url": "wss://…?transport=audio_fork"}`

Other errors: `403` signature/replay/deactivated channel, `404` unsupported provider or unrouted number (from `resolve_bot_for_dialer`), `422` missing dialed number / invalid `botId`, `503` secret not configured.

---

## Phone numbers & SIP trunks

All endpoints in this section are Super-Admin only (role `super_admin`; there is no permission-code variant).

### List phone numbers

`GET /api/v1/phone-numbers`

All non-deleted platform numbers, oldest first, with resolved tenant/bot display names. Auth: JWT bearer. Permission: role `super_admin`. No parameters.

Response `200`:

```json
{
  "success": true,
  "data": [
    {
      "id": "<NUMBER_ID>", "number": "+91XXXXXXXXXX", "country": "IN",
      "tenant": "Acme Collections", "bot": "eDAS Collection Bot",
      "provider": "vaani", "status": "assigned", "isActive": true, "monthlyCost": 2.5
    }
  ]
}
```

Note: the serializer returns tenant/bot **names** (`tenant`, `bot`), not their ids — a client cannot read `tenantId`/`botId` back from this API even though it writes them.

### Create a phone number

`POST /api/v1/phone-numbers`

Register a number, optionally assigning it to a tenant/bot. Auth: JWT bearer. Permission: role `super_admin`. Audit-logged.

Request:

```json
{
  "number": "+91XXXXXXXXXX",
  "country": "IN",
  "provider": "vaani",
  "tenantId": "<TENANT_ID>",
  "botId": "<BOT_ID>",
  "status": "available",
  "monthlyCost": 2.5
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `number` | string | yes | 5–30 chars; must normalize to a valid E.164 number (`422 "Enter a valid E.164 number, e.g. +14155550119."`). Duplicate (any formatting of the same digits) → `409`. |
| `country` | string ≤5 | no | Default `"US"`. |
| `provider` | string ≤50 | no | Default `""`. Telephony carrier label. |
| `tenantId` | string | no | Assignment tenant; unknown → `422`. |
| `botId` | string | no | Assignment bot; unknown → `422`; must belong to `tenantId` (or, when `tenantId` is omitted, the bot's own tenant is used) → else `422`. |
| `status` | string | no | `assigned` \| `available` (default) \| `porting` \| `error`. **Ignored when a tenant is assigned** — the row is forced to `assigned`. |
| `monthlyCost` | number ≥0 | no | Default `0`. |

Response `201`: the serialized number (list shape). Errors: `409` duplicate, `422` validation.

### Update a phone number

`PATCH /api/v1/phone-numbers/{number_id}`

Partial update — only provided fields change; `tenantId`/`botId` accept explicit `null` to clear the assignment (clearing the tenant releases the bot too). Auth: JWT bearer. Permission: role `super_admin`. Audit-logged.

Path params: `number_id` (unknown/deleted → `404`).

Request (all fields optional):

```json
{ "number": "+91XXXXXXXXXX", "country": "IN", "provider": "vaani", "tenantId": null, "botId": null, "status": "available", "monthlyCost": 2.5 }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `number` | string 5–30 | no | Re-validated E.164; duplicate → `409`. |
| `country` | string ≤5 | no | |
| `provider` | string ≤50 | no | |
| `tenantId` | string \| null | no | New assignment (validated as on create). |
| `botId` | string \| null | no | New assignment (validated as on create). |
| `status` | string | no | `assigned` \| `available` \| `porting` \| `error`. Marking an unassigned number `assigned` → `422 "An unassigned number cannot be marked assigned."`. |
| `monthlyCost` | number ≥0 | no | |

Guard rails:
- A number **claimed by a bot's voice channel** (a voice `ChannelConfig` whose `phoneNumber` matches) cannot have its number or assignment changed here → `409` directing the admin to the bot's Channels tab (keeps the trusted inbound routing map and channel config in agreement).
- Assigning a tenant to an **inactive** number → `409 "This phone number is inactive and cannot take new assignments. Activate it first."`.
- Changing the assignment recomputes `status` to `assigned`/`available`.

Response `200`: the serialized number. Errors: `404`, `409`, `422` as above.

### Activate a phone number

`POST /api/v1/phone-numbers/{number_id}/activate`

Set `isActive: true` (idempotent — no-op with a `200` if already active; audit entry only on change). Auth: JWT bearer. Permission: role `super_admin`. No body.

Response `200`: the serialized number. Errors: `404`.

### Deactivate a phone number

`POST /api/v1/phone-numbers/{number_id}/deactivate`

Set `isActive: false`. Deactivation blocks **new** assignments only — the current assignment and live routing are deliberately preserved (never silently breaks an existing deployment). Idempotent. Auth: JWT bearer. Permission: role `super_admin`. No body.

Response `200`: the serialized number. Errors: `404`.

### List SIP trunks

`GET /api/v1/sip-trunks`

All non-deleted SIP trunks, by name. Auth: JWT bearer. Permission: role `super_admin`. No parameters.

Response `200`:

```json
{
  "success": true,
  "data": [
    { "id": "st_XXXXXXXXXXXX", "name": "Primary Trunk", "provider": "twilio", "region": "ap-south-1",
      "capacityLines": 100, "activeCalls": 12, "failurePct": 0.4, "status": "healthy" }
  ]
}
```

---

## Inconsistencies & gotchas

Actual behavior found in code that a consumer should know about:

1. **`GET /providers/catalog` does not validate `capability`** — an unknown value silently returns all four capabilities instead of a 422 (unlike the `/providers/{capability}/…` routes, which reject unknown capabilities).
2. **Two "catalog" endpoints with different shapes:** `GET /providers/catalog` (full provider objects, grouped by capability, from `backend/routers/providers.py`) vs `GET /providers/voice-catalog` (bare code lists + defaults + telephony providers, from `backend/routers/telephony.py`).
3. **Two voice serializers:** `GET /providers/tts/{code}/voices` returns a reduced shape; `GET /voices` and the voice-clone endpoints return the full `serialize_voice` shape (adds `tenantId`, `accent`, `styles`, `speakingRate`, `usageCount`, `cloneMetadata`, …).
4. **`sortBy`/`sortDir` on `GET /conversations` are accepted but never applied** — the list is hard-ordered by `startedAt desc`.
5. **`GET /conversations/{id}` has a write side effect:** it backfills and commits `session_id` on legacy rows during the read.
6. **`PATCH /conversations/{id}` returns the list shape** (no transcript/recording/cost/summary), unlike GET on the same path.
7. **Phone-number responses expose tenant/bot names, not ids** (`tenant`/`bot`), while the write side takes `tenantId`/`botId` — round-tripping the assignment requires an out-of-band lookup.
8. **`ProviderTestRequest.language` is accepted but unused** by every implemented connection test.
9. **Webhook replay protection fails open** when Redis is unavailable (logged as an error, webhook still accepted) — deliberate availability trade-off.
10. **Provider test failures return HTTP 200** with `data.ok: false` and an `error` category; only catalog problems (unknown provider/capability/model) use 4xx.
11. **`POST /phone-numbers` ignores the supplied `status`** whenever a tenant is assigned (forced to `assigned`).
12. **TTS previews are billed** (character-based `usage_events` row, `kind: "tts_preview"`) for non-mock providers when the caller belongs to a tenant; platform-admin previews without a tenant are not billed.
