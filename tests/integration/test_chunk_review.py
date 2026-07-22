"""Super Admin Knowledge Chunk Review — document/chunk inspection, filtering,
pagination, neighbour context, curation, retrieval testing, and access control.

Runs against the live app + local MySQL/PostgreSQL with the mock embedder. All
rows are test-owned (unique ids) and removed in module teardown, including the
audit-log entries the review actions create.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_sessionmaker
from shared.errors import NotFoundError
from shared.ids import new_id
from shared.knowledge.embeddings.mock_provider import MockEmbeddingProvider
from shared.knowledge.ingestion.pipeline import IngestionPipeline
from shared.knowledge.review import get_review_service
from shared.knowledge.service import KnowledgeService
from shared.knowledge.vector_store import PgVectorStore
from shared.models import KnowledgeSource, Tenant, User

pytestmark = pytest.mark.integration

API = "/api/v1/admin/knowledge/review"
_SUFFIX = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _bearer(email: str) -> dict:
    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code, tenant_id=user.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


def _data(resp):
    body = resp.json()
    assert body.get("success") is True, (resp.status_code, body)
    return body["data"]


async def _ingest_pending():
    store = PgVectorStore()
    pipeline = IngestionPipeline(store=store, embedder=MockEmbeddingProvider(dimension=1536))
    while (job_id := await pipeline.claim_next_job()) is not None:
        await pipeline.process_job(job_id)


@pytest.fixture(scope="module")
async def seeded():
    """Two tenants each with an ingested document, plus a zero-chunk document."""
    embedder = MockEmbeddingProvider(dimension=1536)
    store = PgVectorStore()
    svc = KnowledgeService(store=store, embedder=embedder)
    session = get_sessionmaker()()

    def _mk_tenant(name):
        tid = f"tn_rev_{uuid.uuid4().hex[:10]}"
        session.add(Tenant(id=tid, name=f"{name} {tid[-4:]}", domain=f"{tid}.example.test",
                           code=f"C{tid[-5:]}", status="active"))
        session.commit()
        return tid

    def _mk_kb(tid, name):
        kid = f"ksrev_{uuid.uuid4().hex[:10]}"
        session.add(KnowledgeSource(id=kid, tenant_id=tid, scope="tenant", type="document",
                                    name=name, status="indexed"))
        session.commit()
        return kid

    tenant_a = _mk_tenant("Review Alpha")
    tenant_b = _mk_tenant("Review Beta")
    kb_a = _mk_kb(tenant_a, f"Alpha KB {_SUFFIX}")
    kb_b = _mk_kb(tenant_b, f"Beta KB {_SUFFIX}")

    # Large enough to split into several chunks (text chunker ≈ 2048 chars/chunk).
    text_a = ("The grace period for policy renewal is exactly 30 days. "
              "Customers may renew within the grace period without any penalty fee. "
              "Claims filed during the grace period are processed under the prior terms. " * 60)
    text_b = "Beta tenant confidential product roadmap and pricing details. " * 120

    up_a = await svc.upload_document(tenant_id=tenant_a, kb_id=kb_a, file_name="alpha.txt",
                                     data=text_a.encode())
    up_b = await svc.upload_document(tenant_id=tenant_b, kb_id=kb_b, file_name="beta.txt",
                                     data=text_b.encode())
    await _ingest_pending()

    # A zero-chunk document: uploaded then cancelled before ingestion.
    up_zero = await svc.upload_document(tenant_id=tenant_a, kb_id=kb_a, file_name="empty.txt",
                                        data=b"placeholder document body kept tiny")
    await svc.cancel_ingestion(tenant_id=tenant_a, document_id=up_zero.document_id)

    doc_ids = [up_a.document_id, up_b.document_id, up_zero.document_id]
    data = {
        "tenant_a": tenant_a, "tenant_b": tenant_b, "kb_a": kb_a, "kb_b": kb_b,
        "doc_a": up_a.document_id, "doc_b": up_b.document_id, "doc_zero": up_zero.document_id,
    }
    yield data

    # teardown — PG chunks/jobs/docs, MySQL KBs/tenants, and audit rows.
    from sqlalchemy import delete as sa_delete

    from shared.db.postgres import get_pg_sessionmaker
    from shared.knowledge.models import IngestionJob, KnowledgeChunk, KnowledgeDocument

    async with get_pg_sessionmaker()() as pg:
        await pg.execute(sa_delete(IngestionJob).where(IngestionJob.document_id.in_(doc_ids)))
        await pg.execute(sa_delete(KnowledgeChunk).where(KnowledgeChunk.document_id.in_(doc_ids)))
        await pg.execute(sa_delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(doc_ids)))
        await pg.commit()

    from shared.models import AuditLog

    session.execute(sa_delete(AuditLog).where(AuditLog.entity_id.in_(doc_ids)))
    session.execute(sa_delete(KnowledgeSource).where(KnowledgeSource.id.in_([kb_a, kb_b])))
    session.execute(sa_delete(Tenant).where(Tenant.id.in_([tenant_a, tenant_b])))
    session.commit()
    session.close()


# ── access control ─────────────────────────────────────────────────────────


def test_unauthenticated_is_401(client):
    assert client.get(f"{API}/facets").status_code == 401


def test_tenant_roles_forbidden(client):
    assert client.get(f"{API}/facets", headers=_bearer("priya.sharma@meridianhealth.com")).status_code == 403
    assert client.get(f"{API}/facets", headers=_bearer("sam.ellery@meridianhealth.com")).status_code == 403


# ── facets & knowledge bases ─────────────────────────────────────────────────


def test_facets_and_kbs(client, seeded):
    h = _bearer("admin@aurexion.com")
    facets = _data(client.get(f"{API}/facets", headers=h))
    assert {"tenants", "fileTypes", "uploadStatuses", "chunkStatuses"} <= facets.keys()
    assert any(t["id"] == seeded["tenant_a"] for t in facets["tenants"])
    kbs = _data(client.get(f"{API}/knowledge-bases", headers=h))
    assert any(k["id"] == seeded["kb_a"] for k in kbs)


# ── document listing / filtering ─────────────────────────────────────────────


def test_list_documents_across_tenants(client, seeded):
    h = _bearer("admin@aurexion.com")
    docs = _data(client.get(f"{API}/documents?pageSize=200", headers=h))
    ids = {d["documentId"] for d in docs}
    assert seeded["doc_a"] in ids and seeded["doc_b"] in ids
    row = next(d for d in docs if d["documentId"] == seeded["doc_a"])
    assert row["tenantName"] and row["kbName"] and row["chunkCount"] > 0
    assert "embedding" not in row  # never leak vectors


def test_filter_by_tenant_and_kb(client, seeded):
    h = _bearer("admin@aurexion.com")
    docs = _data(client.get(f"{API}/documents?tenantId={seeded['tenant_a']}&pageSize=100", headers=h))
    assert docs and all(d["tenantId"] == seeded["tenant_a"] for d in docs)
    assert seeded["doc_b"] not in {d["documentId"] for d in docs}
    by_kb = _data(client.get(f"{API}/documents?kbId={seeded['kb_a']}&pageSize=100", headers=h))
    assert all(d["kbId"] == seeded["kb_a"] for d in by_kb)


def test_document_detail_has_quality(client, seeded):
    h = _bearer("admin@aurexion.com")
    d = _data(client.get(f"{API}/documents/{seeded['doc_a']}", headers=h))
    assert d["fileName"] == "alpha.txt"
    assert d["quality"]["totalChunks"] > 0
    assert d["hasOriginalFile"] is True
    assert d["tenantCode"]


# ── chunk listing / filtering / search / pagination ──────────────────────────


def test_chunk_pagination(client, seeded):
    h = _bearer("admin@aurexion.com")
    p1 = client.get(f"{API}/chunks?documentId={seeded['doc_a']}&page=1&pageSize=2", headers=h).json()
    assert p1["meta"]["total"] > 2 and len(p1["data"]) == 2
    p2 = client.get(f"{API}/chunks?documentId={seeded['doc_a']}&page=2&pageSize=2", headers=h).json()
    assert {c["chunkId"] for c in p1["data"]}.isdisjoint({c["chunkId"] for c in p2["data"]})
    # natural order: ascending chunk index within a document
    assert [c["chunkIndex"] for c in p1["data"]] == sorted(c["chunkIndex"] for c in p1["data"])
    assert "embedding" not in p1["data"][0]
    assert p1["data"][0]["embeddingGenerated"] is True


def test_chunk_content_search(client, seeded):
    h = _bearer("admin@aurexion.com")
    hits = _data(client.get(f"{API}/chunks?documentId={seeded['doc_a']}&search=grace+period&pageSize=50", headers=h))
    assert hits and all("grace" in c["content"].lower() for c in hits)


def test_chunk_token_and_status_filters(client, seeded):
    h = _bearer("admin@aurexion.com")
    all_chunks = _data(client.get(f"{API}/chunks?documentId={seeded['doc_a']}&pageSize=200", headers=h))
    assert all(c["tokenCount"] is not None for c in all_chunks)
    hi = max(c["tokenCount"] for c in all_chunks)
    filtered = _data(client.get(f"{API}/chunks?documentId={seeded['doc_a']}&minTokens={hi}&pageSize=200", headers=h))
    assert filtered and all(c["tokenCount"] >= hi for c in filtered)
    active = _data(client.get(f"{API}/chunks?documentId={seeded['doc_a']}&status=active&pageSize=200", headers=h))
    assert all(c["status"] == "active" for c in active)


def test_chunk_detail_prev_current_next(client, seeded):
    h = _bearer("admin@aurexion.com")
    chunks = _data(client.get(f"{API}/chunks?documentId={seeded['doc_a']}&pageSize=200", headers=h))
    middle = sorted(chunks, key=lambda c: c["chunkIndex"])[1]
    d = _data(client.get(f"{API}/chunks/{middle['chunkId']}", headers=h))
    assert d["current"]["chunkIndex"] == middle["chunkIndex"]
    assert d["prev"]["chunkIndex"] == middle["chunkIndex"] - 1
    assert d["next"]["chunkIndex"] == middle["chunkIndex"] + 1
    assert "quality" in d and "metadata" in d
    assert "embedding" not in d


def test_zero_chunk_document(client, seeded):
    h = _bearer("admin@aurexion.com")
    result = client.get(f"{API}/chunks?documentId={seeded['doc_zero']}&pageSize=50", headers=h).json()
    assert result["meta"]["total"] == 0 and result["data"] == []


# ── curation actions ─────────────────────────────────────────────────────────


def test_chunk_status_toggle(client, seeded):
    h = _bearer("admin@aurexion.com")
    chunk = _data(client.get(f"{API}/chunks?documentId={seeded['doc_a']}&pageSize=1", headers=h))[0]
    cid = chunk["chunkId"]
    r1 = _data(client.patch(f"{API}/chunks/{cid}/status", headers=h, json={"status": "archived"}))
    assert r1["status"] == "archived" and r1["previousStatus"] == "active"
    r2 = _data(client.patch(f"{API}/chunks/{cid}/status", headers=h, json={"status": "active"}))
    assert r2["status"] == "active"


def test_chunk_flag_and_unflag(client, seeded):
    h = _bearer("admin@aurexion.com")
    chunk = _data(client.get(f"{API}/chunks?documentId={seeded['doc_a']}&pageSize=1", headers=h))[0]
    cid = chunk["chunkId"]
    _data(client.post(f"{API}/chunks/{cid}/flag", headers=h, json={"flagged": True, "reason": "verify"}))
    detail = _data(client.get(f"{API}/chunks/{cid}", headers=h))
    assert detail["warnings"]["flaggedForReview"] is True
    flagged_only = _data(client.get(f"{API}/chunks?documentId={seeded['doc_a']}&flaggedOnly=true&pageSize=50", headers=h))
    assert any(c["chunkId"] == cid for c in flagged_only)
    _data(client.post(f"{API}/chunks/{cid}/flag", headers=h, json={"flagged": False}))


def test_reindex_and_retry(client, seeded):
    h = _bearer("admin@aurexion.com")
    # reindex works on a ready document
    assert client.post(f"{API}/documents/{seeded['doc_a']}/reindex", headers=h).status_code == 200
    # retry works on a cancelled document; other states are rejected
    retry = client.post(f"{API}/documents/{seeded['doc_zero']}/retry", headers=h)
    assert retry.status_code == 200
    # a ready document cannot be retried
    assert client.post(f"{API}/documents/{seeded['doc_a']}/retry", headers=h).status_code == 409


# ── retrieval testing ────────────────────────────────────────────────────────


def test_retrieval_test_scoring(client, seeded):
    h = _bearer("admin@aurexion.com")
    r = _data(client.post(f"{API}/retrieval-test", headers=h,
                          json={"kbIds": [seeded["kb_a"]], "query": "grace period renewal", "topK": 5}))
    assert r["kbIds"] == [seeded["kb_a"]]
    assert "threshold" in r and isinstance(r["results"], list)
    if r["results"]:
        hit = r["results"][0]
        assert {"rank", "score", "vectorScore", "keywordScore", "passedThreshold", "documentName"} <= hit.keys()
        assert hit["rank"] == 1


# ── cross-tenant isolation (DB-query level) ──────────────────────────────────


async def test_cross_tenant_blocked_at_service(seeded):
    svc = get_review_service()
    # tenant B may not read tenant A's document
    with pytest.raises(NotFoundError):
        await svc.get_document(tenant_id=seeded["tenant_b"], document_id=seeded["doc_a"])
    # a tenant-scoped chunk listing excludes the other tenant's chunks
    from shared.knowledge.review import ChunkFilters

    rows, total = await svc.list_chunks(
        tenant_id=seeded["tenant_b"], filters=ChunkFilters(document_id=seeded["doc_a"]),
        page=1, page_size=50, sort_by=None, sort_dir="asc",
    )
    assert total == 0 and rows == []


# ── audit logging ────────────────────────────────────────────────────────────


def test_audit_entries_written(client, seeded):
    h = _bearer("admin@aurexion.com")
    client.get(f"{API}/documents/{seeded['doc_a']}", headers=h)
    client.post(f"{API}/retrieval-test", headers=h,
                json={"kbIds": [seeded["kb_a"]], "query": "grace period", "topK": 3})
    from shared.models import AuditLog

    session = get_sessionmaker()()
    try:
        actions = set(session.execute(
            select(AuditLog.action).where(AuditLog.entity_id == seeded["doc_a"])
        ).scalars())
    finally:
        session.close()
    assert "knowledge.review.document.view" in actions
