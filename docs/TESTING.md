# Testing

```bash
env/bin/python -m pytest -m "not perf"         # main suite (122 tests: unit + integration)
env/bin/python -m pytest backend/tests/unit    # unit only (no services needed)
env/bin/python -m pytest -m integration        # integration only
env/bin/python -m pytest backend/tests/perf -m perf -s   # perf measurements (5, slow)
npm run typecheck                              # frontend types
npm run build                                  # typecheck + production bundle
```

A bare `env/bin/python -m pytest` collects all 127 (main + perf); deselect perf as
above for a fast run.

Configuration is in `pytest.ini`: `testpaths = backend/tests`, `asyncio_mode = auto`,
session-scoped default event loop, markers `integration` and `perf`.

## Philosophy

- **Integration tests run against the real local services** (MySQL, PostgreSQL,
  Redis, MongoDB) but only ever create and delete their **own uniquely-prefixed
  rows** (`tn_test_*`, `ks_test_*`, …) — no truncation, no resets, safe to run
  against a dev database (`backend/tests/conftest.py`, `ControlPlaneFactory`).
- **No external keys needed**: embeddings use the deterministic mock provider
  (`EMBEDDING_PROVIDER=mock` semantics via `MockEmbeddingProvider(dimension=1536)`),
  and voice tests use mock STT/TTS/LLM providers.
- `conftest.py` sets `ECHOSPHERE_TEST_NULLPOOL=1` so asyncpg connections are not
  pooled across the multiple event loops pytest + TestClient create
  (see `backend/db/postgres.py`).

## Suite layout (122 tests)

### Unit (`backend/tests/unit/`, 81)

| File | Tests | Covers |
|---|---|---|
| `test_turn_router.py` | 16 | routing priorities: smalltalk, call control, handoff, KB signals, intents → workflow/tool, safety, clarify |
| `test_providers_and_audio.py` | 13 | provider registry/factory, WAV/PCM helpers, TTS text preparation |
| `test_security_filters.py` | 12 | prompt-injection detection, context sanitization, PII masking |
| `test_retrieval_fusion.py` | 11 | query normalization, weighted RRF, dedupe, context budgeting |
| `test_storage_safety.py` | 10 | extension whitelist, path-segment validation, traversal containment |
| `test_kb_request_normalization.py` | 8 | kb_ids str/list/None normalization and dedupe |
| `test_webhook_verification.py` | 7 | Twilio + generic HMAC schemes, skew, replay |
| `test_workflow_engine.py` | 4 | booking flow, re-ask/handoff, checkpoint resume, session isolation |

### Integration (`backend/tests/integration/`, 41)

| File | Tests | Covers |
|---|---|---|
| `test_api_security.py` | 13 | REST tenant isolation (sanitized 404s), upload validation, role enforcement |
| `test_knowledge_service.py` | 12 | authorize_kb_ids modes, upload validation, ingest→search→delete lifecycle |
| `test_pgvector_store.py` | 7 | dense/keyword search correctness, tenant filter, soft delete, upsert idempotency, dimension enforcement |
| `test_mcp_isolation.py` | 5 | MCP tools cannot cross tenants; sanitized errors |
| `test_voice_pipeline.py` | 3 | KB question uses sources, greeting skips KB, interruption cancels generation |
| `test_end_to_end.py` | 1 | full flow: create KB → upload → ingest → retrieve → voice turn with citations |

## Performance suite

`backend/tests/perf/test_performance.py` (5 tests) seeds a 5,000-chunk KB once and
prints real latencies; assertions are only generous sanity ceilings so CI noise does
not flap. Measured 2026-07-17 on local dev (WSL2):

| Measurement | Result |
|---|---|
| pgvector dense search, warm, pooled (5,000 chunks) | p50 = 40.0 ms, p95 = 77.5 ms |
| Hybrid retrieval | p50 ≈ 305 ms (includes mock-embedding overhead) |
| 20 concurrent dense searches | ≈ 884 ms wall |
| 5-page PDF upload → ready | ≈ 3.0 s |
| Mock-provider transcription → first TTS audio | 3 ms |

Treat these as local reference points, not SLOs; re-run on your hardware.

## Baseline

Before this branch the backend had **no tests**, and the legacy `VoiceBot/` suite
failed collection outright (11 import errors from the `voicebot`/`VoiceBot` case
mismatch — see [MIGRATION_FROM_VOICEBOT.md](MIGRATION_FROM_VOICEBOT.md)).

## Writing new tests

- Put pure-logic tests in `unit/`; anything touching a datastore in `integration/`
  with the `integration` marker (`pytestmark = pytest.mark.integration`).
- Use `ControlPlaneFactory` for MySQL rows and the `store` / `knowledge_service` /
  `mock_embedder` fixtures from `conftest.py`; clean up exactly what you created.
- Never assert on other tenants' data or global state — suites must be runnable
  concurrently against a shared dev database.
