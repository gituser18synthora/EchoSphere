# Deployment

Two supported paths: the non-Docker local setup used for development today, and a
docker-compose sketch. The Python virtualenv is `env/` at the repo root.

## 1. Non-Docker local setup

### 1.1 Prerequisites

Python 3.12+ (`python3 -m venv env && env/bin/pip install -r requirements.txt`),
Node 18+ (`npm install`), MySQL 8 (system service, port 3306), PostgreSQL with
pgvector (local dev is tested on PostgreSQL 18), Redis and MongoDB on default ports.

### 1.2 MySQL (control plane, db `voice_bot`)

```bash
sudo mysql -e "CREATE DATABASE voice_bot CHARACTER SET utf8mb4;
               CREATE USER 'voicebot'@'localhost' IDENTIFIED BY '<password>';
               GRANT ALL PRIVILEGES ON voice_bot.* TO 'voicebot'@'localhost';"
```

### 1.3 PostgreSQL (knowledge plane, db `echosphere_knowledge`)

```bash
sudo -u postgres psql -c "CREATE ROLE echosphere LOGIN PASSWORD '<password>';"
sudo -u postgres psql -c "CREATE DATABASE echosphere_knowledge OWNER echosphere;"
sudo -u postgres psql -d echosphere_knowledge -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

The migration also attempts `CREATE EXTENSION IF NOT EXISTS vector` / `pg_trgm`, so
the last command matters only when the app role is not superuser (the usual case).

### 1.4 Environment

```bash
cp .env.example .env
```

Minimum to fill in (`shared/config.py` documents every knob):

| Key | Notes |
|---|---|
| `JWT_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SUPERADMIN_PASSWORD` | first-login super admin (`admin@aurexion.com`) |
| `MYSQL_USER` / `MYSQL_PASSWORD` | from 1.2 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | from 1.3 |
| `EMBEDDING_PROVIDER` | `mock` for offline dev; `openai` + `OPENAI_API_KEY` for real embeddings |
| `STT/TTS/LLM_PROVIDER` | platform defaults; per-bot overrides live in `voice_bot_settings` |
| `TELEPHONY_WEBHOOK_SECRET` | required before any telephony webhook is accepted |
| `FREESWITCH_PASSWORD` | only for FreeSWITCH ESL call control |

Secrets are referenced as `env:VAR_NAME` (e.g. `STT_API_KEY_REFERENCE=env:OPENAI_API_KEY`)
and resolved at runtime — raw keys never go into DB rows.

**One `.env`, every process.** The API, voice worker, ingestion worker and MCP
server all read the same root `.env` (loaded by `shared/config.py`). Both
services validate configuration at startup (`validate_settings()` in
`shared/config.py`): missing mandatory variables (MySQL/MongoDB/Redis
credentials; `JWT_SECRET` and `SUPERADMIN_PASSWORD` for the API; PostgreSQL
credentials for the API/ingestion worker) abort startup with a message naming
each missing key. Optional provider API keys never block startup in
development — a warning is logged if the selected default provider has no
key — but in production (`APP_ENV=production`) a selected provider without a
resolvable key is a startup error.

### 1.5 Migrate and seed

```bash
env/bin/python -m backend.cli migrate       # MySQL (alembic upgrade head)
env/bin/python -m backend.cli pg-migrate    # PostgreSQL knowledge plane
env/bin/python -m backend.cli seed          # idempotent base records
env/bin/python -m backend.cli seed --demo   # optional demo dataset + logins
```

### 1.6 Run the services

```bash
env/bin/uvicorn backend.main:app --port 8000               # Platform API
env/bin/uvicorn voice_runtime.app:app --port 8015          # Voice runtime (WS)
env/bin/python -m backend.workers.ingestion                # Ingestion worker (optional; embedded in API by default)
env/bin/uvicorn backend.mcp_server.server:app --port 8020  # MCP server (optional)
npm run dev                                                # Frontend → http://localhost:5199
```

Verify: `curl -s localhost:8000/api/health/ready` — all four checks (`mysql`,
`postgres`, `redis`, `mongodb`) must report `ok: true`. Voice worker:
`curl -s localhost:8015/health`; MCP: `curl -s localhost:8020/health`.

### 1.7 Scaling voice workers

The voice runtime is stateless between calls — all trusted state lives in
Redis (`voice:session:{id}`, `botcfg:*`), MongoDB and MySQL — so capacity is
added by running more worker processes:

```bash
env/bin/uvicorn voice_runtime.app:app --port 8015
env/bin/uvicorn voice_runtime.app:app --port 8016
env/bin/uvicorn voice_runtime.app:app --port 8017
```

- Each process serves up to `VOICE_WORKER_CONCURRENCY` simultaneous calls
  (default 20) and closes new sockets with code 4429 above that.
- Put workers behind a WebSocket-capable load balancer (nginx `proxy_pass`
  with `Upgrade`/`Connection` headers, HAProxy, or a cloud LB) and point
  clients at it. Any worker can serve any session — the session id is looked
  up in shared Redis, so no sticky routing is required *for connection
  establishment*; a single call stays on the worker that accepted it for its
  lifetime.
- For browser test calls the API returns `VOICE_WORKER_PORT` to the client,
  so in a load-balanced setup set `VOICE_WORKER_PORT` to the balancer's port.
  For telephony, `public_ws_base` in the webhook connect instructions must
  point at the balancer.
- In-progress LangGraph workflows checkpoint to PostgreSQL, so a worker
  restart loses the audio socket but not workflow state; the caller can
  reconnect (new session) and resume the flow.
- Run one uvicorn per process (`--workers 1`, the default) — call state such
  as the active-session registry is per-process, and capacity accounting
  assumes it.

## 2. Smoke tests

### 2.1 Login and create a knowledge source

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"priya.sharma@meridianhealth.com","password":"Demo@2026!"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["token"])')

KB=$(curl -s -X POST localhost:8000/api/v1/knowledge \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Docs smoke test","type":"document","scope":"tenant"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["id"])')
```

