# Knowledge & RAG API

REST reference for the knowledge plane of the EchoSphere backend (`/api/v1`).
Source of truth: `backend/routers/knowledge.py`, `backend/routers/knowledge_documents.py`,
`backend/routers/knowledge_review.py` and the shared services under `shared/knowledge/`.

Architecture in one paragraph: knowledge-base **metadata** (the `knowledge_sources`
control plane) lives in MySQL; **documents, chunks and embeddings** live in
PostgreSQL with the pgvector extension (`knowledge_documents`, `knowledge_chunks`,
`ingestion_jobs` — see `shared/knowledge/models.py` and [PGVECTOR.md](../PGVECTOR.md)).
Chunk embeddings are stored in the `knowledge_chunks.embedding` pgvector column and
are **never** returned by any API below.

## Conventions

- **Auth**: every endpoint requires `Authorization: Bearer <ACCESS_TOKEN>` (a platform
  JWT from `POST /api/v1/auth/login`). Missing/expired tokens return `401`.
- **Envelope**: success responses are `{"success": true, "data": ...}`; paginated
  lists add `"meta": {"page", "pageSize", "total", "totalPages"}`. Errors are
  `{"success": false, "message": "...", "errors": [...]?}`.
- **Pagination params** (endpoints marked *paginated*): `page` (int, default 1, ≥1),
  `pageSize` (int, default 50, 1–200), `search` (string ≤200), `sortBy` (string ≤50,
  endpoint-specific whitelist), `sortDir` (`asc|desc`, default `desc`).
- **Roles**: `super_admin`, `tenant_admin`, `tenant_user`. "Tenant admin" endpoints
  accept `super_admin` or `tenant_admin`; "tenant member" endpoints accept any of the
  three. Permission-gated endpoints check permission codes on the user's role
  (`require_permission`) — a user needs **at least one** of the listed codes.
- **Tenant isolation**: tenant identity always comes from the JWT. Non-super-admin
  callers can only see their own tenant's sources (plus `global`-scoped ones);
  cross-tenant reads by id return `404`, not `403`.

## Document ingestion lifecycle

Uploading a file creates a `KnowledgeDocument` plus an `IngestionJob`; a background
worker (`shared/knowledge/ingestion/pipeline.py`) processes queued jobs.

- **Document `status`**: `pending` → `processing` → `ready` | `failed` | `cancelled`;
  `archived` after a delete/archive. (`failed`/`cancelled` documents can be retried;
  a retry resets the document to `pending` and queues a new job.)
- **Job `status`**: `queued` → `running` → `completed` | `failed` | `cancelled`.
- **Job `stage` / `progress`** while running: `parsing` → `chunking` (30%) →
  `embedding` (45%) → `storing` (75%) → `verifying` (90%) → `done` (100%).
- **Knowledge-source `status`** (control plane): created as `pending`; the first
  document upload flips it to `indexing`; the pipeline sets it to `indexed` on
  success or `failed` on error. `stale` marks a source that needs re-sync but is
  still searchable (retrieval considers `indexed` and `stale` searchable).

---

## Knowledge sources (control plane)

### List knowledge sources
`GET /api/v1/knowledge`

List knowledge bases visible to the caller. Super admins with no `tenantId`/`botId`
filter see all sources (platform view); everyone else sees their own tenant's
sources plus `global`-scoped ones. *Paginated* (`search` matches name/detail;
ordered by `created_at` ascending — `sortBy`/`sortDir` are not applied here).

- Auth: JWT bearer. Permission: none (any authenticated user).
- Query params:
  - `botId` (string, optional) — bot view: that bot's sources plus tenant/global shared ones.
  - `scope` (string, optional, enum `bot|tenant|global`).
  - `tenantId` (string, optional) — super admin only; others are pinned to their own tenant.
  - `status` (string, optional, enum `indexed|indexing|failed|pending|stale`).
  - `type` (string, optional, enum `document|url|faq|connector`).
  - Pagination params (see Conventions).

Response `200`:

```json
{
  "success": true,
  "data": [
    {
      "id": "<KB_ID>",
      "tenantId": "tn_...",
      "botId": "bot_...",
      "scope": "bot",
      "type": "document",
      "name": "Loan FAQs",
      "detail": "Product FAQ pack",
      "status": "indexed",
      "chunks": 128,
      "sizeKb": 2048,
      "lastSync": "2026-08-01T10:00:00Z",
      "quality": 92,
      "usage30d": 340,
      "createdAt": "2026-07-01T09:00:00Z",
      "updatedAt": "2026-08-01T10:00:00Z"
    }
  ],
  "meta": {"page": 1, "pageSize": 50, "total": 1, "totalPages": 1}
}
```

(`lastSync` is the string `"—"` when the source has never synced.)

### Create knowledge source
`POST /api/v1/knowledge`

Create a knowledge base (metadata only — upload documents separately).

- Auth: JWT bearer. Permission: `manage_knowledge` **or** `knowledge.manage`.
- Only super admins may create `scope: "global"` sources (`403` otherwise).
- `botId` is required when `scope` is `bot` (`422` otherwise); the bot must exist
  and belong to an accessible tenant.
- Name is trimmed and must be unique within the tenant, case-insensitive (`409` on duplicate).

```json
{
  "name": "Loan FAQs",
  "type": "document",
  "detail": "Product FAQ pack",
  "scope": "bot",
  "botId": "bot_...",
  "sizeKb": 0
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | 1–255 chars, trimmed; unique per tenant (case-insensitive). |
| `type` | string | yes | Enum `document\|url\|faq\|connector`. |
| `detail` | string | no | Default `""`; max 500 chars. |
| `scope` | string | no | Enum `bot\|tenant\|global`; default `bot`. `global` = super admin only. |
| `botId` | string | conditional | Required when `scope` is `bot`. |
| `tenantId` | string | no | Super admin only (required for them on non-global scopes); others pinned to own tenant. |
| `sizeKb` | integer | no | Default 0; ≥ 0. |

Response `201`: `{"success": true, "data": {<knowledge source object as above, "status": "pending">}}`

Errors: `403` (global scope without super admin; foreign tenant), `404` (bot not found),
`409` (duplicate name), `422` (missing name / missing `botId` for bot scope).

### Get knowledge base detail
`GET /api/v1/knowledge/{source_id}`

Full KB detail for the View action: the MySQL source row (with tenant/bot/creator
names) combined with live PostgreSQL document and chunk statistics.

- Auth: JWT bearer. Roles: tenant member (`super_admin`, `tenant_admin`, `tenant_user`).
- Path params: `source_id` — knowledge source id.
- `404` if the source doesn't exist, is deleted, or belongs to another tenant
  (global sources are visible to super admins only here).

Response `200`:

```json
{
  "success": true,
  "data": {
    "id": "<KB_ID>",
    "name": "Loan FAQs",
    "description": "Product FAQ pack",
    "type": "document",
    "scope": "bot",
    "status": "indexed",
    "tenantId": "tn_...",
    "tenantName": "Meridian Health",
    "botId": "bot_...",
    "botName": "Collections Bot",
    "chunks": 128,
    "sizeKb": 2048,
    "quality": 92,
    "usage30d": 340,
    "lastSync": "2026-08-01T10:00:00Z",
    "createdAt": "2026-07-01T09:00:00Z",
    "updatedAt": "2026-08-01T10:00:00Z",
    "createdBy": "Priya Sharma",
    "stats": {
      "documentCount": 4,
      "readyDocuments": 3,
      "failedDocuments": 1,
      "activeChunks": 120,
      "embeddedChunks": 120,
      "embeddingModels": ["text-embedding-3-small"],
      "lastError": "Parse failed: encrypted PDF"
    },
    "documents": [ { "documentId": "<DOCUMENT_ID>", "kbId": "<KB_ID>", "fileName": "faq.pdf", "status": "ready", "stage": null, "progress": 100.0, "attempts": 0, "failureReason": null, "chunkCount": 42, "pageCount": 10, "queuedAt": null, "startedAt": null, "finishedAt": null } ]
  }
}
```

`documents` is capped at the 50 most recent and reuses the document-status shape
(job stage is omitted — it is a snapshot, not the live progress tracker).

### Update knowledge source
`PATCH /api/v1/knowledge/{source_id}`

Rename, edit the description, set the status, or trigger a re-sync
(`resync: true` sets status to `indexing` and stamps `last_sync_at`).

- Auth: JWT bearer. Roles: tenant admin (`super_admin` or `tenant_admin`).
  Global-scoped sources: super admin only (`403`).
- Path params: `source_id`.

```json
{
  "name": "Loan FAQs v2",
  "detail": "Updated pack",
  "status": "indexed",
  "resync": false
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | no | Max 255 chars; empty string is ignored. |
| `detail` | string | no | Max 500 chars. |
| `status` | string | no | Enum `indexed\|indexing\|failed\|pending\|stale`. Setting `indexed` stamps `last_sync_at`. Ignored when `resync` is true. |
| `resync` | boolean | no | Default `false`; `true` forces status `indexing` + fresh `last_sync_at`. |

Response `200`: the updated knowledge source object (same shape as list). Audited.

### Archive knowledge source
`DELETE /api/v1/knowledge/{source_id}`

Soft-deletes (archives) the source; it disappears from listings and retrieval.

- Auth: JWT bearer. Roles: tenant admin. Global sources: super admin only.
- Path params: `source_id`.
- Query params: `hard` (boolean, default `false`) — hard deletes are blocked unless
  the deployment sets `ALLOW_HARD_DELETE`; even then the current implementation
  performs a soft delete after passing the guard.

Response `200`: `{"success": true, "data": {"archived": true, "id": "<KB_ID>"}}`

---

## Documents & ingestion

### Get upload constraints
`GET /api/v1/knowledge/upload-config`

Single source of truth for the frontend's file-picker: allowed extensions and the
size limit (`KNOWLEDGE_MAX_FILE_MB`, default 25).

- Auth: JWT bearer. Permission: none (any authenticated user). No parameters.

Response `200`:

```json
{
  "success": true,
  "data": {
    "allowedExtensions": ["csv", "doc", "docx", "json", "md", "markdown", "pdf", "ppt", "pptx", "txt", "xls", "xlsx"],
    "maxFileMb": 25,
    "accept": ".csv,.doc,.docx,.json,.md,.markdown,.pdf,.ppt,.pptx,.txt,.xls,.xlsx"
  }
}
```

### Upload a document
`POST /api/v1/knowledge/{source_id}/documents`

Multipart upload of one file into a KB. Stores the original on disk
(tenant/KB/document-scoped path), creates the `KnowledgeDocument` row (`pending`)
and queues an `IngestionJob` (`queued`); the KB's status flips to `indexing`.
Duplicate content (same SHA-256 within the KB) is detected and returns the existing
document with `duplicate: true` and an empty `jobId` instead of re-ingesting.

- Auth: JWT bearer. Permission: `upload_knowledge_documents` **or** `knowledge.manage`.
- Path params: `source_id` — target KB (must exist, be owned and not deleted;
  any status — uploads are allowed while other files are still indexing).
- Headers: `Content-Type: multipart/form-data`.
- Body: one form field `file` (required) — the file to ingest.

Accepted file types: `pdf`, `docx`, `doc`, `txt`, `md`, `markdown`, `csv`, `json`,
`xlsx`, `xls`, `pptx`, `ppt`. Max size: `KNOWLEDGE_MAX_FILE_MB` (default 25 MB).
The extension is whitelisted and re-validated against the sniffed MIME type.

```bash
curl -X POST "$BASE/api/v1/knowledge/<KB_ID>/documents" \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -F "file=@faq.pdf"
```

Response `201`:

```json
{
  "success": true,
  "data": {
    "documentId": "<DOCUMENT_ID>",
    "jobId": "kjob_...",
    "kbId": "<KB_ID>",
    "duplicate": false,
    "status": "pending"
  }
}
```

Errors: `400` (unsupported extension, content/extension MIME mismatch, empty file,
file exceeds the MB limit), `404` (KB not found / not owned).

### List documents in a KB
`GET /api/v1/knowledge/{source_id}/documents`

Ingestion status for every (non-deleted) document in the KB.

- Auth: JWT bearer. Roles: tenant member.
- Path params: `source_id`.

Response `200` — array of document-status objects:

```json
{
  "success": true,
  "data": [
    {
      "documentId": "<DOCUMENT_ID>",
      "kbId": "<KB_ID>",
      "fileName": "faq.pdf",
      "status": "processing",
      "stage": "embedding",
      "progress": 45.0,
      "attempts": 1,
      "failureReason": null,
      "chunkCount": 0,
      "pageCount": 10,
      "queuedAt": "2026-08-07T10:00:00+00:00",
      "startedAt": "2026-08-07T10:00:02+00:00",
      "finishedAt": null
    }
  ]
}
```

### Get document ingestion status
`GET /api/v1/knowledge/documents/{document_id}/status`

- Auth: JWT bearer. Roles: tenant member (super admins see any tenant's documents;
  others only their own — foreign ids return `404`).
- Path params: `document_id`.

Response `200`: one document-status object (same shape as the list above).

### Retry a failed/cancelled document
`POST /api/v1/knowledge/documents/{document_id}/retry`

Resets the document to `pending` and queues a fresh ingestion job.

- Auth: JWT bearer. Permission: `retry_knowledge_ingestion` **or** `knowledge.manage`.
- Path params: `document_id`. No body.
- Errors: `409` if the document is not `failed` or `cancelled`; `404` unknown/foreign id.

Response `200`: the refreshed document-status object. Audited.

### Cancel an in-flight ingestion
`POST /api/v1/knowledge/documents/{document_id}/cancel`

Cancels the active (`queued`/`running`) job; a `pending`/`processing` document
becomes `cancelled`.

- Auth: JWT bearer. Roles: tenant admin.
- Path params: `document_id`. No body.
- Errors: `409` "No active ingestion job to cancel"; `404` unknown/foreign id.

Response `200`: the refreshed document-status object. Audited.

### Re-index a document
`POST /api/v1/knowledge/documents/{document_id}/reindex`

Queues a new ingestion job for an already-ingested document (re-parse, re-chunk,
re-embed); the document goes back to `processing`.

- Auth: JWT bearer. Roles: tenant admin.
- Path params: `document_id`. No body.

Response `200`: the refreshed document-status object. Audited.

### Delete (archive) a document
`DELETE /api/v1/knowledge/documents/{document_id}`

Soft-deletes the document and archives its chunks — they stop surfacing in retrieval.

- Auth: JWT bearer. Roles: tenant admin.
- Path params: `document_id`. No body.

Response `200`: `{"success": true, "data": {"archived": true, "id": "<DOCUMENT_ID>"}}`. Audited.

---

## Retrieval testing (studio)

### Search test
`POST /api/v1/knowledge/search-test`

Runs the exact hybrid retrieval pipeline the voice bot uses, plus diagnostics and
below-threshold near-misses (test console only — runtime callers never receive
near-miss context). Scope: the caller's tenant (super admins search across all
global KBs unless they pass explicit `kbIds`).

