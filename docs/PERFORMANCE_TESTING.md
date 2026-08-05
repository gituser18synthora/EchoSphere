# EchoSphere Performance Testing Plan

| Field | Value |
|---|---|
| Project | AUREXION EchoSphere VoiceBot Platform |
| Document type | Performance test strategy and execution plan |
| Status | Draft baseline plan |
| Last updated | 2026-08-03 |
| Suggested pilot bot | `bot_8b3d28ab4ea0` |
| Owners | Engineering, QA, DevOps/SRE, Product |

## 1. Purpose

This document defines how EchoSphere performance will be measured, tested, and
accepted before a release. It covers the Platform API, realtime voice calls,
knowledge/RAG, document ingestion, MCP knowledge access, and the backing data
stores.

The plan is intended to answer five questions:

1. How quickly does the bot respond after the caller stops speaking?
2. How many simultaneous calls and API requests can one deployment support?
3. Does knowledge retrieval remain fast as the corpus and concurrency grow?
4. Does the system remain stable during spikes and long-running calls?
5. Does a release introduce a statistically meaningful regression?

This is a performance plan, not a functional test plan. Functional, security,
tenant-isolation, and provider-contract tests must pass before load testing begins.

## 2. System under test

EchoSphere has five runtime processes and four backing stores:

| Component | Default port | Performance responsibility |
|---|---:|---|
| Platform API | 9001 | Authentication, bot configuration, analytics, voice-session issuance, knowledge APIs |
| Voice worker | 9002 | WebSocket media, VAD, STT, routing, RAG, LLM, TTS and barge-in |
| Ingestion worker | — | Parse, chunk, embed, store and verify documents |
| MCP server | 9003 | Authenticated tenant-scoped knowledge tools |
| Vaani gateway | 9011 | Signed webhook and telephony WebSocket contract |
| MySQL | 3306/3307 | Control plane and usage rollups |
| PostgreSQL + pgvector | 5432 | Knowledge chunks, vector/keyword search and workflow checkpoints |
| Redis | 6379 | Voice sessions, configuration cache and rate-limit counters |
| MongoDB | 27017 | Conversation transcripts, per-turn latency and voice events |

The main voice critical path is:

```text
caller audio
  -> VAD / end-of-speech detection
  -> STT final transcript
  -> turn routing
  -> optional knowledge retrieval or workflow
  -> streaming LLM first token
  -> TTS first audio
  -> browser/carrier playback
```

The API and voice worker are independently scalable. One voice-worker process has
a default capacity of `VOICE_WORKER_CONCURRENCY=20`; a connection above that
capacity must be rejected predictably with WebSocket close code `4429`.

## 3. Scope

### 3.1 Included

- REST API response time, throughput and error rate.
- Voice-session creation and WebSocket connection time.
- End-of-speech to first playable bot audio latency.
- STT finalization, LLM time to first token, TTS time to first audio, and total turn latency.
- Barge-in cancellation and playback-clear latency.
- Browser and telephony voice channels.
- Direct LLM turns, knowledge/RAG turns, and workflow/tool turns.
- pgvector dense search, hybrid retrieval and concurrent retrieval.
- Document upload-to-ready time.
- MCP knowledge-search latency and rate-limit behavior.
- Resource saturation: CPU, memory, connection pools, event-loop lag and network.
- Baseline, load, stress, spike, endurance and recovery tests.
- Usage/transcript persistence after a call completes.

### 3.2 Excluded unless separately approved

- Denial-of-service and destructive chaos testing.
- Load against production customer tenants or production phone numbers.
- Unlimited paid-provider traffic.
- Carrier PSTN quality outside the EchoSphere/Vaani boundary.
- LLM answer quality; retrieval quality is evaluated by the separate retrieval suite.

## 4. Test principles and safety controls

