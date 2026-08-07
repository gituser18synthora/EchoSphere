# EchoSphere API Reference

Generated from the live route tables and Pydantic schemas of the running
services. Treat the code as the source of truth; this reference is kept in
sync with it.

## Services and base URLs

| Service | Port (env var) | Base URL (local dev) |
| --- | --- | --- |
| Platform API (backend) | `9001` (`API_PORT`) | `http://localhost:9001` |
| Voice Worker (voice runtime) | `9002` (`VOICE_WORKER_PORT`) | `http://localhost:9002` |
| MCP Server | `9003` (`MCP_PORT`) | `http://localhost:9003` |
| Telephony / Vaani Gateway | `9011` (`TELEPHONY_GATEWAY_PORT`) | `http://localhost:9011` |
| Frontend (Vite dev server) | `5199` (`FRONTEND_PORT`) | `http://localhost:5199` |

`.env` at the repository root is the source of truth for ports. `FREESWITCH_PORT`
(default `9004`) is the FreeSWITCH Event Socket (ESL) port, not an HTTP service.

## Documents

### Platform API (backend, 189 REST operations)

- [Platform & Administration](BACKEND_PLATFORM_ADMIN.md) — authentication,
  users, roles/permissions, tenants, master data, platform configuration,
  plans/subscriptions/invoices, usage metering, analytics, audit, exports,
  reports, integrations.
- [Bots & Bot Configuration](BACKEND_BOTS.md) — bot CRUD, voice settings,
  channels (incl. WhatsApp webhook), prompts, intents, workflows, releases,
  runtime context, customer contexts, testing/scenarios, API connections,
  entities, templates, knowledge gaps.
- [Knowledge & RAG](BACKEND_KNOWLEDGE.md) — knowledge bases, document
  ingestion, retrieval testing, Super Admin chunk/document review.
- [Voice, Providers, Conversations & Telephony](BACKEND_VOICE_CONVERSATIONS.md) —
  provider catalog, models/voices, TTS preview, voice clones, voice sessions,
  conversations (transcripts, recordings), telephony webhooks, phone numbers,
  SIP trunks.

### Voice Runtime

- [Voice Runtime API](VOICE_RUNTIME_API.md) — HTTP endpoints, the browser
  voice WebSocket (`/ws/voice/{session_id}`), the telephony media-streaming
  WebSocket (`/ws/telephony/{provider}/{session_id}`), and the call lifecycle.

### MCP Server

- [MCP Tools](../MCP_TOOLS.md) — JWT-authenticated knowledge tools exposed
  over MCP on port 9003.

## Conventions

- **Authentication** — unless an endpoint is explicitly documented as public
  or webhook-verified, send `Authorization: Bearer <ACCESS_TOKEN>` obtained
  from `POST /api/v1/auth/login`.
- **Tenancy** — the tenant is derived from the authenticated user; it is never
  accepted from request bodies for tenant-scoped resources.
- **Placeholders** — examples use `<ACCESS_TOKEN>`, `<TENANT_ID>`, `<BOT_ID>`,
  `<CONVERSATION_ID>`, etc. Substitute real identifiers; never commit real
  secrets to documentation.

Related reading: [Architecture](../ARCHITECTURE.md) ·
[Voice Runtime overview](../VOICE_RUNTIME.md) ·
[Telephony](../TELEPHONY.md) · [Vaani integration](../VAANI_INTEGRATION.md) ·
[Environment variables](../ENVIRONMENT.md)
