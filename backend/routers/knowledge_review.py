"""Super Admin → Knowledge Management → Chunk Review.

Secured, server-side-paginated APIs for inspecting uploaded knowledge documents
and the chunks generated from them, verifying chunk boundaries/overlap, running
retrieval tests, and safe curation (chunk active/inactive, flag-for-review,
document retry/reindex/archive, original-file download).

Access is gated on the `review_knowledge_chunks` permission (held by Super Admin
only in the base seed). Tenant isolation: a Super Admin (tenant_id=None) sees
every tenant; any other holder of the permission is restricted to their own
tenant at the SQL-query level. Sensitive actions are written to the audit log.
Embeddings are never returned.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import get_current_user, is_super_admin, require_permission
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from shared.db.mysql import get_db
from shared.errors import ApiError
from shared.knowledge.review import ChunkFilters, DocumentFilters, get_review_service
from shared.knowledge.service import get_knowledge_service
from shared.models import User

router = APIRouter(prefix="/admin/knowledge/review", tags=["Knowledge Chunk Review"])

# One permission gate for the whole console.
_reviewer = require_permission("review_knowledge_chunks")


def _review_tenant(user: User) -> str | None:
    """Effective tenant for review queries: Super Admin sees all (None);
    any other permitted user is pinned to their own tenant."""
    return None if is_super_admin(user) else user.tenant_id


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(f"Invalid date: {value}", status_code=422) from exc


def _kb_list(kb_id: str | None, kb_ids: str | None) -> list[str] | None:
    """Accept either a single kbId or a comma-separated kbIds list."""
    values: list[str] = []
    if kb_id:
        values.append(kb_id)
    if kb_ids:
        values.extend(v.strip() for v in kb_ids.split(",") if v.strip())
    return list(dict.fromkeys(values)) or None


# ── filter facets & knowledge-base list ────────────────────────────────────


@router.get("/facets")
async def review_facets(user: User = Depends(_reviewer)):
    return ok(await get_review_service().facets(tenant_id=_review_tenant(user)))


@router.get("/knowledge-bases")
async def review_knowledge_bases(user: User = Depends(_reviewer)):
    return ok(await get_review_service().list_knowledge_bases(tenant_id=_review_tenant(user)))


# ── documents ──────────────────────────────────────────────────────────────


@router.get("/documents")
async def review_documents(
    kb_id: str | None = Query(None, alias="kbId"),
    kb_ids: str | None = Query(None, alias="kbIds"),
    file_type: str | None = Query(None, alias="fileType", max_length=16),
    status: str | None = Query(None, max_length=20),
    ingestion_status: str | None = Query(None, alias="ingestionStatus", max_length=20),
    language: str | None = Query(None, max_length=20),
    uploaded_from: str | None = Query(None, alias="uploadedFrom"),
    uploaded_to: str | None = Query(None, alias="uploadedTo"),
    failed_only: bool = Query(False, alias="failedOnly"),
    include_archived: bool = Query(False, alias="includeArchived"),
    tenant_id: str | None = Query(None, alias="tenantId"),
    params: PageParams = Depends(page_params),
    user: User = Depends(_reviewer),
):
    effective_tenant = _resolve_scope(user, tenant_id)
    filters = DocumentFilters(
        kb_ids=_kb_list(kb_id, kb_ids),
        file_ext=file_type,
        status=status,
        ingestion_status=ingestion_status,
        language=language,
        uploaded_from=_parse_dt(uploaded_from),
        uploaded_to=_parse_dt(uploaded_to),
        failed_only=failed_only,
        include_archived=include_archived,
        search=params.search,
    )
    rows, total = await get_review_service().list_documents(
        tenant_id=effective_tenant,
        filters=filters,
        page=params.page,
        page_size=params.page_size,
        sort_by=params.sort_by,
        sort_dir=params.sort_dir,
    )
    return paginated(rows, page=params.page, page_size=params.page_size, total=total)


@router.get("/documents/{document_id}")
async def review_document_detail(
    document_id: str,
    request: Request,
    user: User = Depends(_reviewer),
    db: Session = Depends(get_db),
):
    doc = await get_review_service().get_document(
        tenant_id=_review_tenant(user), document_id=document_id
    )
    # Viewing sensitive document details is an audited event (req. 18).
    record_audit(
        db, user=user, action="knowledge.review.document.view",
        entity_type="knowledge_document", entity_id=document_id,
        target_label=doc.get("fileName"), tenant_id=doc.get("tenantId"), request=request,
    )
    db.commit()
    return ok(doc)


@router.get("/documents/{document_id}/download")
async def review_document_download(
    document_id: str,
    request: Request,
    user: User = Depends(_reviewer),
    db: Session = Depends(get_db),
):
    from shared.knowledge.ingestion import storage

    doc = await get_knowledge_service().get_document(
        tenant_id=_review_tenant(user), document_id=document_id
    )
    if not doc.storage_path or doc.is_deleted:
        raise ApiError("Original file is not available for this document", status_code=404)
    path = storage.resolve_path(doc.storage_path)
    if not path.is_file():
        raise ApiError("Original file is missing from storage", status_code=404)
    record_audit(
        db, user=user, action="knowledge.review.document.download",
        entity_type="knowledge_document", entity_id=document_id,
        target_label=doc.file_name, tenant_id=doc.tenant_id, request=request,
    )
    db.commit()
    return FileResponse(
        path, filename=doc.file_name, media_type=doc.mime_type or "application/octet-stream"
    )


@router.post("/documents/{document_id}/retry")
async def review_document_retry(
    document_id: str,
    request: Request,
    user: User = Depends(_reviewer),
    db: Session = Depends(get_db),
):
    status = await get_knowledge_service().retry_ingestion(
        tenant_id=_review_tenant(user), document_id=document_id
    )
    record_audit(
        db, user=user, action="knowledge.review.document.retry",
        entity_type="knowledge_document", entity_id=document_id,
        target_label=status.file_name, request=request,
    )
    db.commit()
    return ok(_status_dict(status))


@router.post("/documents/{document_id}/reindex")
async def review_document_reindex(
    document_id: str,
    request: Request,
    user: User = Depends(_reviewer),
    db: Session = Depends(get_db),
):
    status = await get_knowledge_service().reindex_document(
        tenant_id=_review_tenant(user), document_id=document_id
    )
    record_audit(
        db, user=user, action="knowledge.review.document.reindex",
        entity_type="knowledge_document", entity_id=document_id,
        target_label=status.file_name, request=request,
    )
    db.commit()
    return ok(_status_dict(status))


@router.post("/documents/{document_id}/archive")
async def review_document_archive(
    document_id: str,
    request: Request,
    user: User = Depends(_reviewer),
    db: Session = Depends(get_db),
):
    # Soft archive (recoverable) — chunks stop surfacing in retrieval. A true
    # hard delete is intentionally not exposed here.
    await get_knowledge_service().delete_document(
        tenant_id=_review_tenant(user), document_id=document_id, deleted_by=user.id
    )
    record_audit(
        db, user=user, action="knowledge.review.document.archive",
        entity_type="knowledge_document", entity_id=document_id, request=request,
    )
    db.commit()
    return ok({"archived": True, "id": document_id})


# ── chunks ────────────────────────────────────────────────────────────────


@router.get("/chunks")
async def review_chunks(
    document_id: str | None = Query(None, alias="documentId"),
    kb_id: str | None = Query(None, alias="kbId"),
    kb_ids: str | None = Query(None, alias="kbIds"),
    status: str | None = Query(None, max_length=20),
    language: str | None = Query(None, max_length=20),
    page_number: int | None = Query(None, alias="pageNumber", ge=0),
    section: str | None = Query(None, max_length=300),
    created_from: str | None = Query(None, alias="createdFrom"),
    created_to: str | None = Query(None, alias="createdTo"),
    min_tokens: int | None = Query(None, alias="minTokens", ge=0),
    max_tokens: int | None = Query(None, alias="maxTokens", ge=0),
    has_keywords: bool | None = Query(None, alias="hasKeywords"),
    has_metadata: bool | None = Query(None, alias="hasMetadata"),
    flagged_only: bool = Query(False, alias="flaggedOnly"),
    include_archived: bool = Query(True, alias="includeArchived"),
    tenant_id: str | None = Query(None, alias="tenantId"),
    params: PageParams = Depends(page_params),
    user: User = Depends(_reviewer),
):
    effective_tenant = _resolve_scope(user, tenant_id)
    filters = ChunkFilters(
        kb_ids=_kb_list(kb_id, kb_ids),
        document_id=document_id,
        status=status,
        language=language,
        page_number=page_number,
        section=section,
        created_from=_parse_dt(created_from),
        created_to=_parse_dt(created_to),
        min_tokens=min_tokens,
        max_tokens=max_tokens,
        has_keywords=has_keywords,
        has_metadata=has_metadata,
        flagged_only=flagged_only,
        include_archived=include_archived,
        search=params.search,
    )
    rows, total = await get_review_service().list_chunks(
        tenant_id=effective_tenant,
        filters=filters,
        page=params.page,
        page_size=params.page_size,
        sort_by=params.sort_by,
        sort_dir=params.sort_dir,
    )
    return paginated(rows, page=params.page, page_size=params.page_size, total=total)


@router.get("/chunks/{chunk_id}")
async def review_chunk_detail(chunk_id: str, user: User = Depends(_reviewer)):
    return ok(await get_review_service().get_chunk(tenant_id=_review_tenant(user), chunk_id=chunk_id))


class ChunkStatusRequest(BaseModel):
    status: str = Field(pattern="^(active|archived)$")


@router.patch("/chunks/{chunk_id}/status")
async def review_chunk_status(
    chunk_id: str,
    body: ChunkStatusRequest,
    request: Request,
    user: User = Depends(_reviewer),
    db: Session = Depends(get_db),
):
    result = await get_review_service().set_chunk_status(
        tenant_id=_review_tenant(user), chunk_id=chunk_id, status=body.status, updated_by=user.id
    )
    record_audit(
        db, user=user, action="knowledge.review.chunk.status",
        entity_type="knowledge_chunk", entity_id=chunk_id,
        previous_value={"status": result["previousStatus"]},
        new_value={"status": result["status"]}, request=request,
    )
    db.commit()
    return ok(result)


class ChunkFlagRequest(BaseModel):
    flagged: bool = True
    reason: str | None = Field(default=None, max_length=500)


@router.post("/chunks/{chunk_id}/flag")
async def review_chunk_flag(
    chunk_id: str,
    body: ChunkFlagRequest,
    request: Request,
    user: User = Depends(_reviewer),
    db: Session = Depends(get_db),
):
    result = await get_review_service().flag_chunk(
        tenant_id=_review_tenant(user), chunk_id=chunk_id,
        flagged=body.flagged, reason=body.reason, flagged_by=user.id,
    )
    record_audit(
        db, user=user, action="knowledge.review.chunk.flag",
        entity_type="knowledge_chunk", entity_id=chunk_id,
        new_value={"flagged": body.flagged, "reason": body.reason}, request=request,
    )
    db.commit()
    return ok(result)


# ── retrieval testing ──────────────────────────────────────────────────────


class RetrievalTestRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[str] | None = Field(default=None, alias="kbIds")
    document_id: str | None = Field(default=None, alias="documentId")
    top_k: int = Field(default=8, ge=1, le=20, alias="topK")
    min_score: float | None = Field(default=None, alias="minScore", ge=0, le=1)

    model_config = {"populate_by_name": True}


@router.post("/retrieval-test")
async def review_retrieval_test(
    body: RetrievalTestRequest,
    request: Request,
    user: User = Depends(_reviewer),
    db: Session = Depends(get_db),
):
    result = await get_review_service().retrieval_test(
        caller_tenant_id=_review_tenant(user),
        kb_ids=body.kb_ids,
        document_id=body.document_id,
        query=body.query,
        top_k=body.top_k,
        min_score=body.min_score,
    )
    record_audit(
        db, user=user, action="knowledge.review.retrieval_test",
        entity_type="knowledge_base", entity_id=",".join(result["kbIds"])[:40],
        tenant_id=result.get("tenantId"),
        new_value={"query": body.query[:200], "results": len(result["results"])},
        request=request,
    )
    db.commit()
    return ok(result)


def _resolve_scope(user: User, requested_tenant_id: str | None) -> str | None:
    """Super Admins may narrow to a tenant via ?tenantId; others are pinned."""
    if is_super_admin(user):
        return requested_tenant_id  # None = all tenants
    return user.tenant_id


def _status_dict(status) -> dict:
    return {
        "documentId": status.document_id,
        "kbId": status.kb_id,
        "fileName": status.file_name,
        "status": status.status,
        "stage": status.stage,
        "progress": status.progress,
        "attempts": status.attempts,
        "failureReason": status.failure_reason,
        "chunkCount": status.chunk_count,
        "pageCount": status.page_count,
        "queuedAt": status.queued_at.isoformat() if status.queued_at else None,
        "startedAt": status.started_at.isoformat() if status.started_at else None,
        "finishedAt": status.finished_at.isoformat() if status.finished_at else None,
    }
