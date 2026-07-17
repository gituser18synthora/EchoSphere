"""Knowledge documents: upload, ingestion lifecycle and retrieval testing.

The metadata-level `knowledge_sources` API (routers/knowledge.py) is unchanged;
this router adds the real document plane backed by PostgreSQL + pgvector.
"""

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    get_current_user,
    is_super_admin,
    require_tenant_admin,
    require_tenant_member,
)
from backend.core.errors import ApiError, NotFoundError
from backend.core.responses import ok
from backend.db.mysql import get_db
from backend.knowledge.schemas import RetrievalRequest
from backend.knowledge.service import get_knowledge_service
from backend.models import KnowledgeSource, User

router = APIRouter(tags=["Knowledge Documents"])


def _resolve_source(db: Session, user: User, source_id: str) -> KnowledgeSource:
    """Load a source with tenant enforcement (404 on cross-tenant access)."""
    source = db.get(KnowledgeSource, source_id)
    if source is None or source.is_deleted:
        raise NotFoundError("Knowledge source not found")
    if source.scope == "global":
        if not is_super_admin(user):
            raise NotFoundError("Knowledge source not found")
    elif not is_super_admin(user) and source.tenant_id != user.tenant_id:
        raise NotFoundError("Knowledge source not found")
    return source


@router.post("/knowledge/{source_id}/documents", status_code=201)
async def upload_document(
    source_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    source = _resolve_source(db, user, source_id)
    data = await file.read()
    service = get_knowledge_service()
    result = await service.upload_document(
        tenant_id=source.tenant_id,
        kb_id=source.id,
        file_name=file.filename or "upload",
        data=data,
        uploaded_by=user.id,
    )
    record_audit(
        db, user=user, action="knowledge.document.upload", entity_type="knowledge_document",
        entity_id=result.document_id, target_label=file.filename,
        tenant_id=source.tenant_id, request=request,
        new_value={"kbId": source.id, "sizeBytes": len(data), "duplicate": result.duplicate},
    )
    db.commit()
    return ok(
        {
            "documentId": result.document_id,
            "jobId": result.job_id,
            "kbId": result.kb_id,
            "duplicate": result.duplicate,
            "status": result.status,
        }
    )


@router.get("/knowledge/{source_id}/documents")
async def list_documents(
    source_id: str,
    user: User = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    source = _resolve_source(db, user, source_id)
    statuses = await get_knowledge_service().list_documents(
        tenant_id=source.tenant_id, kb_id=source.id
    )
    return ok([_serialize_status(s) for s in statuses])


def _serialize_status(status) -> dict:
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


def _document_tenant(user: User) -> str | None:
    """Documents are tenant-filtered by the service; super admin sees all."""
    return None if is_super_admin(user) else user.tenant_id


@router.get("/knowledge/documents/{document_id}/status")
async def document_status(
    document_id: str,
    user: User = Depends(require_tenant_member),
):
    status = await get_knowledge_service().get_ingestion_status(
        tenant_id=_document_tenant(user), document_id=document_id
    )
    return ok(_serialize_status(status))


@router.post("/knowledge/documents/{document_id}/retry")
async def retry_document(
    document_id: str,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    status = await get_knowledge_service().retry_ingestion(
        tenant_id=_document_tenant(user), document_id=document_id
    )
    record_audit(
        db, user=user, action="knowledge.document.retry", entity_type="knowledge_document",
        entity_id=document_id, target_label=status.file_name,
        tenant_id=user.tenant_id, request=request,
    )
    db.commit()
    return ok(_serialize_status(status))


@router.post("/knowledge/documents/{document_id}/cancel")
async def cancel_document(
    document_id: str,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    status = await get_knowledge_service().cancel_ingestion(
        tenant_id=_document_tenant(user), document_id=document_id
    )
    record_audit(
        db, user=user, action="knowledge.document.cancel", entity_type="knowledge_document",
        entity_id=document_id, target_label=status.file_name,
        tenant_id=user.tenant_id, request=request,
    )
    db.commit()
    return ok(_serialize_status(status))


@router.post("/knowledge/documents/{document_id}/reindex")
async def reindex_document(
    document_id: str,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    status = await get_knowledge_service().reindex_document(
        tenant_id=_document_tenant(user), document_id=document_id
    )
    record_audit(
        db, user=user, action="knowledge.document.reindex", entity_type="knowledge_document",
        entity_id=document_id, target_label=status.file_name,
        tenant_id=user.tenant_id, request=request,
    )
    db.commit()
    return ok(_serialize_status(status))


@router.delete("/knowledge/documents/{document_id}")
async def delete_document(
    document_id: str,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    await get_knowledge_service().delete_document(
        tenant_id=_document_tenant(user), document_id=document_id, deleted_by=user.id
    )
    record_audit(
        db, user=user, action="knowledge.document.delete", entity_type="knowledge_document",
        entity_id=document_id, tenant_id=user.tenant_id, request=request,
    )
    db.commit()
    return ok({"archived": True, "id": document_id})


class RetrievalTestRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[str] | str | None = Field(default=None, alias="kbIds")
    bot_id: str | None = Field(default=None, alias="botId")
    top_k: int = Field(default=6, ge=1, le=20, alias="topK")

    model_config = {"populate_by_name": True}


@router.post("/knowledge/search-test")
async def search_test(
    body: RetrievalTestRequest,
    user: User = Depends(require_tenant_member),
):
    """Retrieval testing for the studio UI — same service the voice bot uses."""
    result = await get_knowledge_service().search(
        RetrievalRequest(
            tenant_id=_document_tenant(user),
            kb_ids=body.kb_ids,
            bot_id=body.bot_id,
            query=body.query,
            top_k=body.top_k,
        )
    )
    return ok(
        {
            "usedKnowledgeBase": result.used_knowledge_base,
            "answerable": result.answerable,
            "confidence": result.confidence,
            "query": result.query,
            "kbIds": result.kb_ids,
            "durationMs": result.duration_ms,
            "skippedReason": result.skipped_reason,
            "sources": [
                {
                    "kbId": s.kb_id,
                    "documentId": s.document_id,
                    "chunkId": s.chunk_id,
                    "chunkIndex": s.chunk_index,
                    "pageNumber": s.page_number,
                    "section": s.section,
                    "score": round(s.score, 4),
                    "vectorScore": s.vector_score and round(s.vector_score, 4),
                    "keywordScore": s.keyword_score and round(s.keyword_score, 4),
                    "text": s.text[:800],
                    "documentName": s.document_name,
                }
                for s in result.sources
            ],
        }
    )
