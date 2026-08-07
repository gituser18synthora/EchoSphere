# Environment and ports

`shared/config.py::Settings` is the authoritative environment contract. The
project-root `.env` is loaded for local processes, while process-level
environment variables take precedence. This document lists names, purposes,
and code defaults only; never copy deployed values, tokens, passwords, or full
connection strings into documentation.

Related documents: [Local/deployment setup](DEPLOYMENT.md),
[architecture](ARCHITECTURE.md), [security](SECURITY.md),
[telephony](TELEPHONY.md), and the [API index](api/README.md).

## Current service ports

The checked-in code defaults and the current root `.env` port assignments agree:

| Service | Environment variable | Current port | Process / protocol |
| --- | --- | --- | --- |
| Frontend | `FRONTEND_PORT` | `5199` | Vite dev server; proxies `/api` to `API_PORT`. |
| Platform API | `API_PORT` | `9001` | `python -m backend.main`, HTTP/FastAPI. |
| Voice worker | `VOICE_WORKER_PORT` | `9002` | `python -m voice_runtime.app`, HTTP + WebSocket. |
| MCP server | `MCP_PORT` | `9003` | `python -m backend.mcp_server.server`, streamable HTTP + health. |
| FreeSWITCH ESL | `FREESWITCH_PORT` | `9004` | Event Socket control connection; **not HTTP**. |
| Telephony gateway | `TELEPHONY_GATEWAY_PORT` | `9011` | `python -m voice_runtime.gateway`, signed HTTP webhook + media WebSocket. |

`8000`, `8015`, and `8020` are not current EchoSphere service ports. `9004`
is current only as the FreeSWITCH Event Socket port; it must not be used as a
webhook or REST base URL.

## Application and frontend

| Variable | Code default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | Environment mode; production makes missing selected-provider credentials fatal and excludes mock providers. |
| `APP_NAME` | `EchoSphere` | Application display/logging name. |
| `API_HOST` | `0.0.0.0` | Platform API bind host. |
| `API_PORT` | `9001` | Platform API port. |
| `FRONTEND_PORT` | `5199` | Vite bind port. `vite.config.ts` loads the root environment without exposing non-`VITE_*` values to browser code. |
| `CORS_ORIGINS` | `http://localhost:5199` | Comma-separated allowed browser origins. |

No browser-side API secret is required. The frontend uses same-origin `/api`
requests in development and Vite proxies them to `API_PORT`.

## Authentication and seed controls

| Variable | Code default | Required / purpose |
| --- | --- | --- |
| `JWT_SECRET` | empty | Required by Platform API; sign/verify access tokens. Use a generated secret, never a documented literal. |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `720` | Access-token lifetime. |
| `ALLOW_HARD_DELETE` | `false` | Gate checked by `?hard=true`; current delete handlers still soft-delete. |
| `AUTO_RUN_MIGRATIONS` | `false` | Run application migrations automatically at startup when enabled. |
| `ENABLE_DATABASE_SEED` | `true` | Run idempotent seed logic. When true, Platform API requires `SUPERADMIN_PASSWORD`. |
| `SUPERADMIN_EMAIL` | code-owned bootstrap default | Initial super-admin email; set per deployment. |
| `SUPERADMIN_NAME` | `Platform Admin` | Initial super-admin display name. |
| `SUPERADMIN_PASSWORD` | empty | Required by Platform API while database seed is enabled. Never reuse or document the deployed value. |

## Databases and cache

All Platform/voice/worker processes validate MySQL, MongoDB, and Redis settings
at startup. Platform API and ingestion additionally validate PostgreSQL.