- Auth: JWT bearer. Roles: tenant member.

```json
{
  "query": "What is the late payment fee?",
  "kbIds": ["<KB_ID>"],
  "botId": null,
  "topK": 6,
  "minScore": null
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | 1–2000 chars. |
| `kbIds` | string \| string[] \| null | no | One id, a list, or omitted = every authorized KB. Unknown/foreign/deleted ids → `404`. |
| `botId` | string | no | Restrict to a bot's sources plus tenant/global shared ones. |
| `topK` | integer | no | Default 6; 1–20. |
| `minScore` | number | no | 0–1. Test-console override of the answerability threshold (`RETRIEVAL_MIN_SCORE`); runtime is unchanged. |

Response `200`:

```json
{
  "success": true,
  "data": {
    "usedKnowledgeBase": true,
    "answerable": true,
    "confidence": 0.8123,
    "query": "What is the late payment fee?",
    "kbIds": ["<KB_ID>"],
    "durationMs": 142.5,
    "skippedReason": null,
    "diagnostics": {
      "kbCount": 1, "queryLength": 30, "embedder": "text-embedding-3-small",
      "embedError": null, "fusionMethod": "weighted", "semanticWeight": 0.65,
      "bm25Weight": 0.35, "minScore": 0.35, "minKeywordRank": 0.02,
      "denseCandidates": 24, "keywordCandidates": 9, "mergedCandidates": 27,
      "afterDedupe": 25, "afterGate": 6, "reranked": 0, "returned": 6,
      "timingsMs": {"embed": 55.1, "dense": 30.2, "keyword": 12.4},
      "zeroResultReason": null
    },
    "sources": [
      {
        "kbId": "<KB_ID>",
        "documentId": "<DOCUMENT_ID>",
        "chunkId": "<CHUNK_ID>",
        "chunkIndex": 12,
        "pageNumber": 3,
        "section": "Fees",
        "rank": 1,
        "score": 0.8123,
        "vectorScore": 0.79,
        "keywordScore": 0.31,
        "rerankScore": null,
        "passedGate": true,
        "text": "Late payment fee is ... (truncated to 800 chars)",
        "documentName": "faq.pdf",
        "meta": {}
      }
    ]
  }
}
```

Errors: `404` (explicit KB id not found/owned), `422` (query length, `topK`/`minScore` bounds).

---

## Super Admin — Knowledge Review APIs

All endpoints below live under `/api/v1/admin/knowledge/review` and are gated by a
single permission: **`review_knowledge_chunks`** (held only by Super Admin in the
base seed). Tenant isolation: a **super admin** (`tenant_id = None`) sees every
tenant and may narrow via `tenantId` where offered; **any other holder** of the
permission is pinned to their own tenant at the SQL level. Sensitive actions
(document view/download, retry, reindex, archive, chunk status/flag, retrieval
tests) are written to the audit log. Chunk embeddings are never selected from the
DB, let alone returned.

### List filter facets
`GET /api/v1/admin/knowledge/review/facets`

Distinct values for the review console's filter dropdowns, plus the static enums.

- Auth: JWT bearer. Permission: `review_knowledge_chunks`. No parameters.

Response `200`:

```json
{
  "success": true,
  "data": {
    "tenants": [{"id": "tn_...", "name": "Meridian Health", "code": "MH"}],
    "fileTypes": ["docx", "pdf", "txt"],
    "languages": ["en", "hi"],
    "uploadStatuses": ["pending", "processing", "ready", "failed", "cancelled", "archived"],
    "ingestionStatuses": ["queued", "running", "completed", "failed", "cancelled"],
    "chunkStatuses": ["active", "archived"]
  }
}
```

### List knowledge bases (filter dropdown)
`GET /api/v1/admin/knowledge/review/knowledge-bases`

- Auth: JWT bearer. Permission: `review_knowledge_chunks`. No parameters.

Response `200`: `{"success": true, "data": [{"id": "<KB_ID>", "name": "Loan FAQs", "tenantId": "tn_...", "scope": "bot", "status": "indexed", "chunks": 128}]}`

### List documents (review)
`GET /api/v1/admin/knowledge/review/documents`

Server-side-paginated document listing across tenants. *Paginated*; `search`
matches file names; `sortBy` one of `createdAt|fileName|sizeBytes|chunkCount|pageCount|status`.

- Auth: JWT bearer. Permission: `review_knowledge_chunks`.
- Query params:
  - `kbId` (string, optional) and/or `kbIds` (string, optional, comma-separated) — merged and de-duplicated.
  - `fileType` (string ≤16, optional) — file extension, e.g. `pdf`.
  - `status` (string ≤20, optional) — document upload/lifecycle status (`pending|processing|ready|failed|cancelled|archived`).
  - `ingestionStatus` (string ≤20, optional) — latest job status (`queued|running|completed|failed|cancelled`).
  - `language` (string ≤20, optional).
  - `uploadedFrom`, `uploadedTo` (ISO 8601 datetime, optional; `422` on bad format).
  - `failedOnly` (boolean, default `false`).
  - `includeArchived` (boolean, default `false`).
  - `tenantId` (string, optional) — honored for super admins only; others pinned.
  - Pagination params (see Conventions).

Response `200` — rows shaped as:

```json
{
  "success": true,
  "data": [
    {
      "documentId": "<DOCUMENT_ID>",
      "tenantId": "tn_...",
      "tenantName": "Meridian Health",
      "tenantCode": "MH",
      "kbId": "<KB_ID>",
      "kbName": "Loan FAQs",
      "fileName": "faq.pdf",
      "fileExt": "pdf",
      "fileType": "pdf",
      "mimeType": "application/pdf",
      "sizeBytes": 482133,
      "docType": "faq",
      "language": "en",
      "status": "ready",
      "uploadStatus": "stored",
      "ingestionStatus": "completed",
      "ingestionStage": "done",
      "ingestionProgress": 100.0,
      "attempts": 1,
      "failureReason": null,
      "pageCount": 10,
      "chunkCount": 42,
      "embeddingModel": "text-embedding-3-small",
      "embeddingDimension": 1536,
      "isDeleted": false,
      "uploadedBy": "usr_...",
      "uploadedByName": "Priya Sharma",
      "uploadedAt": "2026-08-01T10:00:00+00:00",
      "processingCompletedAt": "2026-08-01T10:01:12+00:00",
      "updatedAt": "2026-08-01T10:01:12+00:00"
    }
  ],
  "meta": {"page": 1, "pageSize": 50, "total": 1, "totalPages": 1}
}
```

(`uploadStatus` is derived: `archived` if deleted, else `stored`/`missing` by
presence of the original file on disk.)

### Get document detail (review)
`GET /api/v1/admin/knowledge/review/documents/{document_id}`

The document row above **plus** `quality` (chunk aggregates: `totalChunks`,
`activeChunks`, `archivedChunks`, `minTokens`, `maxTokens`, `avgTokens`,
`chunksMissingPage`, `chunksMissingSection`, `shortChunks`, `ocrChunks`,
`tableChunks`, `promptInjectionChunks`, `flaggedChunks`) and `hasOriginalFile`
(boolean). Viewing is an **audited** event.

- Auth: JWT bearer. Permission: `review_knowledge_chunks`.
- Path params: `document_id`. Errors: `404` unknown/foreign document.

### Download original file
`GET /api/v1/admin/knowledge/review/documents/{document_id}/download`

Streams the original uploaded file (`Content-Disposition` with the stored file
name; stored MIME type or `application/octet-stream`). Audited.

- Auth: JWT bearer. Permission: `review_knowledge_chunks`.
- Path params: `document_id`.
- Errors: `404` if the document is archived, has no stored file, or the file is
  missing from storage.

### Retry ingestion (review)
`POST /api/v1/admin/knowledge/review/documents/{document_id}/retry`

Same semantics as the tenant retry endpoint (`409` unless `failed`/`cancelled`),
executed in review scope. Audited. No body.

- Auth: JWT bearer. Permission: `review_knowledge_chunks`.

Response `200`: document-status object (`documentId`, `kbId`, `fileName`, `status`,
`stage`, `progress`, `attempts`, `failureReason`, `chunkCount`, `pageCount`,
`queuedAt`, `startedAt`, `finishedAt`).

### Re-index document (review)
`POST /api/v1/admin/knowledge/review/documents/{document_id}/reindex`

Queues a fresh ingestion job. Audited. No body.

- Auth: JWT bearer. Permission: `review_knowledge_chunks`.

Response `200`: document-status object (same shape as retry).

### Archive document (review)
`POST /api/v1/admin/knowledge/review/documents/{document_id}/archive`

Soft archive (recoverable): chunks stop surfacing in retrieval. A true hard delete
is intentionally not exposed. Audited. No body.

- Auth: JWT bearer. Permission: `review_knowledge_chunks`.

Response `200`: `{"success": true, "data": {"archived": true, "id": "<DOCUMENT_ID>"}}`

### List chunks (review)
`GET /api/v1/admin/knowledge/review/chunks`

Server-side-paginated chunk listing. *Paginated*; `search` matches chunk content;
`sortBy` one of `chunkIndex|createdAt|updatedAt|tokenCount|pageNumber`.

- Auth: JWT bearer. Permission: `review_knowledge_chunks`.
- Query params:
  - `documentId` (string, optional).
  - `kbId` / `kbIds` (string, optional; comma-separated list supported).
  - `status` (string ≤20, optional, `active|archived`).
  - `language` (string ≤20, optional).
  - `pageNumber` (integer ≥0, optional).
  - `section` (string ≤300, optional).
  - `createdFrom`, `createdTo` (ISO 8601 datetime, optional).
  - `minTokens`, `maxTokens` (integer ≥0, optional).
  - `hasKeywords`, `hasMetadata` (boolean, optional tri-state).
  - `flaggedOnly` (boolean, default `false`).
  - `includeArchived` (boolean, default `true`).
  - `tenantId` (string, optional) — super admins only.
  - Pagination params (see Conventions).

Response `200` — rows shaped as:

```json
{
  "success": true,
  "data": [
    {
      "chunkId": "<CHUNK_ID>",
      "documentId": "<DOCUMENT_ID>",
      "kbId": "<KB_ID>",
      "kbName": "Loan FAQs",
      "tenantId": "tn_...",
      "chunkIndex": 12,
      "pageNumber": 3,
      "section": "Fees",
      "topic": "late fees",
      "chunkType": "text",
      "language": "en",
      "keywords": ["fee", "late payment"],
      "tokenCount": 187,
      "charCount": 812,
      "status": "active",
      "contentPreview": "Late payment fee is ... (280 chars)",
      "content": "Late payment fee is ...",
      "hasMetadata": true,
      "embeddingModel": "text-embedding-3-small",
      "embeddingDimension": 1536,
      "embeddingGenerated": true,
      "createdAt": "2026-08-01T10:01:00+00:00",
      "updatedAt": "2026-08-01T10:01:00+00:00",
      "warnings": {
        "shortChunk": false, "emptyChunk": false, "missingPage": false,
        "missingSection": false, "ocr": false, "table": false,
        "fromImage": false, "promptInjection": false, "flaggedForReview": false
      }
    }
  ],
  "meta": {"page": 1, "pageSize": 50, "total": 1, "totalPages": 1}
}
```

### Get chunk detail (review)
`GET /api/v1/admin/knowledge/review/chunks/{chunk_id}`

The chunk row above **plus**: `metadata` (raw `meta` dict), `contentHash`,
`tenantName`, `fileName`, a `quality` object (the `warnings` flags merged with
`tokenCount`, `charCount`, `overlapWithPrevChars`, `duplicate`, `duplicateCount`,
`piiKinds`, `pii`, `promptInjectionPatterns`, `reviewFlag`), and boundary context
`prev` / `current` / `next` — each `null` or
`{"chunkId", "chunkIndex", "pageNumber", "section", "content", "status"}`.

- Auth: JWT bearer. Permission: `review_knowledge_chunks`.
- Path params: `chunk_id`. Errors: `404` unknown/foreign chunk.

### Set chunk status
`PATCH /api/v1/admin/knowledge/review/chunks/{chunk_id}/status`

Activate or archive a chunk (archived chunks are excluded from retrieval). Audited.

- Auth: JWT bearer. Permission: `review_knowledge_chunks`.

```json
{"status": "archived"}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `status` | string | yes | Enum `active\|archived`. |

