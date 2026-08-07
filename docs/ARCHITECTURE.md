# EchoSphere Architecture

EchoSphere is a multi-tenant voice-bot platform. One repository produces six runnable
processes that share four datastores. The two first-class services are separate
top-level packages — `backend/` (control-plane API) and `voice_runtime/` (realtime
voice worker + telephony gateway) — with common code in `shared/`. Import direction is
strictly `backend → shared ← voice_runtime`; the services never import each other and
are started, scaled and restarted independently, while sharing one virtualenv (`env/`)
and one root `.env`.

The control plane (tenants, bots, users, config, provider catalog, usage/billing,
post-call memories) lives in MySQL; the knowledge plane (documents, chunks,
embeddings) lives in PostgreSQL with pgvector; conversation transcripts and voice
events live in MongoDB; live call/session state and caches live in Redis.

## Processes

| Process | Entry point | Port | Run command |
|---|---|---|---|
| Platform API | `backend/main.py` | `API_PORT` (9001) | `env/bin/python -m backend.main` |
| Voice worker | `voice_runtime/app.py` | `VOICE_WORKER_PORT` (9002) | `env/bin/python -m voice_runtime.app` |
| Telephony gateway | `voice_runtime/gateway.py` (same FastAPI app as the worker) | `TELEPHONY_GATEWAY_PORT` (9011) | `env/bin/python -m voice_runtime.gateway` |
| Ingestion worker | `backend/workers/ingestion.py` | — | `env/bin/python -m backend.workers.ingestion` |
| MCP server | `backend/mcp_server/server.py` | `MCP_PORT` (9003) | `env/bin/python -m backend.mcp_server.server` |
| Frontend (dev) | Vite (`vite.config.ts`) | `FRONTEND_PORT` (5199) | `npm run dev` (proxies `/api` → `API_PORT`) |

`./scripts/dev.sh` starts all six with per-service PID files, logs and health checks.

- API docs: `http://localhost:9001/api/docs`. Liveness: `GET /api/health`.
  Readiness: `GET /api/health/ready` — checks MySQL, PostgreSQL (+pgvector), Redis and
  MongoDB (`backend/main.py`). The ingestion worker also runs embedded in the API
  process by default (`INGESTION_WORKER_EMBEDDED`).
- The voice worker exposes `GET /` (identity), `GET /health`,
  `POST /telephony/webhook/{provider}` (signed dialer webhook that mints a session)
  and two WebSocket endpoints: `/ws/voice/{session_id}` (browser test client) and
  `/ws/telephony/{provider}/{session_id}` (freeswitch | twilio | telnyx | plivo |
  exotel | vaani).
- The **telephony gateway** is the same FastAPI app served on
  `TELEPHONY_GATEWAY_PORT` (9011): external dialers (Vaani) get exactly one public
  host:port for both the inbound-call webhook and the media WebSocket, while browser
  test sessions keep using the 9002 worker. Sessions live in Redis, so either
  process can host any call. FreeSWITCH call control (transfer/hangup) uses ESL on
  `FREESWITCH_HOST:FREESWITCH_PORT` (9004).
- The MCP server serves streamable-HTTP MCP at `/mcp` behind JWT bearer auth, plus
  `GET /health`.

## Component diagram

```
React frontend :5199 ──── REST /api/v1 (JWT) ────────────► Platform API :9001
      │                                                        │
      │ WS /ws/voice/{session_id}                              │ mints voice:session:{id}
      ▼                                                        ▼ (trusted tenant/bot map)
Voice worker :9002 ◄────────────────────────────────────── Redis
      ▲                                                        ▲
      │ WS /ws/telephony/{provider}/{session_id}               │ mints session
      │                                                        │
Dialer / carrier ── POST /telephony/webhook/{provider} ──► Telephony gateway :9011
(Vaani, FreeSWITCH,                                        (same app as the worker)
 Twilio, …)

MCP client / LLM agent ── /mcp (JWT) ──► MCP server :9003

Datastore access:
  Platform API      → MySQL, PostgreSQL+pgvector, Redis, MongoDB
  Voice worker/gw   → MySQL, PostgreSQL+pgvector, Redis, MongoDB
  Ingestion worker  → MySQL, PostgreSQL+pgvector
  MCP server        → MySQL, PostgreSQL+pgvector, Redis
```

## Data ownership