| Variable | Code default | Required / purpose |
| --- | --- | --- |
| `MYSQL_HOST` | `localhost` | Control-plane MySQL host. |
| `MYSQL_PORT` | `3306` | MySQL port. |
| `MYSQL_DATABASE` | `voice_bot` | Control-plane database name. |
| `MYSQL_USER` | empty | Required database user. |
| `MYSQL_PASSWORD` | empty | Required database password. |
| `MONGODB_URI` | `mongodb://localhost:27017` | Required transcript/event store connection string. Treat credentials in a URI as secret. |
| `MONGODB_DATABASE` | `voice_bot` | Mongo transcript/event database. |
| `REDIS_URL` | `redis://localhost:6379` | Required session, checkpoint, cache, and replay-protection store URL. |
| `POSTGRES_HOST` | `localhost` | Knowledge-plane PostgreSQL/pgvector host. |
| `POSTGRES_PORT` | `5432` | PostgreSQL port. |
| `POSTGRES_DATABASE` | `echosphere_knowledge` | Knowledge-plane database. |
| `POSTGRES_USER` | empty | Required by API and ingestion worker. |
| `POSTGRES_PASSWORD` | empty | Required by API and ingestion worker. |
| `POSTGRES_POOL_SIZE` | `10` | Base async PostgreSQL pool size. |
| `POSTGRES_MAX_OVERFLOW` | `20` | Extra pool connections above the base. |

Database schema changes are split by store: MySQL Alembic migrations under
`backend/alembic/`, PostgreSQL/pgvector migrations under `backend/alembic_pg/`,
and Mongo collections/indexes initialized by the shared database layer. See
[Deployment](DEPLOYMENT.md) and [pgvector](PGVECTOR.md) for commands.

## Vector index, ingestion, and retrieval

| Variable | Code default | Purpose |
| --- | --- | --- |
| `PGVECTOR_DISTANCE_METRIC` | `cosine` | Vector distance metric. |
| `PGVECTOR_HNSW_M` | `16` | HNSW connections per node. |
| `PGVECTOR_HNSW_EF_CONSTRUCTION` | `64` | HNSW build-time search width. |
| `PGVECTOR_HNSW_EF_SEARCH` | `100` | HNSW query-time search width. |
| `EMBEDDING_PROVIDER` | `openai` | Embedding adapter. `mock` is suitable only for development/tests. |
| `EMBEDDING_API_KEY_REFERENCE` | `env:OPENAI_API_KEY` | Secret reference, not a key value. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model. |
| `EMBEDDING_DIMENSION` | `1536` | Stored vector dimension; must match model/migration. |
| `EMBEDDING_BATCH_SIZE` | `64` | Ingestion embedding batch size. |
| `KNOWLEDGE_UPLOAD_DIR` | `storage/knowledge` | Uploaded source storage. |
| `KNOWLEDGE_MAX_FILE_MB` | `25` | Per-file limit. |
| `RETRIEVAL_TOP_K` | `6` | Final source count. |
| `RETRIEVAL_CANDIDATE_K` | `24` | Candidate pool before reranking/fusion. |
| `RETRIEVAL_RERANK_K` | `12` | Rerank candidate count. |
| `RETRIEVAL_MIN_SCORE` | `0.35` | Relevance floor. |
| `RETRIEVAL_FUSION_METHOD` | `weighted` | `weighted` normalized score sum or rank-only `rrf`. |
| `RETRIEVAL_SEMANTIC_WEIGHT` | `0.65` | Semantic weight in weighted fusion. |
| `RETRIEVAL_BM25_WEIGHT` | `0.35` | Keyword weight in weighted fusion. |
| `RETRIEVAL_BM25_SATURATION` | `1.0` | Saturation constant for PostgreSQL `ts_rank_cd`. |
| `RETRIEVAL_MIN_KEYWORD_RANK` | `0.02` | Exact-keyword relevance floor. |
| `RETRIEVAL_PHRASE_BOOST` | `0.1` | Fused score bonus for full-query phrase match. |
| `RETRIEVAL_HYBRID_VECTOR_WEIGHT` | `0.6` | Legacy-named semantic weight used by RRF mode. |
| `RETRIEVAL_HYBRID_KEYWORD_WEIGHT` | `0.4` | Legacy-named keyword weight used by RRF mode. |
| `RETRIEVAL_USE_RERANKER` | `false` | Enable the optional reranker. |
| `RETRIEVAL_TS_CONFIG` | `english` | PostgreSQL full-text configuration. |
| `ENABLE_OCR_FALLBACK` | `true` | OCR sparse PDF pages. |
| `OCR_MIN_PAGE_CHARS` | `120` | Text threshold below which OCR is considered. |
| `INGESTION_WORKER_POLL_SECONDS` | `2.0` | Durable job polling interval. |
| `INGESTION_MAX_ATTEMPTS` | `3` | Ingestion retry limit. |
| `INGESTION_WORKER_EMBEDDED` | `true` | Run the ingestion poller inside the API; disable when using dedicated workers. |