- Use a dedicated performance tenant, bot, phone number and knowledge bases.
- Never use real customer PII. Test data must be synthetic and clearly prefixed.
- Run paid STT/LLM/TTS tests only with an approved request and cost budget.
- Record provider name, model, region and account tier with every result.
- Keep load generators on a different host from the system under test for formal runs.
- Synchronize clocks on the load generator, API, voice workers and gateways.
- Test cold-cache and warm-cache behavior separately; do not mix them in one percentile.
- Warm the system before recording a steady-state run.
- Stop a run if data integrity is at risk, error rate exceeds 10% for two minutes,
  CPU remains above 95%, or a backing store becomes unhealthy.

## 5. Metrics and measurement points

### 5.1 Standard definitions

| Metric | Definition |
|---|---|
| p50 | Median latency; 50% of samples are at or below this value |
| p95 | 95% of samples are at or below this value |
| p99 | Tail latency; used to expose intermittent pauses |
| Throughput | Successful operations or completed turns per second/minute |
| Error rate | Unexpected failed operations / total attempted operations × 100 |
| Saturation | Resource usage relative to its configured capacity |
| Regression | Candidate result is slower than the accepted baseline by the configured threshold |

Expected validation failures, authentication failures intentionally generated by a
negative test, MCP `429` responses above its documented rate limit, and the 21st
call rejected at a worker capacity of 20 are not counted as unexpected errors.

### 5.2 Voice timeline

The load harness should timestamp the following events with a monotonic clock:

| Mark | Event |
|---|---|
| T0 | Last non-silent caller audio sample sent/received |
| T1 | Accepted final user transcript reaches the conversation brain |
| T2 | First LLM token is received |
| T3 | First TTS PCM/audio chunk is generated |
| T4 | First bot-audio packet is sent to the browser or carrier |
| T5 | Barge-in speech begins |
| T6 | In-flight generation is cancelled and buffered playback is cleared |

Derived metrics:

| Metric | Formula | What it reveals |
|---|---|---|
| Endpointing/STT latency | T1 - T0 | VAD, `user_speech_timeout`, `finalize_grace` and STT finalization |
| LLM TTFT | T2 - T1 | Routing/retrieval plus provider first-token latency |
| TTS first-audio latency | T3 - first speakable text | TTS provider startup latency |
| Bot mouth-to-ear latency | T4 - T0 | Caller-visible first-response delay |
| Barge-in latency | T6 - T5 | How quickly the bot stops talking |

MongoDB transcript turns currently persist `retrieval`, `llm_first_token`, and
`total` values under `turns[].latencyMs`. The telephony simulator and client-side
harness must measure T0, T3/T4 and T5/T6 because all of those marks are not yet
persisted by the server.

## 6. Initial service-level objectives

These are proposed release targets, not claims about current production behavior.
After three reproducible runs on the production-like test environment, the team
should approve or adjust them and store the resulting baseline with the release.

### 6.1 API, RAG and MCP targets

| Area | Metric | Initial target |
|---|---|---:|
| API liveness `/api/health` | p95 | <= 150 ms |
| API readiness `/api/health/ready` | p95 | <= 500 ms |
| Authenticated read endpoints | p95 | <= 400 ms |
| Authenticated write endpoints | p95 | <= 700 ms |
| `POST /voice-sessions` | p95 | <= 300 ms |
| API steady-state unexpected error rate | 15-minute window | < 1% |
| pgvector dense search, warm, 5k chunks | p95 | <= 150 ms |
| Hybrid retrieval, warm, 5k chunks | p95 | <= 600 ms |
| `POST /knowledge/search-test` | p95 | <= 800 ms |
| Five-page PDF upload to `ready` | p95 | <= 5 s with mock/local embedding |
| MCP `search_knowledge` below rate limit | p95 | <= 800 ms |
| MCP rate limit | Contract | requests above 60/minute receive controlled `429` behavior |

### 6.2 Voice targets

