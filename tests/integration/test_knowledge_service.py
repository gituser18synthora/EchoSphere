"""KnowledgeService: KB authorization modes, upload lifecycle, tenant safety."""

import pytest

from shared.errors import ApiError, NotFoundError
from shared.knowledge.ingestion.pipeline import IngestionPipeline
from shared.knowledge.schemas import RetrievalRequest

pytestmark = pytest.mark.integration


async def ingest_all(store, mock_embedder):
    pipeline = IngestionPipeline(store=store, embedder=mock_embedder)
    while (job_id := await pipeline.claim_next_job()) is not None:
        await pipeline.process_job(job_id)


class TestAuthorizationModes:
    async def test_single_kb(self, knowledge_service, control_plane):
        tenant = control_plane.tenant()
        kb = control_plane.knowledge_source(tenant)
        assert await knowledge_service.authorize_kb_ids(
            tenant_id=tenant, kb_ids=[kb]
        ) == [kb]

    async def test_multiple_kbs_with_duplicates(self, knowledge_service, control_plane):
        tenant = control_plane.tenant()
        kb1 = control_plane.knowledge_source(tenant)
        kb2 = control_plane.knowledge_source(tenant)
        request = RetrievalRequest(tenant_id=tenant, kb_ids=[kb1, kb2, kb1], query="q")
        authorized = await knowledge_service.authorize_kb_ids(
            tenant_id=tenant, kb_ids=request.kb_ids
        )
        assert sorted(authorized) == sorted([kb1, kb2])

    async def test_no_kb_id_returns_all_searchable(self, knowledge_service, control_plane):
        tenant = control_plane.tenant()
        ready = control_plane.knowledge_source(tenant, status="indexed")
        control_plane.knowledge_source(tenant, status="indexing")  # not ready
        control_plane.knowledge_source(tenant, status="failed")
        authorized = await knowledge_service.authorize_kb_ids(tenant_id=tenant, kb_ids=None)
        assert ready in authorized
        assert all(kb == ready or not kb.startswith("kstest_") for kb in authorized)

    async def test_cross_tenant_kb_is_404(self, knowledge_service, control_plane):
        tenant_a = control_plane.tenant()
        tenant_b = control_plane.tenant()
        kb_b = control_plane.knowledge_source(tenant_b)
        with pytest.raises(NotFoundError):
            await knowledge_service.authorize_kb_ids(tenant_id=tenant_a, kb_ids=[kb_b])

    async def test_invalid_kb_is_404(self, knowledge_service, control_plane):
        tenant = control_plane.tenant()
        with pytest.raises(NotFoundError):
            await knowledge_service.authorize_kb_ids(
                tenant_id=tenant, kb_ids=["ks_does_not_exist"]
            )

    async def test_mixed_valid_invalid_rejected_wholesale(
        self, knowledge_service, control_plane
    ):
        tenant = control_plane.tenant()
        kb = control_plane.knowledge_source(tenant)
        with pytest.raises(NotFoundError):
            await knowledge_service.authorize_kb_ids(
                tenant_id=tenant, kb_ids=[kb, "ks_bogus"]
            )

    async def test_archived_kb_not_searchable(self, knowledge_service, control_plane):
        from sqlalchemy import update

        from shared.db.mysql import get_sessionmaker
        from shared.models import KnowledgeSource

        tenant = control_plane.tenant()
        kb = control_plane.knowledge_source(tenant)
        session = get_sessionmaker()()
        session.execute(
            update(KnowledgeSource).where(KnowledgeSource.id == kb).values(is_deleted=True)
        )
        session.commit()
        session.close()
        with pytest.raises(NotFoundError):
            await knowledge_service.authorize_kb_ids(tenant_id=tenant, kb_ids=[kb])


class TestUploadLifecycle:
    async def test_upload_ingest_search_delete(
        self, knowledge_service, store, mock_embedder, control_plane, pg_cleanup, pdf_bytes
    ):
        tenant = control_plane.tenant()
        kb = control_plane.knowledge_source(tenant)
        upload = await knowledge_service.upload_document(
            tenant_id=tenant, kb_id=kb, file_name="policy.pdf", data=pdf_bytes
        )
        pg_cleanup.append(upload.document_id)
        assert not upload.duplicate

        await ingest_all(store, mock_embedder)
        status = await knowledge_service.get_ingestion_status(
            tenant_id=tenant, document_id=upload.document_id
        )
        assert status.status == "ready" and status.chunk_count > 0

        result = await knowledge_service.search(
            RetrievalRequest(tenant_id=tenant, kb_ids=[kb], query="policy grace period")
        )
        assert result.answerable and "30 days" in result.sources[0].text

        # Duplicate upload detected by content hash.
        duplicate = await knowledge_service.upload_document(
            tenant_id=tenant, kb_id=kb, file_name="policy-copy.pdf", data=pdf_bytes
        )
        assert duplicate.duplicate and duplicate.document_id == upload.document_id

        await knowledge_service.delete_document(
            tenant_id=tenant, document_id=upload.document_id
        )
        gone = await knowledge_service.search(
            RetrievalRequest(tenant_id=tenant, kb_ids=[kb], query="policy grace period")
        )
        assert not gone.sources

    async def test_oversize_rejected(self, knowledge_service, control_plane, monkeypatch):
        from shared.config import get_settings

        tenant = control_plane.tenant()
        kb = control_plane.knowledge_source(tenant)
        monkeypatch.setattr(get_settings(), "knowledge_max_file_mb", 0, raising=False)
        with pytest.raises(ApiError):
            await knowledge_service.upload_document(
                tenant_id=tenant, kb_id=kb, file_name="a.txt", data=b"x" * 2048
            )

    async def test_bad_extension_rejected(self, knowledge_service, control_plane):
        tenant = control_plane.tenant()
        kb = control_plane.knowledge_source(tenant)
        with pytest.raises(Exception):
            await knowledge_service.upload_document(
                tenant_id=tenant, kb_id=kb, file_name="evil.exe", data=b"MZ"
            )

    async def test_upload_to_cross_tenant_kb_404(
        self, knowledge_service, control_plane, pdf_bytes
    ):
        tenant_a = control_plane.tenant()
        tenant_b = control_plane.tenant()
        kb_b = control_plane.knowledge_source(tenant_b)
        with pytest.raises(NotFoundError):
            await knowledge_service.upload_document(
                tenant_id=tenant_a, kb_id=kb_b, file_name="a.pdf", data=pdf_bytes
            )


class TestRetrievalIsolation:
    async def test_search_never_leaks_other_tenant(
        self, knowledge_service, store, mock_embedder, control_plane, pg_cleanup
    ):
        tenant_a = control_plane.tenant()
        tenant_b = control_plane.tenant()
        kb_b = control_plane.knowledge_source(tenant_b)
        upload = await knowledge_service.upload_document(
            tenant_id=tenant_b, kb_id=kb_b, file_name="secret.txt",
            data=b"tenant b confidential launch plan details " * 20,
        )
        pg_cleanup.append(upload.document_id)
        await ingest_all(store, mock_embedder)

        # Mode 3 search for tenant A must not see tenant B's content.
        result = await knowledge_service.search(
            RetrievalRequest(tenant_id=tenant_a, query="confidential launch plan")
        )
        assert all("confidential launch plan" not in s.text for s in result.sources)
