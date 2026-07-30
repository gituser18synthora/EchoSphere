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
    require_permission,
    require_tenant_admin,
    require_tenant_member,
)
from shared.errors import ApiError, NotFoundError
from backend.core.responses import ok
from shared.db.mysql import get_db
from shared.knowledge.schemas import RetrievalRequest
from shared.knowledge.service import KnowledgeService, get_knowledge_service
from shared.models import KnowledgeSource, User

router = APIRouter(tags=["Knowledge Documents"])


@router.get("/knowledge/upload-config")
def upload_config(user: User = Depends(get_current_user)):
    """Upload constraints for the frontend — single source of truth, so the UI
    never hardcodes its own list of types or size limits."""
    from shared.config import get_settings
    from shared.knowledge.ingestion.storage import ALLOWED_EXTENSIONS

    settings = get_settings()
    return ok({
        "allowedExtensions": sorted(ALLOWED_EXTENSIONS),
        "maxFileMb": settings.knowledge_max_file_mb,
        "accept": ",".join(f".{e}" for e in sorted(ALLOWED_EXTENSIONS)),
    })


def _resolve_source(db: Session, user: User, source_id: str) -> KnowledgeSource:
    """Load a source with tenant enforcement (404 on cross-tenant access)."""
    source = db.get(KnowledgeSource, source_id)
    if source is None or source.is_deleted:
        raise NotFoundError("Knowledge source")
    if source.scope == "global":
        if not is_super_admin(user):
            raise NotFoundError("Knowledge source")
    elif not is_super_admin(user) and source.tenant_id != user.tenant_id:
        raise NotFoundError("Knowledge source")
    return source


@router.post("/knowledge/{source_id}/documents", status_code=201)
async def upload_document(
    source_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_permission("upload_knowledge_documents", "knowledge.manage")),
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
    user: User = Depends(require_permission("retry_knowledge_ingestion", "knowledge.manage")),
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


@router.get("/knowledge/{source_id}")
async def knowledge_detail(
    source_id: str,
    user: User = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """Complete Knowledge Base detail for the admin/tenant View action.

    Combines the MySQL source row (with tenant/bot names) with live PostgreSQL
    document/chunk statistics. Tenant enforcement happens in _resolve_source —
    non-admin callers can never load another tenant's KB.
    """
    from sqlalchemy import func as sa_func, select as sa_select

    from shared.db.postgres import get_pg_sessionmaker
    from shared.knowledge.models import KnowledgeChunk, KnowledgeDocument
    from shared.models import Tenant, VoiceBot

    source = _resolve_source(db, user, source_id)
    tenant = db.get(Tenant, source.tenant_id) if source.tenant_id else None
    bot = db.get(VoiceBot, source.bot_id) if source.bot_id else None
    creator = db.get(User, source.created_by) if source.created_by else None

    async with get_pg_sessionmaker()() as session:
        docs = (
            await session.execute(
                sa_select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.kb_id == source.id,
                    KnowledgeDocument.is_deleted.is_(False),
                )
                .order_by(KnowledgeDocument.created_at.desc())
            )
        ).scalars().all()
        chunk_stats = (
            await session.execute(
                sa_select(
                    sa_func.count().label("total"),
                    sa_func.count().filter(KnowledgeChunk.embedding.is_not(None)).label("embedded"),
                )
                .select_from(KnowledgeChunk)
                .where(
                    KnowledgeChunk.kb_id == source.id,
                    KnowledgeChunk.status == "active",
                    KnowledgeChunk.is_deleted.is_(False),
                )
            )
        ).one()
        embedding_models = [
            m for (m,) in (
                await session.execute(
                    sa_select(KnowledgeChunk.embedding_model)
                    .where(
                        KnowledgeChunk.kb_id == source.id,
                        KnowledgeChunk.is_deleted.is_(False),
                        KnowledgeChunk.embedding_model.is_not(None),
                    )
                    .distinct()
                )
            ).all()
        ]

    last_error = next((d.failure_reason for d in docs if d.failure_reason), None)
    return ok({
        "id": source.id,
        "name": source.name,
        "description": source.detail or "",
        "type": source.type,
        "scope": source.scope,
        "status": source.status,
        "tenantId": source.tenant_id,
        "tenantName": tenant.name if tenant else ("Platform (global)" if source.scope == "global" else None),
        "botId": source.bot_id,
        "botName": bot.name if bot else None,
        "chunks": source.chunks,
        "sizeKb": source.size_kb,
        "quality": source.quality,
        "usage30d": source.usage_30d,
        "lastSync": source.last_sync_at.isoformat() + "Z" if source.last_sync_at else None,
        "createdAt": source.created_at.isoformat() + "Z" if source.created_at else None,
        "updatedAt": source.updated_at.isoformat() + "Z" if source.updated_at else None,
        "createdBy": creator.name if creator else source.created_by,
        "stats": {
            "documentCount": len(docs),
            "readyDocuments": sum(1 for d in docs if d.status == "ready"),
            "failedDocuments": sum(1 for d in docs if d.status == "failed"),
            "activeChunks": int(chunk_stats.total or 0),
            "embeddedChunks": int(chunk_stats.embedded or 0),
            "embeddingModels": embedding_models,
            "lastError": last_error,
        },
        # Same shape as the documents list endpoint (job stage omitted — this
        # is a snapshot view, not the ingestion progress tracker).
        "documents": [
            _serialize_status(KnowledgeService._status_of(d, None)) for d in docs[:50]
        ],
    })


class RetrievalTestRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[str] | str | None = Field(default=None, alias="kbIds")
    bot_id: str | None = Field(default=None, alias="botId")
    top_k: int = Field(default=6, ge=1, le=20, alias="topK")
    # Test-console override of the answerability threshold (runtime unchanged).
    min_score: float | None = Field(default=None, alias="minScore", ge=0, le=1)

    model_config = {"populate_by_name": True}


@router.post("/knowledge/search-test")
async def search_test(
    body: RetrievalTestRequest,
    user: User = Depends(require_tenant_member),
):
    """Retrieval testing for the studio UI — same service the voice bot uses,
    plus diagnostics and below-threshold near-misses (test console only)."""
    result = await get_knowledge_service().search(
        RetrievalRequest(
            tenant_id=_document_tenant(user),
            kb_ids=body.kb_ids,
            bot_id=body.bot_id,
            query=body.query,
            top_k=body.top_k,
            min_score=body.min_score,
            include_below_threshold=True,
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
            "diagnostics": result.diagnostics,
            "sources": [
                {
                    "kbId": s.kb_id,
                    "documentId": s.document_id,
                    "chunkId": s.chunk_id,
                    "chunkIndex": s.chunk_index,
                    "pageNumber": s.page_number,
                    "section": s.section,
                    "rank": s.rank,
                    "score": round(s.score, 4),
                    "vectorScore": s.vector_score and round(s.vector_score, 4),
                    "keywordScore": s.keyword_score and round(s.keyword_score, 4),
                    "rerankScore": s.rerank_score and round(s.rerank_score, 4),
                    "passedGate": s.passed_gate,
                    "text": s.text[:800],
                    "documentName": s.document_name,
                    "meta": s.meta,
                }
                for s in result.sources
            ],
        }
    )