| Metric | Initial target |
|---|---:|
| Voice-session creation p95 | <= 300 ms |
| WebSocket connection/accept p95 | <= 500 ms |
| Endpointing/STT latency (T1 - T0) p95 | <= 1.5 s |
| LLM first-token latency (T2 - T1) p95 | <= 3.0 s |
| TTS first-audio latency p95 | <= 1.0 s |
| End-of-speech to first outbound bot audio (T4 - T0) p95 | <= 4.5 s |
| End-of-speech to first outbound bot audio p99 | <= 6.0 s |
| Barge-in cancellation/clear (T6 - T5) p95 | <= 300 ms |
| Unexpected WebSocket disconnect rate | < 0.5% |
| Lost or malformed media-message rate | < 0.1% |
| One worker at configured capacity | 20 stable simultaneous calls by default |
| Above-capacity behavior | 21st call rejected with close code `4429`; existing calls unaffected |
| Post-call transcript/session persistence | >= 99.9% within 10 s of call end |

For the pilot bot `bot_8b3d28ab4ea0`, record its turn-detection values with the
result. At the time this plan was created they were `user_speech_timeout=0.70s`
and `finalize_grace=0.15s`. Changing either setting creates a new baseline.

### 6.3 Resource targets

| Resource | Steady-state target |
|---|---:|
| CPU | < 70% average; no sustained > 85% for five minutes |
| Process/container memory | < 80% limit and no monotonic growth > 10% during soak |
| MySQL/PostgreSQL pool usage | < 80% sustained |
| Event-loop lag | p95 < 100 ms |
| Redis command latency | p95 < 10 ms on the test network |
| Disk | No sustained queue/saturation; transcript and log writes remain non-blocking |

## 7. Test data and workload model

### 7.1 Data volumes

Prepare three repeatable datasets:

| Dataset | Tenants | Bots | Knowledge chunks | Purpose |
|---|---:|---:|---:|---|
| Small | 1 | 1 | 5,000 | Fast baseline and comparison with the existing suite |
| Medium | 10 | 50 | 50,000 per large KB | Target-load test |
| Large | 25 | 250 | 250,000+ total | Search/index stress and tenant-filter validation |

Use short, medium and long prompts; English and Hindi audio; knowledge hits and
misses; and repeated queries for cache-warm measurements. Keep stable fixture IDs
or a documented data-generation seed so two releases are comparable.

### 7.2 Production-like voice mix

Unless real traffic data gives a better distribution, start with:

| Turn type | Share |
|---|---:|
| Direct conversation/no knowledge | 50% |
| Knowledge/RAG question | 30% |
| Workflow or external tool/API | 10% |
| Call control, silence, invalid/noisy audio and barge-in | 10% |

Use calls lasting 3-5 minutes for load tests and a smaller number of 15-minute
calls for endurance. Space caller turns realistically instead of continuously
sending audio, because provider streaming and silence handling are part of the
workload.

### 7.3 Load stages

| Stage | API load | Voice load per worker | Duration | Goal |
|---|---:|---:|---:|---|
| Smoke | 1 virtual user | 1 call | 5 min | Validate scripts and measurements |
| Baseline | 1-5 virtual users | 1 call | 10 min | Establish unloaded latency |
| Normal load | 25 virtual users | 5, then 10 calls | 15 min each | Confirm normal operating headroom |
| Target load | 50 virtual users | 15, then 20 calls | 30 min | Validate configured capacity |
| Stress | Increase to 100-200 | Increase until rejection/degradation | 5-min steps | Find the bottleneck and breaking point |
| Spike | 0 to target in 30 s | 0 to 20 calls in 30 s | 15 min | Validate burst handling and recovery |
| Soak | 25 virtual users | 10-15 calls | 2-4 h | Detect leaks, pool exhaustion and latency drift |

Scale API load to the expected business traffic if production forecasts are
available. Do not assume one API virtual user equals one active caller.

## 8. Test scenarios

### 8.1 REST API

