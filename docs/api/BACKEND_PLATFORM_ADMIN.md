# EchoSphere Backend API — Platform & Admin Endpoints

Source of truth: `backend/main.py`, `backend/routers/*`, `backend/serializers.py`, `backend/core/*` (verified against the live OpenAPI schema). Covers health, auth, users/roles/permissions, tenants, master data, platform operations, billing, usage, analytics, audit, exports/reports and integrations.

- **Base URL:** `http://localhost:9001` (host/port come from `API_HOST` / `API_PORT` in `.env`; all v1 routes are prefixed `/api/v1`).
- **Authentication:** JWT bearer token from `POST /api/v1/auth/login`, sent as
  `Authorization: Bearer <ACCESS_TOKEN>`. Tokens expire after `ACCESS_TOKEN_EXPIRE_MINUTES` (default 720 min) and are also rejected if issued before the user's last password change ("sign out other sessions"). Only the two `/api/health` endpoints are public.
- **Roles:** `super_admin` (platform scope, holds every seeded permission), `tenant_admin`, `tenant_user`. Permission checks (`require_permission(...)`) are role-based: the user's role must hold at least one of the listed permission codes.
- **Tenant isolation:** for tenant roles the effective `tenant_id` always comes from the token; a client-supplied `tenantId` matching another tenant returns 403 (or 404 for direct-by-id reads, to avoid leaking existence). Super admins must pass `tenantId` explicitly on tenant-scoped endpoints — omitting it returns 400 `"tenant_id is required for platform administrators."`.
- **Request bodies:** Pydantic models use `populate_by_name = True`, so every aliased field accepts **both** the camelCase alias (e.g. `roleCode`) and the snake_case field name (e.g. `role_code`). Docs below show the camelCase alias.

**Success envelope** (`backend/core/responses.py`):

```json
{ "success": true, "data": { }, "meta": { "page": 1, "pageSize": 50, "total": 123, "totalPages": 3 } }
```

`meta` appears only on paginated list endpoints. Common pagination query params (`backend/core/pagination.py`): `page` (int, default 1, ≥1), `pageSize` (int, default 50, 1–200), `search` (string, ≤200), `sortBy` (string, ≤50), `sortDir` (`asc`|`desc`, default `desc`). Note: several list endpoints accept `sortBy`/`sortDir` but ignore them (called out per endpoint).

**Error envelope** (`shared/errors.py`, installed globally):

```json
{ "success": false, "message": "Human-readable message.", "errors": [ { "field": "email", "message": "value is not a valid email address" } ] }
```

`errors` is present only for field-level failures. Standard shapes:

| Status | Meaning | Typical message |
|---|---|---|
| 400 | Domain error | e.g. `"tenant_id is required for platform administrators."` |
| 401 | Not authenticated | `"Authentication required."`, `"Session expired — please sign in again."`, `"Invalid authentication token."` |
| 403 | Forbidden | `"You do not have permission to perform this action."` |
| 404 | Not found | `"<Entity> not found."` |
| 409 | Conflict / integrity | `"The record conflicts with existing data (duplicate or missing reference)."` or a domain message |
| 422 | Validation | `"Validation failed."` + `errors[]` |
| 503 | DB unavailable | `"The database is temporarily unavailable. Please try again shortly."` |

## Table of contents

