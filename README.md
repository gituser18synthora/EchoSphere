# AUREXION EchoSphere — Enterprise VoiceBot Platform

Multi-tenant VoiceBot platform: React 18 + TypeScript frontend, FastAPI backend,
**MySQL** (control plane via SQLAlchemy + Alembic), **PostgreSQL + pgvector**
(knowledge plane: documents, chunks, embeddings), **MongoDB** (conversation
transcripts via Motor) and **Redis** (sessions, caches). The realtime voice engine
is built on Pipecat and lives in `backend/voice_runtime/`; the legacy `VoiceBot/`
folder has been removed — its adapters and design were ported into `backend/`
(see [docs/MIGRATION_FROM_VOICEBOT.md](docs/MIGRATION_FROM_VOICEBOT.md)).

## Architecture

```
backend/                 FastAPI platform API (port 8000)
  config.py              Env-driven settings (.env); secrets as env: references
  db/                    mysql.py, postgres.py (asyncpg+pgvector), mongo.py, redis.py
  models/                39+ MySQL tables: users, roles, permissions, tenants, plans,
                         subscriptions, invoices, voice_bots, voice_bot_settings
                         (incl. per-bot STT/TTS/LLM providers), voice_profiles,
                         languages, knowledge, prompts(+versions), intents,
                         entities, api_connections, workflows, channels,
                         scenarios, releases, conversation_sessions, alerts,
                         audit_logs, integrations, guardrails, phone_numbers,
                         sip_trunks, settings, usage_records…
  routers/               REST API (/api/v1/…) — auth, RBAC, tenant isolation
  knowledge/             Knowledge plane: ingestion, chunking, hybrid RAG retrieval
  voice_runtime/         Pipecat pipeline, ConversationBrain, session recording
  orchestration/         TurnRouter + LangGraph workflow engine
  providers/             Pluggable STT/TTS/LLM registry
  telephony/             Signed webhooks, provider media streams, FreeSWITCH ESL
  mcp_server/            Tenant-scoped MCP knowledge tools (port 8020)
  workers/ingestion.py   Background document-ingestion worker
  seeds/                 base_seed (mandatory, idempotent) + demo_seed (opt-in)
  alembic/ alembic_pg/   MySQL and PostgreSQL migrations
src/                     Frontend; src/services/api.ts is the typed API client
                         (no mock data — everything is database-driven)
docs/                    Architecture & operations documentation (see below)
```

- Conversation **metadata** lives in MySQL (`conversation_sessions`);
  **transcripts** (nested, variable per-turn documents) live in MongoDB
  (`conversation_transcripts`, unique on `session_id`, indexed on
  `tenant_id`/`bot_id`/`created_at`).
- Soft delete everywhere (`is_deleted`, `deleted_at`, `deleted_by`); hard
  deletes are blocked while `ALLOW_HARD_DELETE=false`.
- Every tenant-owned query is scoped by the authenticated user's `tenant_id`
  (JWT claim) — client-supplied tenant ids are honored only for super admins.
- Audit trail (`audit_logs`) records logins, CRUD, publishes, permission and
  setting changes, and MCP tool calls; secrets are masked before storage.

## Voice runtime & knowledge plane

- **Realtime calls** run in a dedicated worker (port 8015) on a Pipecat pipeline:
  Silero VAD → turn control → STT → ConversationBrain → TTS, with barge-in
  cancellation, per-bot provider selection and pinned published-config snapshots.
  Browser test calls use `ws://…:8015/ws/voice/{session_id}`; telephony media
  streams (Twilio/Telnyx/Plivo/Exotel/FreeSWITCH) use
  `/ws/telephony/{provider}/{session_id}`.
- **Knowledge/RAG**: documents upload through `/api/v1/knowledge/{id}/documents`,
  are ingested in the background (parse → chunk → embed → store → verify) into
  PostgreSQL + pgvector, and are retrieved with hybrid search (HNSW cosine +
  full-text, weighted RRF, confidence gate). The voice bot, the REST search-test
  endpoint and the MCP server all share one `KnowledgeService` with a single
  tenant-authorization choke point.
- **Workflows**: stateful multi-step flows (e.g. appointment booking) run on
  LangGraph with PostgreSQL checkpoints; audio never touches LangGraph.
- **MCP**: external agents can query tenant-scoped knowledge over streamable-HTTP
  MCP (port 8020) with platform JWTs, rate limiting and audit logging.

## Services

| Service | Port | Command |
|---|---|---|
| Platform API | 8000 | `env/bin/uvicorn backend.main:app --port 8000` |
| Voice worker | 8015 | `env/bin/uvicorn backend.voice_worker:app --port 8015` |
| Ingestion worker | — | `env/bin/python -m backend.workers.ingestion` |
| MCP server | 8020 | `env/bin/uvicorn backend.mcp_server.server:app --port 8020` |
| Frontend (dev) | 5199 | `npm run dev` (proxies `/api` → 8000) |