Response `200`: `{"success": true, "data": {"chunkId": "<CHUNK_ID>", "status": "archived", "previousStatus": "active"}}`

### Flag / unflag a chunk
`POST /api/v1/admin/knowledge/review/chunks/{chunk_id}/flag`

Stores (or removes, when `flagged: false`) a review flag in the chunk's metadata. Audited.

- Auth: JWT bearer. Permission: `review_knowledge_chunks`.

```json
{"flagged": true, "reason": "Table extracted badly"}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `flagged` | boolean | no | Default `true`. `false` clears the flag. |
| `reason` | string | no | Max 500 chars; trimmed. |

Response `200`: `{"success": true, "data": {"chunkId": "<CHUNK_ID>", "flagged": true, "reason": "Table extracted badly"}}`

### Retrieval test (review)
`POST /api/v1/admin/knowledge/review/retrieval-test`

Runs retrieval in the **target KB's own tenant scope** so a super admin can test
any tenant's KB (a tenant-scoped reviewer is still restricted to their own tenant —
foreign KBs return `404`). Candidates below the answerability threshold are
included with `passedThreshold: false` for inspection. Audited.

- Auth: JWT bearer. Permission: `review_knowledge_chunks`.

```json
{
  "query": "What is the late payment fee?",
  "kbIds": ["<KB_ID>"],
  "documentId": null,
  "topK": 8,
  "minScore": null
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | 1–2000 chars. |
| `kbIds` | string[] | conditional | KBs to search. One of `kbIds`/`documentId` is required (`422` otherwise); all KBs must belong to one tenant (`422`). |
| `documentId` | string | conditional | Convenience: resolves to that document's KB when `kbIds` is omitted. |
| `topK` | integer | no | Default 8; 1–20. |
| `minScore` | number | no | 0–1; overrides the configured threshold (`RETRIEVAL_MIN_SCORE`) for the `passedThreshold` verdict. |

Response `200`:

```json
{
  "success": true,
  "data": {
    "query": "What is the late payment fee?",
    "kbIds": ["<KB_ID>"],
    "tenantId": "tn_...",
    "topK": 8,
    "threshold": 0.35,
    "confidence": 0.79,
    "answerable": true,
    "durationMs": 140.2,
    "results": [
      {
        "rank": 1,
        "chunkId": "<CHUNK_ID>",
        "documentId": "<DOCUMENT_ID>",
        "documentName": "faq.pdf",
        "kbId": "<KB_ID>",
        "pageNumber": 3,
        "section": "Fees",
        "score": 0.8123,
        "vectorScore": 0.79,
        "keywordScore": 0.31,
        "passedThreshold": true,
        "text": "Late payment fee is ... (truncated to 800 chars)"
      }
    ]
  }
}
```

---

## Retrieval pipeline configuration

`POST /knowledge/search-test` and `POST /admin/knowledge/review/retrieval-test`
run the same hybrid retriever the voice runtime uses
(`shared/knowledge/retrieval/retriever.py`). Its behavior is tuned via environment
variables (defaults in `shared/config.py` — names and purpose only):

| Env var | Purpose |
|---|---|
| `RETRIEVAL_TOP_K` | Number of chunks returned to the caller. |
| `RETRIEVAL_CANDIDATE_K` | Candidate pool fetched per retrieval leg before fusion. |
| `RETRIEVAL_RERANK_K` | Candidates passed to the reranker when enabled. |
| `RETRIEVAL_MIN_SCORE` | Answerability threshold — the relevance gate applied to fused scores. |
| `RETRIEVAL_FUSION_METHOD` | `weighted` (normalized weighted sum of semantic + BM25) or `rrf` (reciprocal-rank fusion). |
| `RETRIEVAL_SEMANTIC_WEIGHT` / `RETRIEVAL_BM25_WEIGHT` | Weights of the vector vs. keyword scores in `weighted` fusion. |
| `RETRIEVAL_BM25_SATURATION` | Saturates unbounded `ts_rank_cd` scores into (0,1) before fusing. |
| `RETRIEVAL_MIN_KEYWORD_RANK` | Raw keyword-rank floor above which a keyword hit counts as relevant on its own (protects exact terms/codes from the vector gate). |
| `RETRIEVAL_PHRASE_BOOST` | Fused-score bonus when a chunk contains the whole query as a phrase. |
| `RETRIEVAL_HYBRID_VECTOR_WEIGHT` / `RETRIEVAL_HYBRID_KEYWORD_WEIGHT` | RRF-mode weights (legacy names kept for existing `.env` files). |
| `RETRIEVAL_USE_RERANKER` | Enables the reranking stage. |
| `RETRIEVAL_TS_CONFIG` | PostgreSQL full-text search configuration used for the keyword leg. |

**Where embeddings live**: chunk vectors are stored in PostgreSQL via the
**pgvector** extension (`knowledge_chunks.embedding`, HNSW-indexed; see
`PGVECTOR_*` and `EMBEDDING_*` settings and [PGVECTOR.md](../PGVECTOR.md)).
Embeddings never leave the database through any of the APIs in this document.
