"""EchoSphere MCP server.

Run: python -m backend.mcp_server.server   (streamable-HTTP on MCP_PORT)

Auth: every request must carry `Authorization: Bearer <platform JWT>` — the
same tokens the REST API issues. Tenant identity comes from the verified
token, NEVER from tool arguments. Cross-tenant access is impossible by
construction: every tool call flows through KnowledgeService.authorize_kb_ids.

Tools:
    list_authorized_knowledge_bases
    search_knowledge
    get_knowledge_source
    get_document_context
"""

import asyncio
import contextvars
import json
import logging
import time
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from shared.config import get_settings
from shared.errors import ApiError, NotFoundError
from backend.core.security import decode_access_token
from shared.knowledge.schemas import RetrievalRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger("backend.mcp")

# Per-request authenticated identity (set by middleware, read by tools).
_current_auth: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "mcp_auth", default=None
)

_TOOL_TIMEOUT_SECONDS = 20.0
_RATE_LIMIT_PER_MINUTE = 60

mcp = FastMCP(
    name="echosphere-knowledge",
    instructions=(
        "Tenant-scoped knowledge retrieval for EchoSphere voice bots. "
        "All results are limited to knowledge bases the authenticated tenant owns."
    ),
    stateless_http=True,
)


def _auth() -> dict:
    auth = _current_auth.get()
    if auth is None:
        raise ApiError("Unauthenticated MCP session", status_code=401)
    return auth


def _tenant_id() -> str | None:
    """Tenant from the verified JWT. Super admins get platform (global) scope."""
    auth = _auth()
    return auth.get("tenant_id")


async def _rate_limit(tenant_id: str | None) -> None:
    from shared.db.redis import get_redis

    key = f"mcp:rate:{tenant_id or '_platform'}:{int(time.time() // 60)}"
    try:
        redis = get_redis()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 90)
        if count > _RATE_LIMIT_PER_MINUTE:
            raise ApiError("Rate limit exceeded", status_code=429)
    except ApiError:
        raise
    except Exception:  # noqa: BLE001 - Redis down must not break MCP
        logger.warning("rate limiter unavailable")