| Store | Owns | Access layer |
|---|---|---|
| MySQL (`voice_bot`) | Control-plane tables: users, roles, tenants, voice_bots, `voice_bot_settings` (per-bot `stt_settings`/`tts_settings`/`llm_settings` JSON incl. turn detection, goal policy and orchestration model), the DB-driven provider catalog (`provider_defs`, `provider_models`, `voice_profiles`, `supported_languages`), `knowledge_sources`, prompts, intents, workflows, channels, `phone_numbers`, `conversation_sessions`, `conversation_memories` (post-call summaries/NBA), `usage_events`, `provider_pricing`, `audit_logs`, … | sync SQLAlchemy, `shared/db/mysql.py`; Alembic `backend/alembic.ini` |
| PostgreSQL (`echosphere_knowledge`) | `knowledge_documents`, `knowledge_chunks` (Vector(1536) + HNSW + FTS GIN), `knowledge_ingestion_jobs`, LangGraph checkpoints | async SQLAlchemy + asyncpg, `shared/db/postgres.py`; Alembic `backend/alembic_pg.ini` (version table `alembic_version_pg`) |
| MongoDB | `conversation_transcripts` (per-turn route/kbSources/latencyMs, events, usage, recording metadata; PII-masked), `voice_events` | Motor, `shared/db/mongo.py` |
| Redis | `voice:session:{id}` trusted session mappings, `botcfg:*` published-config cache, webhook replay keys, MCP rate-limit buckets | `shared/db/redis.py` |

Migrations: `env/bin/python -m backend.cli migrate` (MySQL),
`env/bin/python -m backend.cli pg-migrate` (PostgreSQL). Seeds:
`env/bin/python -m backend.cli seed [--demo]`.

## Key subsystems

- **Voice runtime** (`voice_runtime/`) — Pipecat 1.5 pipeline per call:
  noise gate → Silero VAD → STT → transcript gate → turn control →
  `ConversationBrain` → streaming TTS router. See [VOICE_RUNTIME.md](VOICE_RUNTIME.md)
  and the endpoint reference in [api/VOICE_RUNTIME_API.md](api/VOICE_RUNTIME_API.md).
- **Knowledge plane** (`shared/knowledge/`) — upload validation, background ingestion
  (parse → chunk → embed → store → verify), hybrid retrieval (dense + keyword + fusion).
  See [KNOWLEDGE_AND_RAG.md](KNOWLEDGE_AND_RAG.md) and [PGVECTOR.md](PGVECTOR.md).
- **Providers** (`shared/providers/`) — pluggable STT/TTS/LLM registry
  (`shared/providers/factory.py`). Per-bot selection lives in `voice_bot_settings`
  JSON columns and is validated against the DB-driven catalog
  (`backend/core/provider_catalog.py`); credentials are stored as secret
  *references* and resolved server-side. See [VOICE_PROVIDERS.md](VOICE_PROVIDERS.md).
- **Orchestration** (`shared/orchestration/`) — decision-first Goal Engine
  (`goal_engine.py` + `decision_schema.py`): one bounded, structured LLM call per
  turn produces a validated `ConversationDecision` (intent, identity outcome, scope
  incl. injection attempts, slot observations, next action). Deterministic layers
  run around it: platform commands, hangup/DNC detection and deterministic fast
  paths resolve before the decision call; the legacy `TurnRouter` regex/scripted
  routing (`router.py`) is the fallback when the engine is disabled, times out
  (1.2 s budget) or fails. Stateful multi-step flows run on the LangGraph
  `WorkflowEngine` (`workflow_engine.py`, PostgreSQL checkpoints). See
  [WORKFLOWS.md](WORKFLOWS.md).
- **Post-call intelligence** (`shared/post_call/`) — durable background analysis of
  every completed call: summary, outcome, commitments and a Next Best Action,
  persisted to `conversation_memories` and optionally recalled into the customer's
  next call. Gated by tenant flags (fail-closed). See the memory flow below.
- **Telephony** — split along the service boundary: `backend/telephony/` and
  `shared/telephony_webhooks.py` verify signed inbound webhooks,
  `shared/telephony.py` holds the provider catalog and connect-instruction contract,
  and `voice_runtime/telephony.py` + `voice_runtime/freeswitch.py` own media
  serializers (FreeSWITCH, Vaani; other providers via Pipecat serializers) and ESL
  call control. See [TELEPHONY.md](TELEPHONY.md) and
  [VAANI_INTEGRATION.md](VAANI_INTEGRATION.md).
- **MCP server** (`backend/mcp_server/`) — four tenant-scoped knowledge tools for
  external agents. See [MCP_TOOLS.md](MCP_TOOLS.md).

## A voice call, end to end