### 2.2 Upload a document

```bash
curl -s -X POST "localhost:8000/api/v1/knowledge/$KB/documents" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/handbook.pdf"
# → {"success": true, "data": {"documentId": "...", "jobId": "...", "status": "pending"}}
```

Watch ingestion (requires the ingestion worker to be running):

```bash
curl -s "localhost:8000/api/v1/knowledge/documents/<documentId>/status" \
  -H "Authorization: Bearer $TOKEN"
# stage: parsing → chunking → embedding → storing → verifying; status → ready
```

### 2.3 Retrieval test

```bash
curl -s -X POST localhost:8000/api/v1/knowledge/search-test \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"query\":\"what is the refund policy\",\"kbIds\":[\"$KB\"],\"topK\":5}"
# → answerable/confidence + sources[] with page numbers and scores
```

### 2.4 MCP

See [MCP_TOOLS.md](MCP_TOOLS.md#client-configuration-example) for connecting an
MCP client to `http://localhost:8020/mcp` with the same bearer token.

### 2.5 Browser voice test

1. Sign in at `http://localhost:5199` (demo logins in the README).
2. Open **Studio** for a bot → **Testing** tab → switch to **Voice** mode.
3. Grant microphone access; the page creates a session via
   `POST /api/v1/voice-sessions` and connects to
   `ws://localhost:8015/ws/voice/{sessionId}`. Live transcripts and bot events
   render from the JSON side-channel; `mock` providers work without any keys.

## 3. docker-compose sketch

Reference topology (not exercised in CI) — adapt image tags and secrets. Note the
pgvector-enabled Postgres image.

```yaml
services:
  mysql:
    image: mysql:8.4
    environment:
      MYSQL_DATABASE: voice_bot
      MYSQL_USER: voicebot
      MYSQL_PASSWORD: ${MYSQL_PASSWORD}
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
    volumes: [mysql_data:/var/lib/mysql]
    healthcheck:
      { test: ["CMD", "mysqladmin", "ping", "-h", "localhost", "-p${MYSQL_ROOT_PASSWORD}"],
        interval: 10s, retries: 10 }

  postgres:
    image: pgvector/pgvector:pg17          # Postgres with pgvector preinstalled
    environment:
      POSTGRES_DB: echosphere_knowledge
      POSTGRES_USER: echosphere
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes: [pg_data:/var/lib/postgresql/data]
    healthcheck:
      { test: ["CMD-SHELL", "pg_isready -U echosphere -d echosphere_knowledge"],
        interval: 10s, retries: 10 }

  redis:
    image: redis:7
    volumes: [redis_data:/data]
    healthcheck: { test: ["CMD", "redis-cli", "ping"], interval: 10s, retries: 10 }

  mongo:
    image: mongo:7
    volumes: [mongo_data:/data/db]
    healthcheck:
      { test: ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"],
        interval: 10s, retries: 10 }

  api:
    build: .            # python:3.12 base; pip install -r requirements.txt
    command: uvicorn backend.main:app --host 0.0.0.0 --port 8000
    env_file: .env
    environment: &svc_env { MYSQL_HOST: mysql, POSTGRES_HOST: postgres,
      REDIS_URL: "redis://redis:6379", MONGODB_URI: "mongodb://mongo:27017" }
    ports: ["8000:8000"]
    volumes: [knowledge_files:/app/storage/knowledge]
    depends_on:
      { mysql: { condition: service_healthy }, postgres: { condition: service_healthy },
        redis: { condition: service_healthy }, mongo: { condition: service_healthy } }
    healthcheck:
      { test: ["CMD-SHELL", "curl -fs http://localhost:8000/api/health"],
        interval: 15s, retries: 5 }

  voice-worker:
    build: .
    command: uvicorn voice_runtime.app:app --host 0.0.0.0 --port 8015
    env_file: .env
    environment: *svc_env
    ports: ["8015:8015"]
    depends_on: [api]
    healthcheck:
      { test: ["CMD-SHELL", "curl -fs http://localhost:8015/health"],
        interval: 15s, retries: 5 }

  ingestion-worker:
    build: .
    command: python -m backend.workers.ingestion
    env_file: .env
    environment: *svc_env
    volumes: [knowledge_files:/app/storage/knowledge]   # shared with api
    depends_on: [api]

  mcp:
    build: .
    command: uvicorn backend.mcp_server.server:app --host 0.0.0.0 --port 8020
    env_file: .env
    environment: *svc_env
    ports: ["8020:8020"]
    depends_on: [api]
    healthcheck:
      { test: ["CMD-SHELL", "curl -fs http://localhost:8020/health"],
        interval: 15s, retries: 5 }

  frontend:
    image: node:20
    working_dir: /app
    command: sh -c "npm ci && npm run dev -- --host"
    volumes: [".:/app"]
    ports: ["5199:5199"]

volumes:
  { mysql_data: {}, pg_data: {}, redis_data: {}, mongo_data: {}, knowledge_files: {} }
```

Run migrations once the databases are healthy: `docker compose exec api python -m
backend.cli migrate` (then `pg-migrate` and `seed` the same way).

## 4. FreeSWITCH ESL notes

- Media: a dialplan attaches `mod_audio_fork` to
  `ws://<voice-worker>/ws/telephony/freeswitch/{session_id}` (raw L16 @ 8 kHz).
- Call control: enable `mod_event_socket` (default `127.0.0.1:8021`), set its
  password, configure `FREESWITCH_HOST`/`FREESWITCH_PORT` and
  `FREESWITCH_PASSWORD` (via `FREESWITCH_PASSWORD_REFERENCE=env:FREESWITCH_PASSWORD`).
- On separate hosts, change the event socket `listen-ip` from loopback and firewall
  port 8021 — ESL is plaintext. All ESL operations fail loudly when unconfigured
  (`voice_runtime/freeswitch.py`).

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| Port already in use / stale uvicorn | Kill by port, not by pattern: `fuser -k 8000/tcp` (a `pkill -f uvicorn` can match your own shell and kill it). Kill and restart in separate shell invocations. |
| Tests fail with cross-event-loop asyncpg errors | Export `ECHOSPHERE_TEST_NULLPOOL=1` (the test suite sets it in `tests/conftest.py`; needed when driving app code from ad-hoc scripts under multiple loops). |
| Alembic `ConfigParser` interpolation error | A `%` in a DB password must be escaped as `%%` in ini-style URLs. `backend/alembic_pg/env.py` already escapes the injected URL; do the same if you hand-edit `sqlalchemy.url`. |
| `/api/health/ready` shows `postgres.ok=false, error: pgvector extension not installed` | Run the `CREATE EXTENSION vector` command from 1.3 as superuser, then re-run `pg-migrate`. |
| Uploads accepted but never `ready` | The ingestion worker isn't running — start `python -m backend.workers.ingestion` and check the job `stage`/`error` via the status endpoint. |
| Voice WS closes 4404 for phone/SIP channels | Non-browser channels require a published release; publish the bot first. |
| Bot config changes not reflected in test calls | The `botcfg:*` Redis cache invalidates on settings save and release publish/rollback; already-active calls keep their pinned snapshot by design. |