- [Health](#health)
- [Auth](#auth)
- [Users, Roles & Permissions](#users-roles--permissions)
- [Tenants](#tenants)
- [Tenant Profile & Settings](#tenant-profile--settings)
- [Master Data](#master-data)
- [Languages Catalog](#languages-catalog)
- [Platform Operations](#platform-operations)
- [Billing](#billing)
- [Usage & Currency](#usage--currency)
- [Analytics & Dashboard](#analytics--dashboard)
- [Audit](#audit)
- [Exports & Reports](#exports--reports)
- [Integrations](#integrations)
- [Notes & Known Inconsistencies](#notes--known-inconsistencies)

---

## Health

### Liveness
`GET /api/health`

Basic process liveness. **Public — no auth.** No parameters.

Response `200`:

```json
{ "success": true, "data": { "status": "up", "env": "development" } }
```

### Readiness
`GET /api/health/ready`

Checks every backing service (MySQL, Postgres, Redis, MongoDB). **Public — no auth.** No parameters.

Response `200` (always 200; inspect `data.ready`):

```json
{
  "success": true,
  "data": {
    "ready": true,
    "checks": {
      "mysql": { "ok": true },
      "postgres": { "ok": true },
      "redis": { "ok": true },
      "mongodb": { "ok": true }
    }
  }
}
```

Failed checks carry `{ "ok": false, "error": "<ExceptionClassName>" }`.

---

## Auth

### Login
`POST /api/v1/auth/login`

Exchange email + password for a JWT. **Public.** On success the user's `last_login_at`/`last_active_at` are stamped and `invited` accounts become `active`.

```json
{ "email": "admin@example.com", "password": "<PASSWORD>" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| email | string (email) | yes | Compared lowercased |
| password | string | yes | 1–200 chars |

Response `200`:

```json
{
  "success": true,
  "data": {
    "token": "<ACCESS_TOKEN>",
    "user": {
      "id": "<USER_ID>",
      "name": "Jane Admin",
      "firstName": "Jane",
      "lastName": "Admin",
      "email": "admin@example.com",
      "phone": "",
      "avatarUrl": "",
      "locale": "",
      "timezone": "",
      "role": "super_admin",
      "roleName": "Super Admin",
      "tenantId": null,
      "permissions": ["manage_master_data", "billing.manage", "..."],
      "status": "active",
      "lastLoginAt": "2026-08-07T09:00:00Z",
      "passwordChangedAt": null,
      "tenantName": null
    }
  }
}
```

Errors: `401` `"Invalid email or password."`; `403` `"This account has been deactivated."` / `"Your organization is no longer active."` / `"Your organization is suspended. Contact support."`; `422` malformed body.

### Current user
`GET /api/v1/auth/me`

Returns the authenticated user's public profile (same shape as `login`'s `user`, incl. `tenantName`). **Auth: JWT bearer.** No permission needed. Errors: `401`.

### Logout
`POST /api/v1/auth/logout`

Records a "Signed out" audit entry; JWTs are stateless so the token itself is not revoked. **Auth: JWT bearer.** No body.

Response `200`: `{ "success": true, "data": { "signedOut": true } }`

---

## Users, Roles & Permissions

### List users
`GET /api/v1/users`

Paginated team-member listing (tenant scope) or platform-user listing. **Auth: JWT bearer.** Any authenticated role for tenant scope (own tenant); `scope=platform` is **Super Admin only** (`403` otherwise). Ordered by `created_at` ascending; `sortBy`/`sortDir` are accepted but **not applied**. `search` matches name or email (LIKE).

Query params: `scope` (`tenant`|`platform`, default `tenant`), `tenantId` (string, optional — Super Admin only; required for Super Admin when `scope=tenant`, else `400`), `page`, `pageSize`, `search`, `sortBy`, `sortDir`.

Response `200` (paginated):

```json
{
  "success": true,
  "data": [
    {
      "id": "<USER_ID>",
      "name": "Sam Lee",
      "email": "sam@acme.com",
      "role": "Tenant Admin",
      "roleCode": "tenant_admin",
      "status": "active",
      "lastActive": "2026-08-07T08:12:00Z",
      "botsOwned": 2,
      "mfa": false
    }
  ],
  "meta": { "page": 1, "pageSize": 50, "total": 4, "totalPages": 1 }
}
```

### Create user
`POST /api/v1/users` → `201`

Invite or create a user. **Auth: JWT bearer.** Role required: `tenant_admin` or `super_admin`. Platform-scoped roles can only be granted by Super Admins (`403`).

```json
{ "name": "Sam Lee", "email": "sam@acme.com", "roleCode": "tenant_user", "tenantId": "<TENANT_ID>", "password": null }
```

| Field | Type | Required | Description |
|---|---|---|---|
| name | string | yes | 1–150 chars |
| email | string (email) | yes | Stored lowercased; must be unique (`409`) |
| roleCode | string | yes | Must match an existing role (`422` `"Unknown role."`) |
| tenantId | string | no | Super Admin targeting a tenant; ignored/forced `null` for platform roles |
| password | string \| null | no | If set: min 8 / max 128 and must pass policy (upper+lower+digit, not in weak list) → user `active`. If omitted: a temporary password is generated and returned once → user `invited`. |

Response `201`: team-member object (as in list); plus `"temporaryPassword": "<GENERATED>"` when no password was supplied.

Errors: `409` duplicate email; `422` unknown role / password policy; `403` platform role by non-super-admin.

### Update own profile
`PATCH /api/v1/users/me`

**Auth: JWT bearer.** Any role. Unknown fields are rejected (`extra="forbid"` → `422`).

```json
{ "firstName": "Sam", "lastName": "Lee", "phone": "+14155550119", "avatarUrl": "https://…", "locale": "en-IN", "timezone": "Asia/Kolkata" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| firstName | string | no | ≤80; empty after trim → `422` `"First name cannot be empty."` |
| lastName | string | no | ≤80; providing either name recomputes `name` |
| phone | string | no | ≤30 |
| avatarUrl | string | no | ≤500 |
| locale | string | no | ≤15 |
| timezone | string | no | ≤64 |

Response `200`: updated public user object (same shape as `/auth/me`).

### Change own password
`POST /api/v1/users/me/password`

Rotates the caller's password, invalidates all other sessions and returns a fresh token. **Auth: JWT bearer.** Any role.

```json
{ "currentPassword": "<OLD>", "newPassword": "<NEW>", "confirmPassword": "<NEW>" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| currentPassword | string | yes | 1–200; wrong value → `400` |
| newPassword | string | yes | 1–128; policy-checked; must differ from current (`422`) |
| confirmPassword | string | yes | Must equal `newPassword` (`422`) |

Response `200`:

```json
{ "success": true, "data": { "changed": true, "token": "<NEW_ACCESS_TOKEN>", "message": "Password changed. Other sessions have been signed out." } }
```

### Admin reset user password
`POST /api/v1/users/{user_id}/reset-password`

Two modes: admin-chosen password, or a one-time temporary password (target forced back to `invited`). Either way the target's sessions are invalidated. **Auth: JWT bearer.** Permission: `reset_user_password`. Tenant-scoped: targets outside the caller's tenant → `404`; platform accounts and Super Admin targets require a Super Admin caller (`404`/`403`); resetting your own account → `400`.

Body is **optional** (`extra="forbid"`):

```json
{ "newPassword": "<NEW>", "confirmPassword": "<NEW>" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| newPassword | string \| null | no | ≤128; policy-checked; both fields must match (`422`) |
| confirmPassword | string \| null | no | Must equal `newPassword` |

Response `200` — chosen password: `{ "reset": true, "sessionsInvalidated": true }`; temporary mode adds `"temporaryPassword": "<GENERATED>"` (returned once, never stored).

### Update user
`PATCH /api/v1/users/{user_id}`

**Auth: JWT bearer.** Role: `tenant_admin` or `super_admin`; cross-tenant targets → `404`; platform users only editable by Super Admin (`404`).

```json
{ "name": "Sam L.", "roleCode": "tenant_admin", "status": "active" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| name | string | no | ≤150 |
| roleCode | string | no | `422` unknown role; platform roles: Super Admin only (`403`), and never on tenant members (`422`) |
| status | string | no | `active`\|`invited`\|`deactivated`; self-deactivation → `400` |

Response `200`: team-member object.

### Archive user
`DELETE /api/v1/users/{user_id}`

Soft-deletes (archives) the user and sets status `deactivated`. **Auth: JWT bearer.** Role: `tenant_admin`/`super_admin`, same scoping as PATCH. Self-deletion → `400`.

Query: `hard` (bool, default `false`) — when `true`, blocked with `403` unless `ALLOW_HARD_DELETE=true`; **even when allowed the row is still only soft-deleted** (see Inconsistencies).

Response `200`: `{ "success": true, "data": { "archived": true, "id": "<USER_ID>" } }`

### List roles
`GET /api/v1/roles`

All roles with permission codes and live member counts. **Auth: JWT bearer.** Any authenticated role (no admin gate). No parameters.

```json
{ "success": true, "data": [ { "id": "role_…", "code": "tenant_admin", "name": "Tenant Admin", "description": "…", "scope": "tenant", "permissions": ["manage_team", "…"], "permissionCount": 12, "members": 3 } ] }
```

### List permissions
`GET /api/v1/permissions`

Full permission catalog. **Auth: JWT bearer.** Any authenticated role. No parameters.

```json
{ "success": true, "data": [ { "id": "perm_…", "code": "manage_plans", "name": "Manage Plans", "category": "Platform", "description": "" } ] }
```

---

## Tenants

Tenant serializer fields (used by all tenant endpoints): `id`, `name`, `code`, `domain`, `industry`, `region`, `aiProfileCode`, `defaultLanguages`, `plan` (defaults to `"starter"` when no subscription), `status`, `createdAt`, `users`, `bots`, `callsMonth`, `minutesMonth`, `mrr`, `aiCostMonth`, `health`, `adminEmail`, `callSummaryEnabled`, `usePreviousCallSummary`, `website`, `contactName`, `contactPhone`, `address`, `country`.

### List tenants
`GET /api/v1/tenants`

**Auth: JWT bearer. Super Admin only.** Ordered by `created_at` ascending; `sortBy`/`sortDir` accepted but not applied. `search` matches name or domain.

Query: `page`, `pageSize`, `search`, `sortBy`, `sortDir`, `status` (string, optional — matched verbatim against `active|trial|suspended|provisioning`), `plan` (string, optional plan code — **applied in Python after pagination**, so `meta.total` ignores it; see Inconsistencies).

Response `200` (paginated): array of tenant objects.

```json
{
  "success": true,
  "data": [
    {
      "id": "<TENANT_ID>", "name": "Acme Corp", "code": "acme", "domain": "acme.com",
      "industry": "banking", "region": "in-mumbai", "aiProfileCode": "balanced",
      "defaultLanguages": ["en-IN", "hi-IN"], "plan": "growth", "status": "active",
      "createdAt": "2026-01-05T10:00:00Z", "users": 8, "bots": 3, "callsMonth": 1200,
      "minutesMonth": 4100, "mrr": 499.0, "aiCostMonth": 210, "health": "good",
      "adminEmail": "admin@acme.com", "callSummaryEnabled": false, "usePreviousCallSummary": false,
      "website": "", "contactName": "", "contactPhone": "", "address": "", "country": ""
    }
  ],
  "meta": { "page": 1, "pageSize": 50, "total": 12, "totalPages": 1 }
}
```

### Create tenant
`POST /api/v1/tenants` → `201`

Creates tenant + subscription + settings + (if the email is new) a tenant-admin user, in one transaction. **Auth: JWT bearer. Super Admin only.**

```json
{
  "name": "Acme Corp",
  "code": "acme",
  "domain": "acme.com",
  "industry": "banking",
  "region": "in-mumbai",
  "aiProfileCode": "balanced",
  "planCode": "growth",
  "adminEmail": "admin@acme.com",
  "adminName": "Tenant Admin",
  "adminPassword": null,
  "status": "provisioning",
  "seats": 10,
  "defaultLanguages": ["en-IN", "hi-IN"],
  "callSummaryEnabled": false,
  "usePreviousCallSummary": false
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| name | string | yes | 1–200 |
| code | string \| null | no | ≤50, stored trimmed+lowercase; unique (`409`) |
| domain | string | yes | 3–255, stored lowercase; unique (`409`) |
| industry | string \| null | no | Active industry code or name (`422` if unknown/inactive) |
| region | string \| null | no | Active data-region code or name (`422`) |
| aiProfileCode | string \| null | no | Active AI profile code or name (`422`) |
| planCode | string | no (default `starter`) | Must be an existing **active** plan (`422`) |
| adminEmail | string (email) | yes | Tenant admin account; if the email already exists no user is created |
| adminName | string | no (default `"Tenant Admin"`) | |
| adminPassword | string \| null | no | min 8 + policy; omitted → temporary password returned once, admin user `invited` |
| status | string | no (default `provisioning`) | `active`\|`trial`\|`suspended`\|`provisioning` |
| seats | int \| null | no | ≥1; defaults to the plan's `seats_included` |
| defaultLanguages | string[] \| null | no | Active language codes; empty result → `422`; unknown/inactive codes → `422` |
| callSummaryEnabled | bool | no (default `false`) | Post-call intelligence opt-in |
| usePreviousCallSummary | bool | no (default `false`) | |

Response `201`: tenant object, plus `"adminUser": { "email": "…", "temporaryPassword": "<GENERATED>" }` when a new admin user was created (temporaryPassword only when no `adminPassword` was supplied). Subscription is created `trial` (mrr 0) when `status=trial`, else `active` with the plan's monthly price.

Errors: `409` duplicate domain/code; `422` unknown plan/master refs/languages; `500` if the `tenant_admin` role seed is missing.

### Get tenant
`GET /api/v1/tenants/{tenant_id}`

**Auth: JWT bearer.** Role: `tenant_admin`/`super_admin`; tenant admins can only fetch their own tenant (others → `404`). Response `200`: tenant object. `404` unknown/deleted.

### Update tenant
`PATCH /api/v1/tenants/{tenant_id}`

**Auth: JWT bearer. Super Admin only.** All fields optional; only provided fields change.

```json
{ "name": "Acme Corp", "code": "acme", "industry": "banking", "region": "in-mumbai", "aiProfileCode": "balanced", "planCode": "enterprise", "status": "active", "health": "good", "adminEmail": "admin@acme.com", "defaultLanguages": ["en-IN"], "callSummaryEnabled": true, "usePreviousCallSummary": false }
```

| Field | Type | Required | Description |
|---|---|---|---|
| name | string | no | ≤200 |
| code | string | no | ≤50; unique (`409`); empty string clears it |
| industry / region / aiProfileCode | string | no | Active master-data code/name (`422`) |
| planCode | string | no | Active plan (`422`); changing plan snapshots `bot_limit`, `minutes_included` and (if subscription active) `mrr` onto the subscription |
| status | string | no | `active`\|`trial`\|`suspended`\|`provisioning` |
| health | string | no | `good`\|`warning`\|`serious`\|`critical`\|`neutral` |
| adminEmail | string (email) | no | Stored lowercased |
| defaultLanguages | string[] | no | Same validation as create (`422`) |
| callSummaryEnabled / usePreviousCallSummary | bool | no | |

Response `200`: tenant object. Errors: `404`, `409`, `422`.

### Archive tenant
`DELETE /api/v1/tenants/{tenant_id}`

Soft-deletes the tenant and sets status `suspended`. **Auth: JWT bearer. Super Admin only.** Query: `hard` (bool, default false — `403` unless `ALLOW_HARD_DELETE=true`; still soft-deletes, see Inconsistencies).

Response `200`: `{ "archived": true, "id": "<TENANT_ID>" }`. `404` unknown/already deleted.

---

## Tenant Profile & Settings

### Get tenant profile
`GET /api/v1/tenant/profile`

Combined tenant + settings + subscription view for the Company Profile screen. **Auth: JWT bearer.** Permission: `view_tenant_profile` **or** `tenants.manage`. Query: `tenantId` (optional for tenant roles — must be own tenant or `403`; **required** for Super Admin → `400` otherwise).

Response `200`:

```json
{
  "success": true,
  "data": {
    "tenantId": "<TENANT_ID>", "name": "Acme Corp", "displayName": "Acme Corp", "code": "acme",
    "domain": "acme.com", "industry": "banking", "website": "https://acme.com",
    "contactName": "Jo Ops", "contactEmail": "ops@acme.com", "contactPhone": "+91…",
    "address": "…", "country": "India", "timezone": "Asia/Kolkata",
    "defaultLanguages": ["en-IN"], "branding": { "supportEmail": "help@acme.com" },
    "supportEmail": "help@acme.com", "supportPhone": "", "workingHours": {},
    "dataRegion": "in-mumbai", "dataRegionName": "India (Mumbai)",
    "dataRegionInfrastructureReady": true, "plan": "growth", "planName": "Growth",
    "subscriptionStatus": "active", "status": "active", "aiProfileCode": "balanced"
  }
}
```

Fields after `workingHours` are read-only (Super Admin controlled via `PATCH /tenants/{id}`).

### Update tenant profile
`PUT /api/v1/tenant/profile`

Tenant-admin-editable fields **only** — `extra="forbid"`, so payloads containing plan/code/region/status are rejected with `422`. **Auth: JWT bearer.** Permission: `edit_tenant_profile` **or** `tenants.manage`. Query: `tenantId` (same rules as GET).

```json
{
  "displayName": "Acme Corp", "website": "https://acme.com", "contactName": "Jo Ops",
  "contactEmail": "ops@acme.com", "contactPhone": "+91…", "address": "…", "country": "India",
  "timezone": "Asia/Kolkata", "defaultLanguages": ["en-IN", "hi-IN"],
  "branding": { "logoUrl": "…" }, "supportEmail": "help@acme.com", "supportPhone": "+91…",
  "workingHours": { "mon": ["09:00", "18:00"] }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| displayName | string | no | ≤200; blank after trim keeps previous value |
| website | string | no | ≤300 |
| contactName | string | no | ≤150 |
| contactEmail | string (email) | no | Stored lowercased |
| contactPhone | string | no | ≤30 |
| address | string | no | ≤500 |
| country | string | no | ≤100 |
| timezone | string | no | ≤64 |
| defaultLanguages | string[] | no | Active language codes; empty → `422` |
| branding | object | no | Merged into existing branding JSON |
| supportEmail | string (email) | no | Stored inside `branding.supportEmail` |
| supportPhone | string | no | ≤30; stored inside `branding.supportPhone` |
| workingHours | object | no | Replaces `businessHours` |

Response `200`: same shape as `GET /tenant/profile`. Errors: `404`, `422`.

### Get tenant settings
`GET /api/v1/tenant/settings`

Returns (and lazily creates) the tenant's settings row. **Auth: JWT bearer.** Any authenticated role (tenant-scoped; Super Admin must pass `tenantId`). Query: `tenantId` (optional/required as above). `404` unknown tenant.

```json
{
  "success": true,
  "data": {
    "tenantId": "<TENANT_ID>", "displayName": "Acme Corp", "timezone": "Asia/Kolkata",
    "defaultLanguages": ["en-IN"], "branding": {}, "businessHours": {}, "holidays": [],
    "notifications": [], "security": {}, "retentionDays": 365
  }
}
```

### Update tenant settings
`PUT /api/v1/tenant/settings`

**Auth: JWT bearer.** Role: `tenant_admin` or `super_admin`. Query: `tenantId` (same rules). Only provided (non-null) fields change.

| Field | Type | Required | Description |
|---|---|---|---|
| displayName | string | no | ≤200 |
| timezone | string | no | |
| defaultLanguages | string[] | no | Validated against active languages (`422`) |
| branding | object | no | Replaces the JSON blob |
| businessHours | object | no | |
| holidays | array | no | |
| notifications | array | no | |
| security | object | no | |
| retentionDays | int | no | 1–3650 |

Response `200`: tenant-settings object (as in GET).

---

## Master Data

Generic Super Admin CRUD over 12 catalog types (`backend/routers/master_data.py`). `{mtype}` must be one of:

| mtype | Entity | Permission (or `manage_master_data`) | Notes |
|---|---|---|---|
| `industries` | Industry | `manage_industries` | |
| `countries` | Country | `manage_data_regions` | Integer auto-increment ids; no `code` field; region locked to `Asia` |
| `data-regions` | Data region | `manage_data_regions` | `countryId` FK required and must be active |
| `plans` | Plan | `manage_plans` | |
| `ai-profiles` | AI config profile | `manage_ai_profiles` | Provider/model pairs validated against the provider catalog |
| `providers` | Provider definition | `manage_master_data` | `kind` required on create: `voice`\|`stt`\|`tts`\|`llm`\|`embedding`; code unique per kind |
| `provider-models` | Provider model | `manage_master_data` | `capability` (`stt`\|`tts`\|`llm`\|`embedding`) + `providerCode` immutable after create; code unique per provider+capability, case preserved |
| `languages` | Supported language | `manage_languages` | Uses `enabled` instead of `status`; code ≤15 chars, case preserved |
| `voices` | Voice profile | `manage_master_data` | No `code`; provider/model/settings validated against catalog + `params_schema` |
| `currencies` | Currency | `manage_currencies` | 3-letter ISO 4217, uppercased; base currency (USD) protected |
| `exchange-rates` | Exchange rate | `manage_exchange_rates` | Nameless; `baseCode` must equal the platform base (USD) |
| `provider-pricing` | Provider price | `manage_pricing` | Nameless; capability/provider/model/component immutable after create |

The **same permission gates reads and writes** — listing a type requires its manage permission. Every mutation is audited. Referenced records can be deactivated/archived but never hard-deleted.

Common response fields for all types (`_master_common`): `id`, `status` (`active`|`inactive`|`archived`; `languages` expose `enabled` instead), `usageCount` (live reference count), `createdAt`, `updatedAt`, `createdBy`, `updatedBy` (resolved to user names when known). Type-specific fields mirror the create/update payloads in camelCase — e.g. plans add `code, name, description, priceMonthly, priceAnnual, currency, botLimit, minutesIncluded, seatsIncluded, kbLimit, storageGbIncluded, languagesIncluded, concurrentCallLimit, monthlyCallLimit, monthlyTokenLimit, monthlyEmbeddingLimit, recordingRetentionDays, transcriptRetentionDays, analyticsRetentionDays, features, overageRates, isPublic, isRecommended, sortOrder`; exchange rates serialize `rate` **as a string** (Numeric(18,8) precision); provider pricing serializes `unitPrice`/`sellingPrice` as strings.

### List master records
`GET /api/v1/master/{mtype}`

**Auth: JWT bearer.** Permission: per-type (table above) or `manage_master_data`; unknown `{mtype}` → `404` `"Master data type not found."`.

Query params (all optional):

| Param | Type | Default | Applies to | Description |
|---|---|---|---|---|
| `kind` | string | — | providers | Filter by provider kind |
| `capability` | string | — | provider-models, provider-pricing | `stt`\|`tts`\|`llm`\|`embedding` (+`telephony` for pricing) |
| `provider` | string | — | voices, provider-models, provider-pricing | Provider code |
| `gender` | string | — | voices | |
| `language` | string | — | voices | Locale prefix or member of `languages[]` |
| `status` | string | — | all | `active`\|`inactive`\|`archived` (pattern-validated). For `languages`, `active` → `enabled=true`, any other value → `enabled=false` |
| `includeInactive` | bool | `true` | all | `false` restricts to active/enabled |
| `page`/`pageSize`/`search` | | 1 / 50 | all | `search` fields vary per type (e.g. plans: code/name/description; voices also match id) |
| `sortBy` | string | — | all | One of `name`, `code`, `iso2`, `iso3`, `createdAt`, `updatedAt`, `sortOrder` (default: `sort_order` ascending) |
| `sortDir` | string | `desc` | all | Only honored with a valid `sortBy` |

Ordering guarantee: active/enabled records always sort before inactive/archived ones, regardless of `sortBy`; ties break by name/display name then id.

Response `200` (paginated), e.g. an industry row:

```json
{
  "success": true,
  "data": [
    {
      "id": "ind_ab12…", "status": "active", "usageCount": 3,
      "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-06-01T00:00:00Z",
      "createdBy": "Jane Admin", "updatedBy": "Jane Admin",
      "code": "banking", "name": "Banking", "description": "", "icon": "🏦", "sortOrder": 1,
      "defaultPromptTemplateId": null, "defaultGuardrailProfileId": null, "defaultWorkflowTemplateId": null
    }
  ],
  "meta": { "page": 1, "pageSize": 50, "total": 8, "totalPages": 1 }
}
```

### Create master record
`POST /api/v1/master/{mtype}` → `201`

**Auth: JWT bearer.** Permission as per table. Body is a free-shape JSON object (`extra="allow"`); camelCase keys are converted to snake_case, and only the type's editable-field allowlist is applied — unknown fields are silently ignored.

Example (`plans`):

```json
{
  "code": "growth", "name": "Growth", "description": "For scaling teams",
  "priceMonthly": 499, "priceAnnual": 4990, "currency": "USD",
  "botLimit": 10, "minutesIncluded": 5000, "seatsIncluded": 10, "kbLimit": 20,
  "storageGbIncluded": 50, "languagesIncluded": 5, "concurrentCallLimit": 25,
  "monthlyCallLimit": 100000, "monthlyTokenLimit": 0, "monthlyEmbeddingLimit": 0,
  "recordingRetentionDays": 90, "transcriptRetentionDays": 365, "analyticsRetentionDays": 365,
  "features": ["voice", "whatsapp"], "overageRates": { "perMinute": 0.05 },
  "isPublic": true, "isRecommended": false, "sortOrder": 2
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| code | string | yes, except `voices` and `countries` | ≤50; unique per type (`409`); lowercased except languages (≤15, case kept), currencies (uppercased) and provider-models (case kept) |
| name | string | yes, except `exchange-rates` / `provider-pricing` | ≤150 (`422` when missing) |
| kind | string | providers only | `voice`\|`stt`\|`tts`\|`llm`\|`embedding` (`422`) |
| *(type-specific)* | — | varies | Editable fields per type (see `_EDITABLE` in `master_data.py`); the payloads mirror the serializer fields listed above |

Cross-type validation (all `422` with per-field `errors[]` unless noted):
- Non-negative numeric checks per type (e.g. all plan limits, `sortOrder` everywhere, voice `latencyMs/speakingRate/pitch`, currency `decimalPlaces` 0–4 integer).
- `plans.currency` must be an **active** currency code (DB-driven; bootstrap fallback list `INR, USD, EUR, GBP, AED`).
- `countries`: `iso2`/`iso3` strict 2/3-letter alpha, uppercased, unique; name unique; `region` must be `Asia` (forced).
- `data-regions`: `countryId` (or legacy `countryCode`/`country`) must resolve to an active country; canonical name/region snapshotted.
- `languages.direction` ∈ `ltr`|`rtl`.
- `ai-profiles`: each of stt/tts/llm/embedding provider+model must exist in the provider catalog; model without provider rejected.
- `provider-models`: capability ∈ `stt|tts|llm|embedding`; provider must exist for that capability; `languages`/`codecs`/`sampleRates` lists; `paramsSchema` object; `streaming` boolean.
- `voices`: provider must be an active `tts`/`voice` provider; `modelCodes` must belong to it; `providerSettings` validated against the model's `params_schema`; `locale` must be model-supported when the model declares languages.
- `currencies`: ISO-4217 code, symbol 1–8 chars required.
- `exchange-rates`: `baseCode` must be `USD` (platform base); base ≠ target; both currencies active; `rate` > 0 (≤ 10,000,000); `effectiveFrom` ISO datetime, not before year 2000 or more than 366 days ahead; duplicate pair+effective date → `422`.
- `provider-pricing`: `capability` ∈ `llm|embedding|stt|tts|telephony`; `component` ∈ `input_tokens|output_tokens|cached_input_tokens|tokens|characters|audio_seconds|call_seconds|requests`; `unit` ∈ `per_token|per_1k_tokens|per_1m_tokens|per_character|per_1k_characters|per_1m_characters|per_second|per_minute|per_hour|per_request`; `unitPrice` > 0 (≤ 1,000,000); optional `sellingPrice` > 0 (empty string clears it); AI capabilities must reference a configured provider/model; non-USD `currencyCode` requires an existing USD→X exchange rate; duplicate provider/model/component/effective-date → `422`.

Response `201`: the serialized record. Errors: `404` unknown type, `409` duplicate code, `422` validation.

### Update master record
`PATCH /api/v1/master/{mtype}/{item_id}`

**Auth: JWT bearer.** Permission as per table. Same free-shape body; partial update (null/absent fields ignored, except clearable fields — ai-profile provider/model/`defaultVoice`, voice `locale`/`providerVoiceId`/`accent`, pricing `sellingPrice` — where empty string/null clears the column). Immutable-after-create: provider-model `capability`/`providerCode`; provider-pricing `capability`/`providerCode`/`modelCode`/`component` (`422` "Cannot be changed after creation…"). Setting `isDefault: true` on voices/languages/provider-models clears the previous default (per provider for voices, per provider+capability for models, platform-wide for languages). Editing a country's name re-syncs the snapshot on its data regions.

Response `200`: serialized record. Errors: `404`, `422`.

### Set master record status
`POST /api/v1/master/{mtype}/{item_id}/status`

Lifecycle transition. **Auth: JWT bearer.** Permission as per table.

```json
{ "status": "inactive" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| status | string | yes | `active`\|`inactive`\|`archived`. For `languages` this maps to `enabled` (only `active` enables). Base currency cannot leave `active` (`422`). |

Response `200`: serialized record. Deactivating `providers`/`provider-models`/`voices` invalidates all cached bot runtime configs immediately.

### Archive master record
`DELETE /api/v1/master/{mtype}/{item_id}`

Reference-protected archive (soft delete); hard removal is never performed. **Auth: JWT bearer.** Permission as per table.

Errors: `404`; `409` when `usageCount > 0` (`"This <type> is used by N existing records and cannot be deleted. Deactivate or archive it instead."`); `409` for the base currency.

Response `200`: `{ "archived": true, "id": "…" }`. Cache invalidation as for status changes.

### Master record audit trail
`GET /api/v1/master/{mtype}/{item_id}/audit`

Last 100 audit entries for one record. **Auth: JWT bearer.** Permission as per table. No pagination params.

```json
{ "success": true, "data": [ { "id": "aud_…", "actor": "Jane Admin", "action": "Updated plan", "previousValue": { "status": "active" }, "newValue": { "status": "inactive" }, "time": "2026-08-01T10:00:00+00:00Z" } ] }
```

(Note: `time` is `isoformat() + "Z"`, which produces a double offset like `+00:00Z` for tz-aware timestamps — cosmetic, see Inconsistencies.)

### Duplicate plan
`POST /api/v1/master/plans/{item_id}/duplicate` → `201`

Clones a plan as `inactive` with code `<code>_copy` (then `_copy2`, `_copy3`, … until unique), name `"<name> (copy)"`, and `isRecommended` forced false. **Auth: JWT bearer.** Permission: `manage_plans` or `manage_master_data`. No body. Response `201`: serialized plan. `404` unknown plan.

### Tenants on a plan
`GET /api/v1/master/plans/{item_id}/tenants`

**Auth: JWT bearer.** Permission: `manage_plans` or `manage_master_data`.

```json
{ "success": true, "data": [ { "id": "<TENANT_ID>", "name": "Acme Corp", "domain": "acme.com", "subscriptionStatus": "active", "mrr": 499.0 } ] }
```

---

## Languages Catalog

### List supported languages
`GET /api/v1/languages`

Read-only language catalog for authoring screens (management lives under `/master/languages`). **Auth: JWT bearer.** Any authenticated role.

Query: `includeDisabled` (bool, default `false`), `tenantId` (string, optional — restricts to the tenant's assigned languages; tenant roles may only pass their own tenant (`403`), Super Admins any; `404` unknown tenant).

```json
{
  "success": true,
  "data": [
    {
      "id": "lang_…", "code": "en-IN", "name": "English (India)", "nativeName": "English",
      "isoCode": "en", "script": "Latin", "direction": "ltr",
      "providerSupport": { "sarvam": true }, "isDefault": true, "enabled": true,
      "sortOrder": 1, "usageCount": 4, "updatedAt": "2026-06-01T00:00:00Z"
    }
  ]
}
```

---

## Platform Operations

### List system settings
`GET /api/v1/system-settings`

**Auth: JWT bearer. Super Admin only.** No parameters. Ordered by key.

```json
{ "success": true, "data": [ { "key": "maintenance_mode", "value": false, "description": "" } ] }
```

### Upsert system setting
`PUT /api/v1/system-settings`

Creates or updates one setting by key. **Auth: JWT bearer. Super Admin only.**

```json
{ "key": "maintenance_mode", "value": false, "description": "Global maintenance flag" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| key | string | yes | 1–100 chars; upsert key |
| value | object \| array \| string \| int \| bool \| null | no (default null) | Stored as JSON |
| description | string \| null | no | ≤500; unchanged when omitted |

Response `200`: `{ "key": "…", "value": …, "description": "…" }`.

### Health metrics
`GET /api/v1/health-metrics`

Stored platform health snapshot rows. **Auth: JWT bearer. Super Admin only.** No parameters.

```json
{ "success": true, "data": [ { "name": "API latency p95", "status": "ok", "value": "180ms", "target": "<250ms", "spark": [1, 2, 3] } ] }
```

### List alerts
`GET /api/v1/alerts`

Latest 100 platform alerts, newest first. **Auth: JWT bearer.** Any authenticated role — Super Admins see all alerts; tenant members see only alerts with `scope=tenant` for their own tenant.

Query: `status` (`open`|`acknowledged`|`resolved`, optional, pattern-validated).

```json
{ "success": true, "data": [ { "id": "alrt_…", "severity": "critical", "title": "TTS provider errors", "source": "voice-runtime", "time": "2026-08-07T06:00:00Z", "status": "open", "scope": "platform" } ] }
```

### Update alert status
`PATCH /api/v1/alerts/{alert_id}`

Acknowledge/resolve an alert. **Auth: JWT bearer. Super Admin only** (tenant members can list but not update — see Inconsistencies).

```json
{ "status": "acknowledged" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| status | string | yes | `open`\|`acknowledged`\|`resolved` |

Response `200`: serialized alert. `404` unknown alert.

### List guardrails
`GET /api/v1/guardrails`

**Auth: JWT bearer. Super Admin only.** No parameters.

```json
{ "success": true, "data": [ { "id": "gr_…", "name": "PII redaction", "category": "privacy", "description": "…", "enforcement": "redact", "enabled": true, "triggers30d": 12 } ] }
```

### Update guardrail
`PATCH /api/v1/guardrails/{guardrail_id}`

**Auth: JWT bearer. Super Admin only.**

```json
{ "enabled": true, "enforcement": "block" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| enabled | bool \| null | no | |
| enforcement | string \| null | no | `block`\|`flag`\|`redact` |

Response `200`: serialized guardrail. `404` unknown.

### Admin dashboard
`GET /api/v1/dashboard/admin`

Platform KPI cards computed from live data (30-day window vs previous 30 days). **Auth: JWT bearer. Super Admin only.** No parameters.

```json
{
  "success": true,
  "data": {
    "kpis": [
      { "label": "Active tenants", "value": "12", "spark": [], "intent": "up-good" },
      { "label": "Calls (30d)", "value": "8,314", "delta": 12.5, "spark": [190, 204], "intent": "up-good" }
    ],
    "activeTenants": 12,
    "liveBots": 9
  }
}
```

KPI objects: `label`, `value` (formatted string), optional `delta` (percent vs previous window, omitted when previous ≤ 0), `spark` (numbers), `intent` (`up-good`|`down-good`).

### Onboarding options
`GET /api/v1/onboarding/options`

Active master-data choices for the tenant-onboarding wizard. **Auth: JWT bearer.** Permission: `tenants.manage` or `manage_master_data`. No parameters.

```json
{
  "success": true,
  "data": {
    "industries": [ { "code": "banking", "name": "Banking", "icon": "🏦" } ],
    "dataRegions": [ { "code": "in-mumbai", "name": "India (Mumbai)", "infrastructureReady": true } ],
    "plans": [ { "code": "growth", "name": "Growth", "description": "", "priceMonthly": 499.0, "minutesIncluded": 5000, "botLimit": 10, "seatsIncluded": 10, "isRecommended": true } ],
    "aiProfiles": [ { "code": "balanced", "name": "Balanced", "description": "", "costCategory": "standard" } ],
    "languages": [ { "code": "en-IN", "name": "English (India)", "nativeName": "English", "direction": "ltr" } ]
  }
}
```

Only `active` (and for plans also `isPublic`) records are returned.

---

## Billing

### List plans
`GET /api/v1/plans`

Full plan catalog ordered by monthly price. **Auth: JWT bearer. Super Admin only.** No parameters. **Note:** no `is_deleted`/status filter is applied — archived and soft-deleted plans are included (see Inconsistencies).

Response `200`: array of plan objects (same serializer as `/master/plans`; `usageCount` always 0 here):

```json
{ "success": true, "data": [ { "id": "pl_…", "code": "starter", "name": "Starter", "description": "", "priceMonthly": 99.0, "priceAnnual": 990.0, "currency": "USD", "botLimit": 2, "minutesIncluded": 1000, "seatsIncluded": 3, "kbLimit": 5, "storageGbIncluded": 10, "languagesIncluded": 2, "concurrentCallLimit": 5, "monthlyCallLimit": 10000, "monthlyTokenLimit": 0, "monthlyEmbeddingLimit": 0, "recordingRetentionDays": 30, "transcriptRetentionDays": 90, "analyticsRetentionDays": 180, "features": [], "overageRates": {}, "status": "active", "isPublic": true, "isRecommended": false, "sortOrder": 1, "usageCount": 0, "createdAt": "…", "updatedAt": "…", "createdBy": "", "updatedBy": "" } ] }
```

### List subscriptions
`GET /api/v1/subscriptions`

**Auth: JWT bearer. Super Admin only.** Ordered by `created_at` ascending; `sortBy`/`sortDir` accepted but not applied; `search` matches tenant name.

Query: `page`, `pageSize`, `search`, `sortBy`, `sortDir`.

```json
{
  "success": true,
  "data": [
    { "id": "sub_…", "tenantId": "<TENANT_ID>", "tenant": "Acme Corp", "plan": "growth", "seats": 10, "botLimit": 10, "minutesIncluded": 5000, "minutesUsed": 4100, "renewsAt": "2026-09-01T00:00:00Z", "status": "active", "mrr": 499.0 }
  ],
  "meta": { "page": 1, "pageSize": 50, "total": 12, "totalPages": 1 }
}
```

`minutesUsed` is the current calendar month's tenant-level usage rollup.

### List invoices
`GET /api/v1/invoices`

**Auth: JWT bearer. Super Admin only.** Newest first (`issued_at` desc); `search` matches tenant name.

Query: `page`, `pageSize`, `search`, `sortBy`, `sortDir` (sort params not applied), `status` (string, optional — invoice statuses are `paid`|`open`|`past_due`|`void`, but this filter is **not** pattern-validated here).

```json
{
  "success": true,
  "data": [ { "id": "inv_…", "tenantId": "<TENANT_ID>", "tenant": "Acme Corp", "period": "2026-07", "amount": 499.0, "status": "paid", "issuedAt": "2026-08-01T00:00:00Z" } ],
  "meta": { "page": 1, "pageSize": 50, "total": 34, "totalPages": 1 }
}
```

### Download invoice PDF
`GET /api/v1/invoices/{invoice_id}/pdf`

Streams the invoice as PDF. **Auth: JWT bearer.** Permission: `billing.manage` **and** the caller must be a Super Admin (`403` `"Only platform billing administrators can download invoices."` otherwise). Download is audited.

Response `200`: binary `application/pdf` with headers `Content-Disposition: attachment; filename="echosphere-invoice-<INVOICE_ID>-<DATE>.pdf"`, `Content-Length`, `X-Content-Type-Options: nosniff`. Errors: `404` unknown invoice/tenant.

---

## Usage & Currency

### Platform usage
`GET /api/v1/usage/platform`

Platform-wide metered usage/cost from `usage_events`. **Auth: JWT bearer. Super Admin only.**

Query: `days` (int, default 30, 1–365), `capability` (string, optional — one of `llm`|`embedding`|`stt`|`tts`|`telephony`, not pattern-validated), `tenantId` (string, optional filter).

```json
{
  "success": true,
  "data": {
    "period": { "start": "2026-07-08T09:00:00Z", "end": "2026-08-07T09:00:00Z", "days": 30 },
    "baseCurrency": "USD",
    "totalCostUsd": 812.44,
    "totalCostConverted": { "INR": 67854.1, "EUR": 748.9 },
    "missingPriceEvents": 0,
    "byTenant": [ { "tenantId": "<TENANT_ID>", "tenant": "Acme Corp", "requests": 1200, "inputTokens": 90000, "outputTokens": 41000, "cachedTokens": 0, "totalTokens": 131000, "characters": 520000, "audioSeconds": 96000.0, "costUsd": 310.2, "chargeUsd": 402.5, "missingPriceEvents": 0 } ],
    "byCapability": { "llm": { "requests": 0, "inputTokens": 0, "outputTokens": 0, "cachedTokens": 0, "totalTokens": 0, "characters": 0, "audioSeconds": 0.0, "costUsd": 0.0, "chargeUsd": 0.0, "missingPriceEvents": 0 } },
    "byProviderModel": [ { "capability": "tts", "provider": "sarvam", "model": "bulbul:v2", "requests": 300, "inputTokens": 0, "outputTokens": 0, "cachedTokens": 0, "totalTokens": 0, "characters": 220000, "audioSeconds": 0.0, "costUsd": 120.1, "chargeUsd": 150.0, "missingPriceEvents": 0 } ]
  }
}
```

### Tenant usage summary
`GET /api/v1/usage/summary`

Tenant-scoped usage + cost. **Auth: JWT bearer.** Any authenticated role — tenant roles locked to their own tenant; Super Admin must pass `tenantId` (`400` otherwise).

Query: `days` (int, default 30, 1–365), `tenantId` (string — see above), `botId` (string, optional filter).

Response `200`: like `/usage/platform` but with `tenantId` at the top, no `byTenant`, and `capabilities` (every capability key present, zero-filled): `{ "tenantId": "…", "period": {…}, "baseCurrency": "USD", "totalCostUsd": 12.3, "totalCostConverted": {…}, "missingPriceEvents": 0, "capabilities": { "llm": {…}, "embedding": {…}, "stt": {…}, "tts": {…}, "telephony": {…} }, "byProviderModel": [ … ] }`.

### Session usage breakdown
`GET /api/v1/usage/sessions/{session_id}`

Per-call cost audit: every usage event of one voice session. **Auth: JWT bearer.** Any authenticated role; the session's tenant must match the caller's tenant (mismatch → `404`, Super Admin unrestricted). `404` `"Session usage not found."` when no events exist.

```json
{
  "success": true,
  "data": {
    "sessionId": "<SESSION_ID>", "tenantId": "<TENANT_ID>", "botId": "<BOT_ID>",
    "baseCurrency": "USD", "totalCostUsd": 0.042, "totalChargeUsd": 0.055,
    "aiVoiceCostUsd": 0.031,
    "costByCapability": { "stt": 0.012, "tts": 0.019, "llm": 0.011 },
    "totalCostConverted": { "INR": 3.51 },
    "events": [
      {
        "id": "ue_…", "capability": "tts", "provider": "sarvam", "model": "bulbul:v2",
        "voice": "anushka", "occurredAt": "2026-08-07T06:10:00Z", "requests": 1,
        "inputTokens": 0, "outputTokens": 0, "cachedTokens": 0, "reasoningTokens": 0,
        "totalTokens": 0, "characters": 220, "audioSeconds": 14.2,
        "usageSource": "provider", "usageMetadata": { "basis": "provider_metrics" },
        "pricingStatus": "priced", "pricingSnapshot": { "unit": "per_hour", "unitPrice": "…" },
        "costUsd": 0.019, "chargeUsd": 0.024
      }
    ]
  }
}
```

### Currency rates
`GET /api/v1/currency/rates`

Active display currencies + the USD→X rates in force; powers the display-currency selector. **Auth: JWT bearer.** Any authenticated role. No parameters.

```json
{
  "success": true,
  "data": {
    "baseCurrency": "USD",
    "currencies": [ { "code": "USD", "name": "US Dollar", "symbol": "$", "decimalPlaces": 2, "isBase": true, "hasRate": true }, { "code": "INR", "name": "Indian Rupee", "symbol": "₹", "decimalPlaces": 2, "isBase": false, "hasRate": true } ],
    "rates": { "INR": 83.52, "EUR": 0.92 }
  }
}
```

---

## Analytics & Dashboard

### Platform analytics
`GET /api/v1/analytics/platform`

Platform-wide charts (call volume, revenue vs AI cost, plan mix, MRR by plan, top tenants, AI cost by provider). **Auth: JWT bearer. Super Admin only.**

Query: `days` (int, default 30, 7–90).

```json
{
  "success": true,
  "data": {
    "labels": ["Jul 9", "Jul 10"],
    "callVol": [120, 140],
    "revenue": [199.5, 199.5],
    "aiCost": [14.2, 15.8],
    "callsSeries": [ { "t": "Jul 9", "calls": 120 } ],
    "revVsCost": [ { "t": "Jul 9", "revenue": 199.5, "aiCost": 14.2 } ],
    "planMix": [ { "label": "Enterprise", "value": 2 }, { "label": "Growth", "value": 5 }, { "label": "Starter", "value": 5 } ],
    "mrrByPlan": [ { "label": "Growth", "value": 2495.0 } ],
    "topTenantsByCalls": [ { "label": "Acme Corp", "value": 1200 } ],
    "aiCostByProvider": [ { "label": "LLM", "value": 210 }, { "label": "STT", "value": 120 }, { "label": "TTS", "value": 90 }, { "label": "Telephony", "value": 60 } ]
  }
}
```

Note: `revenue` is a flat `MRR / 30` per-day figure and `planMix` only buckets the hardcoded `enterprise`/`growth`/`starter` codes (see Inconsistencies).

### Tenant analytics
`GET /api/v1/analytics/tenant`

Tenant dashboard: KPIs, call/containment series, sentiment/language mix, top intents, knowledge usage, cost series, data-driven recommendations. **Auth: JWT bearer.** Any authenticated role — tenant roles locked to their own tenant; Super Admin must pass `tenantId` (`400` otherwise).

Query: `days` (int, default 30, 7–90), `tenantId` (string — see above), `botId` (string, optional filter).

```json
{
  "success": true,
  "data": {
    "kpis": [ { "label": "Total calls", "value": "1,204", "delta": 8.2, "spark": [40, 44], "intent": "up-good" } ],
    "callsSeries": [ { "t": "Jul 9", "calls": 40, "contained": 31 } ],
    "containmentSeries": [ { "t": "Jul 9", "rate": 77.5 } ],
    "sentimentSplit": [ { "label": "Positive", "value": 46 }, { "label": "Neutral", "value": 41 }, { "label": "Negative", "value": 13 } ],
    "languageMix": [ { "label": "Hindi", "value": 52 } ],
    "topIntents": [ { "label": "payment_promise", "value": 214, "trend": 0 } ],
    "knowledgeUsage": [ { "label": "Collections FAQ", "value": 320 } ],
    "costSeries": [ { "t": "Jul 9", "llm": 2.4, "tts": 1.1, "stt": 0.9, "telephony": 1.6 } ],
    "recommendations": [ { "id": "rc-stale-ks_…", "title": "Re-sync stale knowledge: Collections FAQ", "detail": "…", "impact": "high", "link": "/t/knowledge" } ]
  }
}
```

KPI cards: Total calls, Containment rate, Escalations, Avg CSAT, AI cost, Avg cost / call.

---

## Audit

### List audit log
`GET /api/v1/audit`

Paginated audit trail, newest first. **Auth: JWT bearer.** Role: `tenant_admin` or `super_admin` — Super Admins see all entries; tenant admins only their own tenant's. `search` matches action, actor name, or target label; `sortBy`/`sortDir` accepted but not applied.

Query: `page`, `pageSize`, `search`, `sortBy`, `sortDir`, `entityType` (string, optional — e.g. `user`, `tenant`, `master:plans`, `export`, `report`, `invoice`).

```json
{
  "success": true,
  "data": [
    { "id": "aud_…", "actor": "Jane Admin", "actorRole": "super_admin", "action": "Updated tenant", "target": "Acme Corp", "tenant": "Acme Corp", "time": "2026-08-07T08:00:00Z", "ip": "10.0.0.5", "entityType": "tenant", "entityId": "<TENANT_ID>" }
  ],
  "meta": { "page": 1, "pageSize": 50, "total": 240, "totalPages": 5 }
}
```

---

## Exports & Reports

Both endpoints stream a file (`text/csv` or Excel `xlsx`) with headers `Content-Disposition: attachment; filename="…"`, `Content-Length` and `X-Content-Type-Options: nosniff`, and write an audit entry (`data.export` / `report.export`).

### Operational data export
`GET /api/v1/exports/{export_type}`

**Auth: JWT bearer.** Gate permission: `billing.manage` **or** `conversations.view`; per-type checks follow. `{export_type}` ∈:

| export_type | Access | Allowed filters | Rejected filters (`422`) |
|---|---|---|---|
| `subscriptions` | Super Admin **and** `billing.manage` (`403` otherwise) | `status` (`active`\|`past_due`\|`cancelled`\|`trial`), `plan` (existing plan code, `422` unknown), `search` | `tenantId`, `botId`, `sentiment`, `contained`, `flagged` |
| `invoices` | Super Admin **and** `billing.manage` | `status` (`paid`\|`open`\|`past_due`\|`void`), `search` | `plan`, `tenantId`, `botId`, `sentiment`, `contained`, `flagged` |
| `conversations` | `conversations.view` (`403` otherwise); tenant-scoped | `tenantId` (required for Super Admin → `422`; tenant roles: own tenant only → `403`), `botId` (must belong to the tenant → `404`), `sentiment` (`positive`\|`neutral`\|`negative`), `contained` (bool), `flagged` (bool), `search` | `plan`, `status` |

Query params (all optional unless stated): `format` (`csv`|`xlsx`, default `csv`, else `422`), `search` (≤200), `status` (≤30), `plan` (≤50), `tenantId` (≤40), `botId` (≤40), `sentiment` (≤20), `contained` (bool), `flagged` (bool).

Responses: `200` file stream; `404` `"Unsupported export type '…'."`; `422` bad format/filter; `403` scope violations.

Columns — subscriptions: subscription_id, tenant, plan_code, plan_name, status, seats, bot_limit, minutes_included, minutes_used (this month), usage_percent, mrr, currency, renewal_date, created_at. Invoices: invoice_id, tenant, period, amount, status, issued_at, created_at. Conversations: conversation_id, bot, channel, caller, started_at, duration_seconds, sentiment, intents, outcome, escalation_reason, csat, cost_usd, language, qa_score, flagged.

### Report export
`GET /api/v1/reports/{report_type}/export`

Aggregate, date-bounded reports built in the database. **Auth: JWT bearer.** Permission: `analytics.view`. `{report_type}` ∈:

| report_type | Access | Bot filter | Columns |
|---|---|---|---|
| `usage` | any `analytics.view` holder (tenant-scoped) | yes | date, calls, contained_calls, escalations, minutes, containment_rate, average_csat |
| `revenue` | **platform only** — Super Admin (`403` otherwise) | no (`422`) | date, plan_code, plan_name, currency, active_subscriptions, mrr, daily_revenue, mrr_inr, exchange_rate_inr |
| `ai_cost` | any `analytics.view` holder (tenant-scoped) | yes | date, llm_cost, tts_cost, stt_cost, embedding_cost, telephony_cost, ai_cost, total_cost, ai_cost_inr, total_cost_inr, exchange_rate_inr |

Query: `format` (`csv`|`xlsx`, default `csv`), `days` (int, default 30, 7–90), `tenantId` (≤40 — tenant roles: own tenant only, `403`; Super Admin: optional, omit for platform-wide), `botId` (≤40 — must exist (`404`) and belong to the tenant scope; passing it narrows scope to the bot's tenant).

Responses: `200` file stream (filename `echosphere-<report>-<start>-to-<end>.<ext>`); `404` unknown report type / tenant / bot; `422` bad format or unsupported bot filter; `403` platform-only report or cross-tenant.

---

## Integrations

Integration objects: `{ "id", "name", "category", "description", "status", "connectedAt" }` where `status` is the per-tenant state (`available` when never connected) and `connectedAt` is ISO or null.

### List integrations
`GET /api/v1/integrations`

Platform integration catalog merged with the tenant's connection state. **Auth: JWT bearer.** Any authenticated role; tenant-scoped. Query: `tenantId` (optional for tenant roles — own tenant only (`403`); **required** for Super Admin → `400`).

```json
{ "success": true, "data": [ { "id": "int_…", "name": "Salesforce", "category": "crm", "description": "…", "status": "connected", "connectedAt": "2026-06-10T12:00:00Z" } ] }
```

### Connect integration
`POST /api/v1/integrations/{integration_id}/connect`

Marks the integration connected for the caller's tenant (upserts the tenant-integration row) and stores optional config. **Auth: JWT bearer.** Role: `tenant_admin` (or `super_admin` — but see note). **Note:** the tenant is resolved from the caller's token with no `tenantId` parameter, so a Super Admin (no tenant) always gets `400` `"tenant_id is required for platform administrators."` — effectively tenant-admin-only (see Inconsistencies).

```json
{ "config": { "apiBase": "https://example.my.salesforce.com" } }
```

| Field | Type | Required | Description |
|---|---|---|---|
| config | object \| null | no | Stored as-is on the tenant integration when provided |

Response `200`: integration object with `status: "connected"` and `connectedAt` set. `404` unknown integration.

### Disconnect integration
`POST /api/v1/integrations/{integration_id}/disconnect`

Returns the tenant's integration to `available` and clears `connectedAt` (no-op if never connected; still audited). **Auth: JWT bearer.** Role: `tenant_admin` (same Super Admin `400` caveat as connect). No body.

Response `200`: integration object with `status: "available"`, `connectedAt: null`. `404` unknown integration.

---

## Notes & Known Inconsistencies

Documented behavior is as implemented; the following look like bugs or rough edges worth knowing:

1. **`GET /tenants` `plan` filter is post-pagination.** The plan filter runs in Python after the page is fetched, so `meta.total`/`totalPages` ignore it and a page can return fewer rows than `pageSize` while later pages still hold matches.
2. **`hard=true` never hard-deletes.** `DELETE /users/{id}` and `DELETE /tenants/{id}` call `guard_hard_delete()` (403 `"Permanent deletion is disabled…"` unless `ALLOW_HARD_DELETE=true`) but then perform a soft delete regardless — with hard deletes enabled the flag changes nothing.
3. **`GET /plans` (billing) includes archived/soft-deleted plans.** Unlike `/master/plans` and every other list, it has no `is_deleted`/status filter.
4. **Super Admins cannot use integration connect/disconnect.** Both resolve the tenant as `resolve_tenant_id(user, None)` and accept no `tenantId`, so a Super Admin always receives `400`, while `GET /integrations` does accept `tenantId`.
5. **Tenant members can list alerts but not acknowledge them.** `GET /alerts` is tenant-visible; `PATCH /alerts/{id}` is Super Admin only.
6. **Ignored sort params.** `/users`, `/tenants`, `/subscriptions`, `/invoices` and `/audit` accept `sortBy`/`sortDir` via the shared pagination dependency but never apply them (fixed orderings noted per endpoint). Only `/master/{mtype}` honors them.
7. **Master-data reads require manage permissions.** `GET /master/{mtype}` is gated by the same `manage_*` permission as writes — there is no read-only master permission.
8. **`/roles` and `/permissions` are open to every authenticated user**, including `tenant_user`, and `/roles` exposes each role's full permission-code list.
9. **Double timezone suffix in master audit timestamps.** `GET /master/{mtype}/{item_id}/audit` returns `created_at.isoformat() + "Z"`, yielding `…+00:00Z` for tz-aware values (the shared `iso()` helper elsewhere only appends `Z` for naive datetimes).
10. **`languages` status filter conflates `inactive` and `archived`.** For the `enabled`-based `languages` type, `status=archived` and `status=inactive` both filter `enabled = false`.
11. **`/analytics/platform` simplifications.** `revenue` is a constant `MRR/30` repeated per day (not historical), and `planMix` only counts the hardcoded plan codes `enterprise`, `growth`, `starter` — custom plan codes are silently omitted (though `mrrByPlan` covers all plans).
12. **`/invoices` `status` and `/usage/platform` `capability` filters are unvalidated** free strings (no 422 on typos — you just get an empty result), unlike the pattern-validated filters elsewhere.
13. **PATCH /users/me audit gap (minor).** The audit record's before/after snapshots cover name/phone/locale/timezone but not `avatarUrl`, which is still updatable.