```
Caller (browser)                          Caller (phone)
      │                                         │
      │ POST /api/v1/voice-sessions (JWT)       │ dialer/carrier → signed webhook
      ▼                                         ▼ (API telephony webhook, or
Platform API :9001                          gateway POST /telephony/webhook/{provider})
      │  writes voice:session:{id} → Redis (trusted tenant/bot mapping, TTL)
      │  returns opaque session id + ws path
      ▼
WS connect: /ws/voice/{session_id} (worker :9002) or
            /ws/telephony/{provider}/{session_id} (:9011)
      │  worker loads the Redis session, resolves the pinned published bot config,
      │  optionally loads the customer's previous-call memory (tenant flag gated)
      ▼
Per-call Pipecat pipeline (voice_runtime/pipeline.py):

  audio in ─► audio gate ─► Silero VAD ─► STT ─► transcript gate ─► turn controller
              (adaptive     (speech        │      (noise/foreign-    (segment-buffered
               energy vs     probability)  │       hallucination      turns, adaptive
               noise floor)                │       rejection)         endpointing,
                                           │                          word-confirmed
        per bot config: Sarvam streaming WS│                          barge-in)
        Deepgram Flux (v2/listen,          │                              │
        authoritative end-of-turn) or      │                              ▼
        segmented REST (OpenAI Whisper, …) │                    ConversationBrain
                                                                (voice_runtime/brain.py)
  1. deterministic first: hangup detection on EVERY segment, DNC/consent,
     platform commands, deterministic fast paths (no LLM latency)
  2. Goal Engine decision — one bounded structured LLM call (1.2 s default,
     per-bot `orchestration_timeout_seconds` clamped to 0.5–5.0 s);
     on failure/timeout falls back to deterministic TurnRouter routing
  3. guarded transitions: call policy / identity gate / slot capture,
     LangGraph workflow step, or KB retrieval for knowledge routes
  4. response LLM streams tokens ─► streaming TTS router (per-language engines,
     barge-in cancellation, transient-failure fallback engine)
                                           │
  audio out ◄── transport.output ◄─────────┘   (barge-in cancels retrieval, LLM
                                                stream and provider-side synthesis)

Call end ─► SessionRecorder.finalize():
  Mongo conversation_transcripts upsert + MySQL conversation_sessions row
  + stereo WAV recording finalized + post-call analysis enqueued (see below)
```

The security model that underpins every arrow above — server-side tenant resolution,
the single KB-authorization choke point, sanitized 404s — is described in
[MULTI_TENANCY.md](MULTI_TENANCY.md) and [SECURITY.md](SECURITY.md).

## Retrieval (RAG) path

There is exactly one retriever. Every knowledge flow goes through
`KnowledgeService` (`shared/knowledge/service.py`) — the single tenant-authorization
choke point — into the hybrid retriever
(`shared/knowledge/retrieval/retriever.py`, ported from KMRAG and re-keyed to
`tenant_id + kb_id` filtering):

```
query ─► normalize ─► dense (pgvector HNSW cosine) ┐
                    └► keyword (PostgreSQL FTS)    ┴► score fusion (weighted sum
                                                       or weighted RRF)
      ─► duplicate removal ─► relevance gate (vector similarity OR keyword rank)
      ─► optional rerank ─► context-window budgeting (~3000 tokens)
```

Tuning lives in `RETRIEVAL_*` settings (`shared/config.py`): `RETRIEVAL_TOP_K`,
`RETRIEVAL_CANDIDATE_K`, `RETRIEVAL_RERANK_K`, `RETRIEVAL_MIN_SCORE`,
`RETRIEVAL_FUSION_METHOD`, semantic/BM25 weights, phrase boost, minimum keyword
rank. Consumers — all sharing this one implementation, in-process (no MCP network
hop for the voice path):

- the voice bot's `ConversationBrain` (grounded `KNOWLEDGE` routes with numbered
  citations and sanitized context),
- the REST knowledge search/test endpoints (`backend/routers/knowledge.py`,
  `backend/routers/testing.py`),
- the four external-agent MCP knowledge tools
  (`backend/mcp_server/server.py`). There is no in-repository component named
  `AgentAssist`; KMRAG is ported source lineage, not a running service.

Details: [KNOWLEDGE_AND_RAG.md](KNOWLEDGE_AND_RAG.md).

## Post-call memory flow

```
call ends ─► SessionRecorder.finalize()
   │            └─ enqueue_post_call(): INSERT queued conversation_memories row
   │               (idempotent; ONLY if tenant.call_summary_enabled — fail-closed)
   ▼
background poller (shared/post_call/processor.py — embedded in the voice worker
and the telephony gateway; optimistic single-row claims, retry + stale reclaim)
   │
   ├─ analyzer.py: ONE bounded LLM call over transcript + recorded final state
   │    → validated PostCallAnalysis (summary, outcome, commitments, slots,
   │      proposed next action); deterministic fallback_analysis if the model fails
   ├─ nba.py: deterministic reconciliation — recorded platform facts (dispositions,
   │    verified tool outcomes, escalation, workflow position, dated commitments)
   │    outrank the LLM's proposal → final Next Best Action
   ▼
conversation_memories row updated (MySQL): summary + outcome + NBA + slots
   │
   ▼  next call from the same customer (tenant+bot scoped: runtime context →
      customer context → phone-tail fallback; never cross-tenant)
recall.load_previous_memory() — ONLY if tenant.use_previous_call_summary
(fail-closed) — injects the previous-call memory as greeting/continuation
context for the new call's ConversationBrain.
```