| ID | Scenario | Measurement |
|---|---|---|
| API-01 | Login once, then call `/auth/me` and list bots | p50/p95/p99, RPS, errors |
| API-02 | Read bot and voice settings | Cache behavior and MySQL latency |
| API-03 | Create browser voice sessions for authorized bots | API + Redis issuance latency |
| API-04 | Tenant analytics and usage summary | Aggregation latency under data volume |
| API-05 | Mixed 80% reads / 20% writes using test-owned rows | Throughput and lock contention |
| API-06 | Readiness while stores are under load | Dependency latency and correctness |

Authentication should normally be performed once per virtual-user session. A
separate login-specific test may measure password-hash capacity; it must not
dominate the general API workload.

### 8.2 Knowledge and ingestion

| ID | Scenario | Measurement |
|---|---|---|
| KB-01 | 100 warm dense searches against 5,000 chunks | p50/p95 and result presence |
| KB-02 | Hybrid retrieval with hit, miss and below-threshold queries | p50/p95/p99 and correctness |
| KB-03 | 20, then 50 concurrent searches | Pool saturation and tail latency |
| KB-04 | Upload and ingest 5-, 25- and 100-page PDFs | Upload-to-ready time and chunks/s |
| KB-05 | Search while ingestion is writing | Query latency degradation and errors |
| KB-06 | Multi-tenant concurrent search | Latency plus zero cross-tenant results |

### 8.3 Voice and telephony

| ID | Scenario | Measurement |
|---|---|---|
| VOICE-01 | Browser call, direct answer | T0-T4 and disconnects |
| VOICE-02 | Browser call, knowledge answer | Retrieval, LLM TTFT and T0-T4 |
| VOICE-03 | Telephony/Vaani full call | Webhook, WebSocket, media and T0-T4 |
| VOICE-04 | Barge in while the bot is producing long audio | T5-T6 and stale audio after clear |
| VOICE-05 | Silence, short replies and noisy audio | False endpoints and timeout behavior |
| VOICE-06 | 1/5/10/15/20 simultaneous calls | Percentiles and worker resources |
| VOICE-07 | Attempt call 21 at default worker capacity | Controlled 4429; no impact on active calls |
| VOICE-08 | 15-minute call with repeated turns | Memory growth, history cap and stability |
| VOICE-09 | Hindi/English and language switching | Latency split by language/provider |
| VOICE-10 | Abrupt carrier disconnect | Fast cleanup and correct persistence |

Run mock providers first to isolate application overhead. Run real providers as a
separate, labelled test because network region, provider queues, model and account
tier materially affect the result.

### 8.4 MCP

| ID | Scenario | Measurement |
|---|---|---|
| MCP-01 | `list_authorized_knowledge_bases` | p50/p95 and auth overhead |
| MCP-02 | `search_knowledge` at <= 1 request/s per token | p50/p95/p99 and retrieval latency |
| MCP-03 | Exceed 60 requests/minute for one token | Controlled rate-limit behavior |
| MCP-04 | Parallel requests from multiple tenants/tokens | Throughput, fairness and isolation |

## 9. Environment and entry criteria

Record this information before every formal run:

- Git commit SHA and whether the worktree contains uncommitted changes.
- Environment name and date/time.
- Host/container CPU, memory and operating-system limits.
- Process count and `VOICE_WORKER_CONCURRENCY` per worker.
- MySQL and PostgreSQL pool sizes; pgvector index and corpus size.
- Redis and MongoDB topology.
- STT, LLM, TTS and embedding providers/models/regions.
- Bot version, language, voice settings and turn-detection settings.
- Load-generator version, host and network path.
- Dataset version/seed and cache state.

Entry criteria:

1. Migrations and seeds are complete.
2. Unit and integration tests pass.
3. All health/readiness endpoints are healthy.
4. The performance tenant and synthetic test data exist.
5. Logs, transcript access and system-resource monitoring are available.
6. Paid-provider budget and rate limits are confirmed.
7. No unrelated deployment or ingestion job is running in the environment.

## 10. Execution procedure

### 10.1 Pre-run health check

```bash
curl -fsS http://127.0.0.1:9001/api/health
curl -fsS http://127.0.0.1:9001/api/health/ready
curl -fsS http://127.0.0.1:9002/health
curl -fsS http://127.0.0.1:9003/health
curl -fsS http://127.0.0.1:9011/health
```