The selected embedding provider's referenced key is a startup warning in
development and a startup error in production if unresolved.

## Provider defaults

These are platform fallbacks. Governed tenant/bot selections stored in MySQL
override them.

| Variable | Code default | Purpose |
| --- | --- | --- |
| `STT_PROVIDER` | `sarvam` | Default streaming speech-to-text provider. |
| `STT_API_KEY_REFERENCE` | `env:SARVAM_API_KEY` | STT secret reference. |
| `STT_MODEL` | `saaras:v3` | Default governed STT model. |
| `TTS_PROVIDER` | `sarvam` | Default text-to-speech provider. |
| `TTS_API_KEY_REFERENCE` | `env:SARVAM_API_KEY` | TTS secret reference. |
| `TTS_MODEL` | `bulbul:v3` | Default governed streaming TTS model. |
| `TTS_VOICE` | `shubh` | Default Sarvam provider voice code. |
| `LLM_PROVIDER` | `openai` | Default conversation LLM provider. |
| `LLM_API_KEY_REFERENCE` | `env:OPENAI_API_KEY` | LLM secret reference. |
| `LLM_MODEL` | `gpt-4o-mini` | Default conversation/fast-orchestration model. |

Referenced secret variables commonly include `OPENAI_API_KEY`,
`SARVAM_API_KEY`, and provider-specific keys configured in `provider_defs`.
Only the reference name is stored in the database. `shared/config.py` trims
loader artifacts such as surrounding quotes and inline comments without ever
logging the secret value.

Provider adapter diagnostics also recognize `SARVAM_TTS_WS_URL` and
`ELEVENLABS_WS_BASE` as optional endpoint overrides. Leave them unset unless
testing a compatible proxy.

## Outbound API connections

| Variable | Code default | Purpose |
| --- | --- | --- |
| `API_CONNECT_ALLOW_PRIVATE` | `false` | Allow loopback/private targets. Keep false in production unless explicitly required. |
| `API_CONNECT_ALLOWED_HOSTS` | empty | Comma-separated hostname allowlist; empty permits any public host. |
| `API_CONNECT_MAX_RESPONSE_KB` | `64` | Maximum captured upstream response size. |

These settings back the SSRF guard used by API-connection tests and runtime
tools. API connection `secret://name` references resolve through an environment
variable derived from the name; raw credentials are rejected by the API.

## Voice runtime and recordings

| Variable | Code default | Purpose |
| --- | --- | --- |
| `VOICE_WORKER_HOST` | `0.0.0.0` | Worker bind host. |
| `VOICE_WORKER_PORT` | `9002` | Worker HTTP/WebSocket port. |
| `VOICE_WORKER_CONCURRENCY` | `20` | Maximum concurrent sessions admitted by the worker. |
| `VOICE_SESSION_TIMEOUT` | `900` | Inactive session timeout in seconds. |
| `MAX_CALL_DURATION` | `3600` | Hard call-duration limit in seconds. |
| `DEFAULT_SILENCE_TIMEOUT` | `12` | Default silence timeout in seconds. |
| `VOICE_CALL_RECORDING_ENABLED` | `true` | Write per-call stereo WAV recording. |
| `VOICE_RECORDINGS_DIR` | `storage/recordings` | Recording storage path. |
| `VOICE_CLONE_AUDIO_DIR` | `storage/voice_clones` | Uploaded/recorded clone-source audio path. |

Storage directories should be writable by the service user and backed up or
mounted according to the deployment's retention policy.

## Post-call intelligence

| Variable | Code default | Purpose |
| --- | --- | --- |
| `POST_CALL_WORKER_EMBEDDED` | `true` | Run the durable summary/outcome/NBA poller in voice worker/gateway. |
| `POST_CALL_POLL_SECONDS` | `3.0` | Poll interval. |
| `POST_CALL_MAX_ATTEMPTS` | `3` | Retry limit. |
| `POST_CALL_STALE_PROCESSING_SECONDS` | `600.0` | Reclaim age for orphaned processing jobs. |
| `POST_CALL_LLM_TIMEOUT_SECONDS` | `25.0` | Post-call analysis LLM timeout. |

## MCP server