Both tenant switches (`call_summary_enabled`, `use_previous_call_summary`) live on
the tenant row, are Super Admin controlled, and are resolved server-side at the
moment they matter (`shared/post_call/tenant_flags.py`) — an unknown tenant or a
failed lookup behaves as "off".

## Repository layout (Python)

```
backend/                Control-plane platform API (owns auth, tenancy, config)
  main.py               API app factory + health/readiness + config validation
  routers/              REST API (/api/v1/*) — 30+ routers, ~189 operations:
                        auth, users, tenants, bots, channels, providers, catalog,
                        knowledge*, conversations, voice_clones, voice_sessions,
                        telephony, workflows, prompts, intents, testing, usage,
                        billing, analytics, audit, exports, reports, integrations,
                        apis, master_data, platform, customer/runtime context,
                        releases
  core/                 deps (JWT/RBAC), security, audit, responses, pagination,
                        safe_http (SSRF guard), softdelete, provider_catalog
  serializers.py        API response shaping
  telephony/            webhooks.py — signature verification + replay guard
  mcp_server/           Streamable-HTTP MCP server + JWT middleware
  workers/ingestion.py  Job-queue poller (FOR UPDATE SKIP LOCKED)
  seeds/                base_seed + demo_seed
  cli.py                migrate / pg-migrate / seed
  alembic/ alembic_pg/  MySQL and PostgreSQL migration environments

voice_runtime/          Realtime voice worker + telephony gateway
  app.py                Call host (webhook + WebSockets, Pipecat runner)
  gateway.py            Same app on TELEPHONY_GATEWAY_PORT for external dialers
  pipeline.py           Pipeline assembly: gate → VAD → STT → turns → brain → TTS
  brain.py              ConversationBrain (turn buffering, Goal Engine, RAG,
                        streaming, barge-in, hangup)
  audio_gate.py         Adaptive energy gate ahead of the VAD
  transcript_gate.py    Final-transcript quality gate (noise/hallucinations)
  endpointing.py        Finished-thought / short-reply endpoint classification
  barge_in.py           Word-confirmed barge-in turn-start strategy
  sarvam_stt.py         Sarvam streaming STT with honest finalization
  deepgram_stt.py       Deepgram Flux STT (authoritative end-of-turn)
  services.py           Segmented REST STT/TTS adapters over shared/providers
  tts_router.py         Streaming TTS router: per-language engines, fallback
  call_policy.py        Domain call-state policy (identity gate, commitments)
  turn_metrics.py       Per-turn end-to-end latency measurement
  serializer.py         Browser PCM + JSON side-channel wire protocol
  telephony.py          FreeSWITCH/Vaani media-stream serializers
  recording.py          SessionRecorder → Mongo transcript + MySQL row + WAV
  freeswitch.py         ESL call control (transfer / hangup)

shared/                 Imported by both services; never imports them
  config.py             Pydantic settings + validate_settings() fail-fast
  db/                   mysql.py, postgres.py, mongo.py, redis.py
  models/               MySQL ORM (control plane)
  knowledge/            models, service, ingestion/, parsing/, chunking/,
                        retrieval/, vector_store/, embeddings/, security.py
  orchestration/        goal_engine.py + decision_schema.py (decision-first),
                        router.py (deterministic fallback), workflow_engine.py
                        (LangGraph), intent_classifier, entity_extractor,
                        prompt_compiler, delivery, phrases, tool_executor
  post_call/            processor (durable queue), analyzer, nba, recall,
                        tenant_flags, schema
  providers/            stt/, tts/, llm/ + factory registry
  audio/                pcm.py, text.py
  turn_detection.py     Per-channel end-of-turn timing contract + validation
  voice_sessions.py     Redis session store (API-issued, worker-consumed)
  bot_config.py         Trusted bot-config resolution + cache invalidation
  telephony.py          Provider catalog + connect instructions
  telephony_webhooks.py Shared inbound-webhook verification/minting
  billing/ customer_context.py runtime_context.py tenant_languages.py
  errors.py ids.py      ApiError family + handlers, opaque prefixed IDs

tests/                  unit/, integration/, perf/ (spans all packages)
```

The legacy `VoiceBot/` folder was removed on this branch; its usable parts were ported
into `backend/`. See [MIGRATION_FROM_VOICEBOT.md](MIGRATION_FROM_VOICEBOT.md).
