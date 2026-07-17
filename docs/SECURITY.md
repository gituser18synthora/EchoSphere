# Security Model

This document catalogs the concrete controls in the codebase. No formal compliance
certification (SOC 2, ISO 27001, HIPAA, …) is claimed.

## Authentication and authorization

- **JWT (HS256)** on the REST API and the MCP server; tokens are issued by
  `POST /api/v1/auth/login` and carry `sub`, `role`, `tenant_id`
  (`backend/core/security.py`). Secret: `JWT_SECRET` env var; expiry
  `ACCESS_TOKEN_EXPIRE_MINUTES` (default 720).
- **Roles**: `super_admin`, `tenant_admin`, `tenant_user`, enforced by dependencies
  in `backend/core/deps.py` (`require_tenant_admin`, `require_tenant_member`, …).
- **Tenant resolution is server-side only** — JWT claim, Redis session mapping, or
  phone-number mapping; never request bodies or MCP tool arguments. Cross-tenant
  access is always answered with a sanitized 404. Details:
  [MULTI_TENANCY.md](MULTI_TENANCY.md).
- The voice worker accepts only opaque session ids previously issued by the
  authenticated API or a verified telephony webhook, and re-checks that the session
  tenant matches the bot tenant (close 4403 on mismatch).

## Upload safety (`backend/knowledge/`)

- Extension whitelist (`ingestion/storage.py: ALLOWED_EXTENSIONS`).
- Magic-byte MIME sniffing (`service.py: sniff_mime`) — a `.pdf` that is not a PDF
  (or an office file without its container signature) is rejected with 400.
- Size cap `KNOWLEDGE_MAX_FILE_MB` (default 25) and empty-file rejection.
- **Path traversal safe by construction**: on-disk paths are built exclusively from
  server-generated ids under `storage/knowledge/{tenant}/{kb}/{doc}.{ext}`; every
  segment is validated against `^[A-Za-z0-9_-]{1,64}$` and resolved paths must stay
  inside the storage root (checked on save *and* on read).
- Per-KB sha256 dedupe prevents content-hash collisions from creating duplicate rows.

## Prompt-injection defenses (layered)

1. **At ingest**: `detect_prompt_injection` (`backend/knowledge/security.py`) flags
   suspicious patterns ("ignore previous instructions", role tags, DAN, …) per chunk;
   flags are stored in chunk meta.
2. **At retrieval**: `sanitize_for_context` strips role/tool markup and neutralizes
   code fences before chunks enter a prompt.
3. **At generation**: the grounded system prompt (built in
   `backend/voice_runtime/brain.py` and `bot_config.py`) instructs the model to
   answer only from the quoted context and to treat context as reference data,
   never as instructions.

## PII handling

`mask_pii` (`backend/knowledge/security.py`) supports card numbers, Aadhaar, PAN,
email, phone. Applied policy:

- Transcript turns are masked for card/aadhaar/PAN before hitting MongoDB
  (`SessionRecorder.finalize`).
- Caller phone numbers are masked before the MySQL `conversation_sessions` row is
  written (`caller_masked`).
- The `TurnRouter` SAFETY route intercepts callers reading out card numbers, OTPs or
  passwords: the bot deflects and the number is never processed as a normal turn.

## Webhook security

Telephony webhooks are signature-verified (Twilio HMAC-SHA1 scheme; generic
HMAC-SHA256 over `timestamp.body` with 300 s max skew) with constant-time compares
and Redis single-use replay protection (`backend/telephony/webhooks.py`). See
[TELEPHONY.md](TELEPHONY.md).

## Secret management

Secrets are configured **only as `env:` references** (`Settings.resolve_secret`,
`backend/config.py`): DB rows and configs store strings like `env:OPENAI_API_KEY`,
never raw values. Raw keys exist only in `.env`/process environment. Audit-log
writes mask secret values before storage; logs never print resolved secrets.

## Audit logging

MySQL `audit_logs` records logins, CRUD, publishes, permission/setting changes, and
specifically:

- knowledge document mutations: upload / retry / cancel / reindex / delete
  (`backend/routers/knowledge_documents.py`, actions `knowledge.document.*`);
- voice session issuance (`voice.session.create`);
- every MCP tool call (actions `mcp.*`, actor `mcp`) —
  `backend/mcp_server/server.py`.

## Error and information hygiene

- API errors use a uniform envelope (`backend/core/errors.py`); MCP errors are
  reduced to `{error: not_found | request_error | timeout | internal}` with no SQL,
  paths or stack traces.
- Missing vs. forbidden is indistinguishable to callers (404 either way).
- Retrieval results never include raw embeddings.
- Knowledge search logs record ids and counts, not document content.

## Operational limits

- MCP: 60 requests/min per tenant (Redis buckets), 20 s per-tool timeouts.
- Voice: worker concurrency cap, max call duration, session and idle timeouts
  ([VOICE_RUNTIME.md](VOICE_RUNTIME.md)).
- Hard deletes are refused while `ALLOW_HARD_DELETE=false` (soft delete + archive is
  the default everywhere).

## Known boundaries

- The generic webhook scheme and FreeSWITCH ESL rely on shared secrets in env vars —
  rotate them like any credential; there is no built-in rotation.
- Replay protection and MCP rate limiting fail open (with loud logs) if Redis is
  down, favoring availability.
- Prompt-injection detection is heuristic; it reduces, not eliminates, poisoning
  risk — hence the layered prompt rules.
- PII masking is regex-based and scoped to the classes listed above.