def _audit_sync(tenant_id: str | None, user_id: str | None, tool: str, detail: dict) -> None:
    from shared.ids import new_id
    from shared.db.mysql import get_sessionmaker
    from shared.models import AuditLog

    session = get_sessionmaker()()
    try:
        session.add(
            AuditLog(
                id=new_id("au"),
                tenant_id=tenant_id,
                user_id=user_id,
                actor_name="mcp",
                actor_role="mcp_session",
                action=f"mcp.{tool}",
                entity_type="knowledge",
                entity_id=detail.get("kb_id") or detail.get("document_id"),
                target_label=json.dumps(detail)[:250],
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.warning("mcp audit write failed")
    finally:
        session.close()


async def _guard(tool: str, **detail) -> tuple[str | None, dict]:
    """Common per-tool gate: auth → rate limit → audit. Returns (tenant, auth)."""
    auth = _auth()
    tenant_id = auth.get("tenant_id")
    await _rate_limit(tenant_id)
    await asyncio.to_thread(_audit_sync, tenant_id, auth.get("sub"), tool, detail)
    return tenant_id, auth


def _sanitized(exc: Exception) -> dict:
    """Errors leave the server without SQL, paths or internals."""
    if isinstance(exc, NotFoundError):
        return {"error": "not_found", "message": "Knowledge base or document not found"}
    if isinstance(exc, ApiError):
        return {"error": "request_error", "message": exc.message}
    if isinstance(exc, TimeoutError):
        return {"error": "timeout", "message": "The operation timed out"}
    logger.exception("mcp tool failure")
    return {"error": "internal", "message": "Internal error"}


@mcp.tool()
async def list_authorized_knowledge_bases() -> dict:
    """List the knowledge bases the authenticated tenant may search."""
    try:
        tenant_id, _ = await _guard("list_authorized_knowledge_bases")
        from shared.knowledge.service import KnowledgeService

        rows = await asyncio.to_thread(
            KnowledgeService._query_authorized_kbs, tenant_id, None, None, True
        )
        return {
            "knowledge_bases": [
                {
                    "kb_id": row.id,
                    "name": row.name,
                    "type": row.type,
                    "scope": row.scope,
                    "status": row.status,
                    "chunks": row.chunks,
                }
                for row in rows
            ]
        }
    except Exception as exc:  # noqa: BLE001
        return _sanitized(exc)


@mcp.tool()
async def search_knowledge(
    query: str,
    kb_id: str | list[str] | None = None,
    top_k: int = 6,
) -> dict:
    """Search the tenant's knowledge bases. kb_id may be one id, a list, or
    omitted to search every authorized knowledge base."""
    try:
        if not query or not query.strip():
            raise ApiError("query must not be empty", status_code=422)
        if not 1 <= int(top_k) <= 20:
            raise ApiError("top_k must be between 1 and 20", status_code=422)
        tenant_id, _ = await _guard("search_knowledge", kb_id=str(kb_id))
        from shared.knowledge.service import get_knowledge_service

        request = RetrievalRequest(
            tenant_id=tenant_id,
            kb_ids=kb_id,  # validator normalizes str | list | None
            query=query,
            top_k=int(top_k),
        )
        result = await asyncio.wait_for(
            get_knowledge_service().search(request), timeout=_TOOL_TIMEOUT_SECONDS
        )
        payload = result.model_dump()
        # Raw embeddings are never present in RetrievalResult; keep it that way.
        return payload
    except Exception as exc:  # noqa: BLE001
        return _sanitized(exc)


@mcp.tool()
async def get_knowledge_source(kb_id: str) -> dict:
    """Get one knowledge base's metadata and its document ingestion statuses."""
    try:
        tenant_id, _ = await _guard("get_knowledge_source", kb_id=kb_id)
        from shared.knowledge.service import get_knowledge_service

        service = get_knowledge_service()
        await service.authorize_kb_ids(
            tenant_id=tenant_id, kb_ids=[kb_id], searchable_only=False
        )
        documents = await asyncio.wait_for(
            service.list_documents(tenant_id=tenant_id, kb_id=kb_id),
            timeout=_TOOL_TIMEOUT_SECONDS,
        )
        return {
            "kb_id": kb_id,
            "documents": [d.model_dump(mode="json") for d in documents],
        }
    except Exception as exc:  # noqa: BLE001
        return _sanitized(exc)


@mcp.tool()
async def get_document_context(document_id: str, chunk_id: str, window: int = 1) -> dict:
    """Return a chunk plus its neighbors (for citation expansion)."""
    try:
        if not 0 <= int(window) <= 3:
            raise ApiError("window must be between 0 and 3", status_code=422)
        tenant_id, _ = await _guard("get_document_context", document_id=document_id)
        from sqlalchemy import select

        from shared.db.postgres import get_pg_sessionmaker
        from shared.knowledge.models import KnowledgeChunk
        from shared.knowledge.service import get_knowledge_service

        # Ownership check through the document's KB.
        doc = await get_knowledge_service().get_document(
            tenant_id=tenant_id, document_id=document_id
        )
        async with get_pg_sessionmaker()() as session:
            anchor = (
                await session.execute(
                    select(KnowledgeChunk).where(
                        KnowledgeChunk.id == chunk_id,
                        KnowledgeChunk.document_id == document_id,
                        KnowledgeChunk.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if anchor is None:
                raise NotFoundError("Chunk")
            neighbors = (
                await session.execute(
                    select(KnowledgeChunk)
                    .where(
                        KnowledgeChunk.document_id == document_id,
                        KnowledgeChunk.chunk_index.between(
                            anchor.chunk_index - int(window),
                            anchor.chunk_index + int(window),
                        ),
                        KnowledgeChunk.is_deleted.is_(False),
                        KnowledgeChunk.status == "active",
                    )
                    .order_by(KnowledgeChunk.chunk_index)
                )
            ).scalars().all()
        return {
            "document_id": document_id,
            "file_name": doc.file_name,
            "chunks": [
                {
                    "chunk_id": c.id,
                    "chunk_index": c.chunk_index,
                    "page_number": c.page_number,
                    "section": c.section,
                    "content": c.content,
                }
                for c in neighbors
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return _sanitized(exc)


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """Validates the platform JWT and stashes identity for the tool layer."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        try:
            payload = decode_access_token(header.split(" ", 1)[1].strip())
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid or expired token"}, status_code=401)
        token = _current_auth.set(
            {"sub": payload.get("sub"), "tenant_id": payload.get("tenant_id"),
             "role": payload.get("role")}
        )
        try:
            return await call_next(request)
        finally:
            _current_auth.reset(token)


def build_app():
    app = mcp.streamable_http_app()
    app.add_middleware(JWTAuthMiddleware)

    async def health(request):
        from shared.db.postgres import pg_health_check

        return JSONResponse({"status": "up", "postgres": await pg_health_check()})

    from starlette.routing import Route

    app.router.routes.insert(0, Route("/health", health, methods=["GET"]))
    return app


app = build_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    if not settings.mcp_enabled:
        raise SystemExit("MCP_ENABLED=false — refusing to start")
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port, log_level="info")


if __name__ == "__main__":
    main()
