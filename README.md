# AUREXION EchoSphere — Enterprise VoiceBot Platform

Multi-tenant VoiceBot platform: React 18 + TypeScript frontend, FastAPI backend,
**MySQL** (structured/relational data via SQLAlchemy + Alembic) and **MongoDB**
(conversation transcripts / document data via Motor). The legacy voice-call
engine lives in `VoiceBot/` (FreeSWITCH-oriented STT/LLM/TTS pipeline, unchanged).

## Architecture

```
backend/                 FastAPI platform API (port 8000)
  config.py              Env-driven settings (.env)
  db/mysql.py            SQLAlchemy engine/session (MySQL)
  db/mongo.py            Motor client + index bootstrap (MongoDB)
  models/                39 tables: users, roles, permissions, tenants, plans,
                         subscriptions, invoices, voice_bots, voice_profiles,
                         languages, knowledge, prompts(+versions), intents,
                         entities, api_connections, workflows, channels,
                         scenarios, releases, conversation_sessions, alerts,
                         audit_logs, integrations, models, guardrails,
                         phone_numbers, sip_trunks, settings, usage_records…
  routers/               REST API (/api/v1/…) — auth, RBAC, tenant isolation
  seeds/                 base_seed (mandatory, idempotent) + demo_seed (opt-in)
  alembic/               migrations
src/                     Frontend; src/services/api.ts is the typed API client
                         (no mock data — everything is database-driven)
VoiceBot/                Existing voice engine (MongoDB voicebot_configs, Redis cache)
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
  setting changes; secrets are masked before storage.

## Setup

```bash
# 1. Python backend
python3 -m venv env
env/bin/pip install -r requirements.txt

# 2. Environment
cp .env.example .env            # then fill MYSQL_*, JWT_SECRET, SUPERADMIN_PASSWORD

# 3. MySQL — create the database and user (system MySQL, port 3306):
#    sudo mysql -e "CREATE DATABASE voice_bot CHARACTER SET utf8mb4;
#                   CREATE USER 'voicebot'@'localhost' IDENTIFIED BY '<password>';
#                   GRANT ALL PRIVILEGES ON voice_bot.* TO 'voicebot'@'localhost';"
#    (Dev fallback: a project-local MySQL datadir lives in .devdb/ on port 3307:
#     mysqld --datadir=$PWD/.devdb/mysql --port=3307 --socket=$PWD/.devdb/mysql.sock \
#            --mysqlx=OFF --bind-address=127.0.0.1 &)

# 4. MongoDB — a running mongod on MONGODB_URI is all that's needed.

# 5. Migrations + seed
env/bin/python -m backend.cli migrate        # alembic upgrade head
env/bin/python -m backend.cli seed           # idempotent base records
env/bin/python -m backend.cli seed --demo    # optional: dev demo dataset

# 6. Run
env/bin/uvicorn backend.main:app --port 8000 --reload   # API (docs at /api/docs)
npm install && npm run dev                                  # frontend → http://localhost:5199
```

## Sign-in (after demo seed)

| Role | Email | Password |
|------|-------|----------|
| Super Admin | `admin@aurexion.com` | value of `SUPERADMIN_PASSWORD` |
| Super Admin (demo) | `alex.rivera@aurexion.com` | `Demo@2026!` |
| Tenant Admin | `priya.sharma@meridianhealth.com` | `Demo@2026!` |
| Tenant User | `sam.ellery@meridianhealth.com` | `Demo@2026!` |

## Tests

```bash
env/bin/pytest VoiceBot/tests        # engine unit tests (unchanged)
npm run typecheck                      # frontend types
npm run build                          # typecheck + production bundle
```

## Backend gaps

Capabilities still without a backend (recording playback, voice sample
synthesis, scheduled publish, knowledge connector OAuth, CSV export jobs,
live call feed) remain behind feature flags in `src/services/flags.ts` —
see `TODO_BACKEND.md`.