API docs: `/api/docs` · liveness: `/api/health` · readiness: `/api/health/ready`
(checks MySQL, PostgreSQL+pgvector, Redis, MongoDB).

## Setup

```bash
# 1. Python backend
python3 -m venv env
env/bin/pip install -r requirements.txt

# 2. Environment
cp .env.example .env            # fill MYSQL_*, POSTGRES_*, JWT_SECRET, SUPERADMIN_PASSWORD

# 3. MySQL — create the database and user (system MySQL, port 3306):
#    sudo mysql -e "CREATE DATABASE voice_bot CHARACTER SET utf8mb4;
#                   CREATE USER 'voicebot'@'localhost' IDENTIFIED BY '<password>';
#                   GRANT ALL PRIVILEGES ON voice_bot.* TO 'voicebot'@'localhost';"
#    (Dev fallback: a project-local MySQL datadir lives in .devdb/ on port 3307:
#     mysqld --datadir=$PWD/.devdb/mysql --port=3307 --socket=$PWD/.devdb/mysql.sock \
#            --mysqlx=OFF --bind-address=127.0.0.1 &)

# 4. PostgreSQL (knowledge plane) — role, database, pgvector extension:
#    sudo -u postgres psql -c "CREATE ROLE echosphere LOGIN PASSWORD '<password>';"
#    sudo -u postgres psql -c "CREATE DATABASE echosphere_knowledge OWNER echosphere;"
#    sudo -u postgres psql -d echosphere_knowledge -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 5. MongoDB + Redis — running services on MONGODB_URI / REDIS_URL are all that's needed.

# 6. Migrations + seed
env/bin/python -m backend.cli migrate        # MySQL: alembic upgrade head
env/bin/python -m backend.cli pg-migrate     # PostgreSQL knowledge plane
env/bin/python -m backend.cli seed           # idempotent base records
env/bin/python -m backend.cli seed --demo    # optional: dev demo dataset

# 7. Run (each in its own shell — see the Services table above)
env/bin/uvicorn backend.main:app --port 8000 --reload
env/bin/uvicorn backend.voice_worker:app --port 8015
env/bin/python -m backend.workers.ingestion
env/bin/uvicorn backend.mcp_server.server:app --port 8020   # optional
npm install && npm run dev                   # frontend → http://localhost:5199
```

Full walkthrough (including docker-compose sketch, smoke-test curls and
troubleshooting): [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Sign-in (after demo seed)

| Role | Email | Password |
|------|-------|----------|
| Super Admin | `admin@aurexion.com` | value of `SUPERADMIN_PASSWORD` |
| Super Admin (demo) | `alex.rivera@aurexion.com` | `Demo@2026!` |
| Tenant Admin | `priya.sharma@meridianhealth.com` | `Demo@2026!` |
| Tenant User | `sam.ellery@meridianhealth.com` | `Demo@2026!` |

## Tests

```bash
env/bin/python -m pytest -m "not perf" # backend suite (122 tests: unit + integration)
env/bin/python -m pytest backend/tests/perf -m perf -s   # perf measurements
npm run typecheck                      # frontend types
npm run build                          # typecheck + production bundle
```

Integration tests use the real local services with test-owned rows only and the mock
embedding provider — no external API keys required. See
[docs/TESTING.md](docs/TESTING.md).

## Documentation

| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | processes, datastores, component and call flow |
| [docs/VOICE_RUNTIME.md](docs/VOICE_RUNTIME.md) | Pipecat pipeline, brain routing, barge-in, providers |
| [docs/KNOWLEDGE_AND_RAG.md](docs/KNOWLEDGE_AND_RAG.md) | ingestion pipeline, hybrid retrieval, KB modes |
| [docs/PGVECTOR.md](docs/PGVECTOR.md) | knowledge-plane schema, indexes, migrations, perf |
| [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md) | MCP server, tools, auth, rate limits |
| [docs/TELEPHONY.md](docs/TELEPHONY.md) | webhooks, signatures, media streams, FreeSWITCH |
| [docs/WORKFLOWS.md](docs/WORKFLOWS.md) | LangGraph workflows and checkpointing |
| [docs/MULTI_TENANCY.md](docs/MULTI_TENANCY.md) | tenant resolution and isolation guarantees |
| [docs/SECURITY.md](docs/SECURITY.md) | auth, upload safety, PII, prompt injection, audit |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | local + docker-compose setup, smoke tests |
| [docs/TESTING.md](docs/TESTING.md) | suite layout, markers, perf numbers |
| [docs/MIGRATION_FROM_VOICEBOT.md](docs/MIGRATION_FROM_VOICEBOT.md) | what happened to `VoiceBot/` |

## Backend gaps

Capabilities still without a backend (recording playback, voice sample
synthesis, scheduled publish, knowledge connector OAuth, CSV export jobs,
live call feed) remain behind feature flags in `src/services/flags.ts` —
see `TODO_BACKEND.md`.