If a service is intentionally excluded, record that fact instead of silently
ignoring a failed check.

### 10.2 Existing component performance suite

```bash
env/bin/python -m pytest tests/perf -m perf -s
```

This suite currently measures:

- 5,000-chunk pgvector seed throughput.
- Warm dense-search p50/p95.
- Hybrid retrieval latency.
- 20 concurrent dense searches.
- Five-page PDF upload-to-ready time.
- Mock-provider transcription-to-first-TTS-audio latency.

The assertions are generous sanity ceilings. Formal release acceptance must compare
the printed measurements with the targets and previous accepted baseline; a green
pytest result by itself is not a performance approval.

### 10.3 Telephony scenarios

Use the existing Vaani simulator for contract-level and single-call timing:

```bash
env/bin/python backend/scripts/vaani_dialer_sim.py webhook --bot bot_8b3d28ab4ea0
env/bin/python backend/scripts/vaani_dialer_sim.py full-call --bot bot_8b3d28ab4ea0 --say "Meri payment ki due date kya hai?"
env/bin/python backend/scripts/vaani_dialer_sim.py barge-in --bot bot_8b3d28ab4ea0
env/bin/python backend/scripts/vaani_dialer_sim.py abrupt-disconnect --bot bot_8b3d28ab4ea0
```

The simulator reads secrets from environment configuration and must not print or
hard-code them. Use an approved WAV corpus for repeatable formal measurements;
synthetic audio is useful for transport/VAD checks but is not a substitute for real
speech in STT latency testing.

### 10.4 Load generation

Use k6, Locust, or an equivalent version-controlled harness for REST and WebSocket
load. The harness must:

- Reuse authentication tokens during the general workload.
- Create unique session IDs and test-owned rows.
- Pace PCM/media frames at the real sample rate instead of sending a whole file instantly.
- Timestamp T0-T6 with a monotonic clock.
- Export raw samples and a summarized p50/p95/p99 report.
- Tag results by endpoint, scenario, provider, model, bot, language and load stage.
- Treat configured 429/4429 responses separately from unexpected failures.
- Clean up only data created by that run.

Do not start a formal concurrency run until the smoke scenario succeeds with one
virtual user and one voice call.

### 10.5 Run order

1. Capture configuration and idle resource usage.
2. Run functional smoke tests.
3. Run one cold-cache sample; label it separately.
4. Warm the application for five minutes.
5. Run the baseline stage three times.
6. Run normal and target load stages.
7. Run stress and spike tests only after target load passes.
8. Run the soak test on the candidate release.
9. Allow a ten-minute recovery window.
10. Confirm health, active sessions, pools and persistence returned to normal.
11. Save raw output, configuration, logs and the completed report.

## 11. Observability and evidence

### 11.1 Available project evidence

| Source | Evidence |
|---|---|
| `GET /api/health/ready` | MySQL, PostgreSQL, Redis and MongoDB readiness |
| `GET :9002/health` | Voice-worker status, Redis health and `active_sessions` |
| MongoDB `conversation_transcripts` | Per-turn route, KB use and `latencyMs` |
| MongoDB `voice_events` | Cancellation, handoff and timeout events |
| MySQL `conversation_sessions` | Completion, duration, status and cost |
| Application logs | Provider first token, errors, retries and session lifecycle |
| Perf harness | Request percentiles and client-observed T0-T6 timing |

Useful transcript inspection in `mongosh`:

```javascript
db.conversation_transcripts.find(
  { bot_id: "bot_8b3d28ab4ea0" },
  { session_id: 1, created_at: 1, "turns.role": 1, "turns.route": 1, "turns.latencyMs": 1 }
).sort({ created_at: -1 }).limit(20)
```

### 11.2 Host and datastore monitoring

Capture, at minimum:

- Per-process CPU, resident memory, open files and network throughput.
- Voice-worker event-loop lag and active-session count.
- MySQL connections, slow queries, lock waits and pool checkout time.
- PostgreSQL active/waiting connections, query time and index usage.
- Redis latency, blocked clients, memory and evictions.
- MongoDB operation latency, connections and write errors.
- Provider HTTP status, retry count and rate-limit response headers where available.

The project does not currently expose a complete Prometheus endpoint or persist all
T0-T6 marks. Until that instrumentation is added, the external load harness and
timestamped simulator logs are the source of truth for end-to-end voice latency.

## 12. Analysis and pass/fail rules

A performance run passes only when all of the following are true:

1. Every mandatory scenario completed at its planned load and duration.
2. p95/p99 and error-rate targets pass after excluding only documented expected errors.
3. No tenant-data leakage, duplicate billing, transcript loss or corrupted session occurred.
4. Resource targets pass and the system recovers after load.
5. There is no memory/connection growth trend during the soak test.
6. Candidate p95 is not more than 10% slower than the accepted baseline for critical
   voice and RAG metrics, unless the new absolute result is below a formally approved
   noise floor.
7. Results are reproducible: at least two of three baseline/target runs pass.

Do not average percentiles from separate runs. Retain raw samples, compare each run,
and calculate an aggregate percentile only from the combined raw sample set when the
environment and configuration are identical.

Severity guidance:

| Severity | Example |
|---|---|
| Blocker | Calls fail or data is corrupted at/below target load |
| Critical | Voice p95 misses by > 20%, worker crashes, or error rate >= 5% |
| Major | SLO miss <= 20%, resource saturation, or slow recovery |
| Minor | Non-critical endpoint regression or reporting/instrumentation gap |

## 13. Results template

Copy this section for each formal run:

```text
Run ID:
Date/time/timezone:
Environment:
Git commit and worktree state:
Load-generator host/version:
Dataset and cache state:
Bot/version/language:
STT / LLM / TTS / embedding provider and model:
Turn detection settings:
Worker count and concurrency per worker:
API virtual users / active calls / duration:

Scenario | Samples | Throughput | p50 | p95 | p99 | Error % | Target | Pass/Fail
---------|---------|------------|-----|-----|-----|---------|--------|----------

Peak CPU:
Peak and final memory:
DB pool/connection peak:
Redis/Mongo health:
Unexpected disconnects:
Missing transcripts/sessions:
Estimated provider cost:

Comparison with previous baseline:
Bottleneck/root cause:
Defects raised:
Recommendation: PASS / PASS WITH RISK / FAIL
Approvers:
Evidence links:
```

## 14. Current reference measurements

The repository performance suite recorded these local WSL2 reference values on
2026-07-17:

| Measurement | Observed value |
|---|---:|
| pgvector dense search, warm, 5,000 chunks | p50 40.0 ms; p95 77.5 ms |
| Hybrid retrieval | p50 approximately 305 ms |
| 20 concurrent dense searches | approximately 884 ms wall time |
| Five-page PDF upload to ready | approximately 3.0 s |
| Mock-provider transcription to first TTS audio | 3 ms |

These are developer-machine reference points, not production SLOs. Mock voice
providers exclude external STT/LLM/TTS network and model latency.

## 15. Recommended follow-up automation

To make this plan continuously executable, add the following version-controlled
artifacts in later work:

1. An HTTP load script for the selected API workload mix.
2. A paced browser WebSocket PCM load client with T0-T6 timestamps.
3. A multi-call wrapper around the Vaani simulator.
4. JSON/CSV export from `tests/perf/test_performance.py` in addition to console output.
5. Prometheus/OpenTelemetry metrics for endpoint duration, event-loop lag, active
   calls, STT finalization, LLM TTFT, TTS first audio and provider errors.
6. A CI baseline-comparison job that warns at 5% and fails at a confirmed 10%
   critical-path regression.
7. A cost ceiling for real-provider test runs.

Performance testing is complete only when the report, raw evidence and configuration
are stored together and the release decision is explicitly recorded.
