# PostgreSQL + pgvector (Knowledge Plane)

The knowledge plane runs in its own PostgreSQL database, `echosphere_knowledge`
(local dev is tested on PostgreSQL 18 with the pgvector extension), accessed with
async SQLAlchemy 2.0 + asyncpg (`backend/db/postgres.py`). The MySQL control plane is
untouched; `knowledge_chunks.kb_id` references MySQL `knowledge_sources.id` logically
only (cross-database — no FK constraint).

## Setup

```bash
sudo -u postgres psql -c "CREATE ROLE echosphere LOGIN PASSWORD '<password>';"
sudo -u postgres psql -c "CREATE DATABASE echosphere_knowledge OWNER echosphere;"
# pgvector must be installed on the server; extension creation needs superuser:
sudo -u postgres psql -d echosphere_knowledge -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Set `POSTGRES_HOST/PORT/DATABASE/USER/PASSWORD` in `.env`, then migrate:

```bash
env/bin/python -m backend.cli pg-migrate
# equivalent:
env/bin/alembic -c backend/alembic_pg.ini upgrade head
```

The PG Alembic environment (`backend/alembic_pg/`) is separate from the MySQL one:
its config is `backend/alembic_pg.ini`, its version table is `alembic_version_pg`,
and the URL is injected from `.env` inside `alembic_pg/env.py` (with `%` escaped to
`%%` — required by ConfigParser when passwords contain `%`).

The initial migration (`a1f2c3d4e5f6_knowledge_plane_initial.py`) also runs
`CREATE EXTENSION IF NOT EXISTS vector` and `pg_trgm`, so pre-creating the extension
is only necessary when the migration user is not superuser.

**Rollback**: `alembic -c backend/alembic_pg.ini downgrade base` drops the three
knowledge tables; extensions are intentionally left installed (shared database
objects).

## Schema

Defined in `backend/knowledge/models.py` (own `DeclarativeBase`, so each Alembic
environment migrates exactly one database):

### knowledge_documents
One uploaded file per row. Notable: `content_hash` (sha256) with unique constraint
`uq_kdoc_kb_content_hash (kb_id, content_hash)` for per-KB dedupe; status
`pending | processing | ready | failed | cancelled | archived`; `storage_path`
(server-generated, relative to `KNOWLEDGE_UPLOAD_DIR`); soft-delete columns.

### knowledge_chunks
The retrieval unit:

- `embedding Vector(1536)` — dimension fixed at migration time (`EMBEDDING_DIM`);
  writes with a different dimension are rejected by `PgVectorStore.upsert_chunks`.
- `uq_kchunk_doc_index (document_id, chunk_index)` — idempotent upserts; re-index
  replaces in place.
- Provenance: `page_number`, `section`, `topic`, `chunk_type`, `keywords`,
  `language`, `meta` (JSONB, includes `file_name` and any
  `prompt_injection_flags`).
- Filtering columns used by every query: `tenant_id` (NULL = platform/global scope),
  `kb_id`, `status` (`active | archived`), `is_deleted`.

### knowledge_ingestion_jobs
DB-backed queue: status `queued | running | completed | failed | cancelled`, `stage`,
`progress`, `attempts`/`max_attempts`, `error`, `payload` (e.g. `{"reindex": true}`),
timestamps. Claimed with `FOR UPDATE SKIP LOCKED` ordered by `queued_at`
(index `ix_kjob_status_queued`).

## Indexes

Created in the migration (expression indexes are kept out of the ORM definition):

```sql
-- ANN: HNSW, cosine (matches OpenAI text-embedding-* models);
-- m / ef_construction come from PGVECTOR_HNSW_M / PGVECTOR_HNSW_EF_CONSTRUCTION
CREATE INDEX ix_kchunk_embedding_hnsw ON knowledge_chunks
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Keyword FTS: expression GIN — the ts config MUST match RETRIEVAL_TS_CONFIG
CREATE INDEX ix_kchunk_content_tsv ON knowledge_chunks
  USING gin (to_tsvector('english', content));
```

Plus composite b-trees: `ix_kchunk_tenant_kb (tenant_id, kb_id)`,
`ix_kchunk_kb_status_deleted (kb_id, status, is_deleted)`, `ix_kchunk_document`,
and the document/job indexes listed in the migration.

## Query behavior (`backend/knowledge/vector_store/pgvector_store.py`)

- **Dense**: `ORDER BY embedding <=> :query` (cosine distance) with
  `SET LOCAL hnsw.ef_search = PGVECTOR_HNSW_EF_SEARCH` (default 100) executed in the
  same transaction. Similarity is reported as `1 - distance`, floored at 0.
- **Keyword**: `websearch_to_tsquery` + `ts_rank_cd`; when the AND semantics match
  nothing, a second pass ORs the sanitized terms via `to_tsquery`.
- **Every** read and write includes the tenant clause and
  `status='active' AND is_deleted=false` **in SQL**, not in Python.
- Upserts are batched (200 rows) via `INSERT ... ON CONFLICT (uq_kchunk_doc_index)
  DO UPDATE`, resurrecting soft-deleted rows on re-index.
- Deletes are soft (`status='archived', is_deleted=true`); only re-indexing performs
  physical `DELETE` (`hard_delete_document`).

## Connection pooling

`get_pg_engine()` pools with `pool_pre_ping`, `pool_recycle=3600`,
size `POSTGRES_POOL_SIZE=10`, `max_overflow=20`. Setting
`ECHOSPHERE_TEST_NULLPOOL=1` switches to `NullPool` — required for the test suite,
where multiple event loops (pytest session loop + TestClient portals) would otherwise
share loop-bound asyncpg connections. `pg_health_check()` verifies the connection,
the pgvector extension version and a real vector operation
(`SELECT '[1,0]'::vector <=> '[0,1]'::vector`).

## Measured performance

From the perf suite (`backend/tests/perf/test_performance.py`,
run 2026-07-17 on local dev under WSL2 — see [TESTING.md](TESTING.md)):

| Scenario | Result |
|---|---|
| Dense search, warm, 5,000-chunk KB, pooled | p50 = 40.0 ms, p95 = 77.5 ms |
| Hybrid search (same corpus) | p50 ≈ 305 ms (includes mock-embedding overhead) |
| 20 concurrent dense searches | ≈ 884 ms wall time |
| 5-page PDF upload → ready | ≈ 3.0 s |

Numbers are local-dev measurements, not production benchmarks; re-run with
`env/bin/python -m pytest backend/tests/perf -m perf -s`.