| Variable | Code default | Purpose |
| --- | --- | --- |
| `MCP_ENABLED` | `true` | Enable MCP service behavior. |
| `MCP_HOST` | `0.0.0.0` | Bind host. |
| `MCP_PORT` | `9003` | Streamable HTTP/health port. |

MCP clients send a normal Platform API JWT as a bearer token; no independent
MCP password is defined.

## FreeSWITCH and telephony gateway

| Variable | Code default | Purpose |
| --- | --- | --- |
| `FREESWITCH_HOST` | `127.0.0.1` | ESL host. |
| `FREESWITCH_PORT` | `9004` | ESL TCP port, not HTTP. |
| `FREESWITCH_PASSWORD_REFERENCE` | `env:FREESWITCH_PASSWORD` | ESL password reference. |
| `FREESWITCH_CALLER_CHANNEL` | `auto` | Interleaved caller stream: `auto`, `first`/`left`, or `second`/`right`. |
| `FREESWITCH_INPUT_GAIN` | `12.0` | Upper bound for adaptive inbound audio gain. |
| `FREESWITCH_SEND_KILL_AUDIO` | `true` | Send `killAudio` to clear buffered bot speech on barge-in. |
| `TELEPHONY_WEBHOOK_SECRET_REFERENCE` | `env:TELEPHONY_WEBHOOK_SECRET` | HMAC secret reference. |
| `TELEPHONY_PUBLIC_WS_BASE` | empty | Public `ws://`/`wss://` base for provider media; empty derives it from the webhook request. |
| `TELEPHONY_GATEWAY_HOST` | `0.0.0.0` | Gateway bind host. |
| `TELEPHONY_GATEWAY_PORT` | `9011` | Gateway webhook/media port. |

The referenced variables `FREESWITCH_PASSWORD` and
`TELEPHONY_WEBHOOK_SECRET` contain actual secrets and must not be committed.
For short-lived FreeSWITCH channel debugging,
`ECHOSPHERE_FS_AUDIO_DEBUG_DIR` enables raw per-channel captures; never enable
it on production calls without an approved retention/access policy.

The Vaani simulator additionally accepts non-runtime convenience variables
`VAANI_SIM_BASE` and `VAANI_SIM_TO`; these affect only
`backend/scripts/vaani_dialer_sim.py`.

## Startup validation by service

| Service passed to `validate_settings` | Mandatory settings | Provider handling |
| --- | --- | --- |
| `api` | MySQL user/password, Mongo URI, Redis URL, JWT secret; super-admin password while seed enabled; PostgreSQL user/password | Missing selected embedding key warns in development and fails in production. |
| `voice-runtime` | MySQL user/password, Mongo URI, Redis URL | Missing default STT/TTS/LLM referenced key warns in development and fails in production. Per-bot overrides are resolved at call start. |
| `ingestion-worker` | MySQL user/password, Mongo URI, Redis URL, PostgreSQL user/password | Missing selected embedding key warns in development and fails in production. |

Tests can set `ECHOSPHERE_TEST_NULLPOOL` to disable normal pooling/embedded
worker startup. It is a test-only switch, not a production tuning variable.

## Safe local template

Use `.env.example` as the starting list and replace placeholders locally. A
documentation-safe pattern is:

```dotenv
API_PORT=9001
VOICE_WORKER_PORT=9002
MCP_PORT=9003
FREESWITCH_PORT=9004
TELEPHONY_GATEWAY_PORT=9011
FRONTEND_PORT=5199

JWT_SECRET=<GENERATED_JWT_SECRET>
MYSQL_USER=<MYSQL_USER>
MYSQL_PASSWORD=<MYSQL_PASSWORD>
POSTGRES_USER=<POSTGRES_USER>
POSTGRES_PASSWORD=<POSTGRES_PASSWORD>
SUPERADMIN_PASSWORD=<INITIAL_ADMIN_PASSWORD>
OPENAI_API_KEY=<OPENAI_API_KEY>
SARVAM_API_KEY=<SARVAM_API_KEY>
FREESWITCH_PASSWORD=<FREESWITCH_ESL_PASSWORD>
TELEPHONY_WEBHOOK_SECRET=<TELEPHONY_WEBHOOK_SECRET>
```

Do not commit the populated `.env`.
