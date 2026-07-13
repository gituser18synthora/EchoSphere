import asyncio
import logging
import os
import uuid
 
from sqlalchemy import select, func, and_, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.engine import URL
 
logger = logging.getLogger(__name__)
 
 
# ==========================================
#   DB ENGINE  (reads from env, same as agent assist)
# ==========================================
 
def _build_pg_engine():
    """
    Build async Postgres engine from environment variables.
    Mirrors agent assist config.py PG_URL_OBJ construction.
    Add these to your .env:
 
        RAG_PG_HOST=192.168.60.132
        RAG_PG_PORT=5432
        RAG_PG_USER=postgres
        RAG_PG_PASSWORD=postgres
        RAG_PG_DATABASE=AI RAG
        RAG_EMBEDDING_MODEL=text-embedding-3-small
    """
    pg_url = URL.create(
        "postgresql+asyncpg",
        username=os.getenv("RAG_PG_USER", "postgres"),
        password=os.getenv("RAG_PG_PASSWORD", "postgres"),
        host=os.getenv("RAG_PG_HOST", "192.168.60.132"),
        port=int(os.getenv("RAG_PG_PORT", "5432")),
        database=os.getenv("RAG_PG_DATABASE", "AI RAG"),
    )
    return create_async_engine(pg_url, echo=False)
 
 
_pg_engine = None
_AsyncSessionLocal = None
 
 
def _get_session_factory():
    global _pg_engine, _AsyncSessionLocal
    if _AsyncSessionLocal is None:
        _pg_engine = _build_pg_engine()
        _AsyncSessionLocal = sessionmaker(
            _pg_engine, class_=AsyncSession, expire_on_commit=False
        )
    return _AsyncSessionLocal
 
 
# ==========================================
#   ORM MODELS  (same tables as agent assist)
# ==========================================
 
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import Column, String, Boolean, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from pgvector.sqlalchemy import Vector
 
_Base = declarative_base()
 
 
class _KnowledgeBase(_Base):
    __tablename__ = "knowledge_bases"
    ai_kb_id = Column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id = Column(PG_UUID(as_uuid=True), index=True)
    is_active = Column(Boolean, default=True)
 
 
class _RagChunk(_Base):
    __tablename__ = "rag_chunks"
    chunk_id = Column(String, primary_key=True)
    text_content = Column(String)
    embeddings = relationship("_RagEmbedding", back_populates="chunk")
 
 
class _RagEmbedding(_Base):
    __tablename__ = "rag_embeddings"
    id = Column(Integer, primary_key=True, autoincrement=True)
    chunk_id = Column(String, ForeignKey("rag_chunks.chunk_id"))
    ai_kb_id = Column(PG_UUID(as_uuid=True))
    embedding = Column(Vector(1536))
    chunk = relationship("_RagChunk", back_populates="embeddings")
 
 
# ==========================================
#   PGVECTOR RAG ADAPTER
# ==========================================
 
EMBEDDING_MODEL = os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small")
 
 
class PGVectorRAGAdapter:
    """
    Drop-in RAG adapter for the voicebot orchestrator.
 
    Usage in orchestrator __init__:
        from voicebot.adapters.rag import PGVectorRAGAdapter
        self.rag_adapter = PGVectorRAGAdapter(
            tenant_id=config.tenant_id,
            openai_api_key=settings.openai_api_key,
            enabled=config.engine.enable_rag,
        )
 
    Usage in _build_messages():
        context = await self.rag_adapter.search(current_text)
    """
 
    def __init__(
        self,
        tenant_id: str,
        openai_api_key: str,
        enabled: bool = True,
        top_k: int = 3,
    ):
        self.tenant_id = tenant_id
        self.enabled = enabled
        self.top_k = top_k
        self._kb_ids: list = []         # cached per-instance after first resolve
        self._openai_api_key = openai_api_key
        self._openai_client = None      # lazy init to avoid import cost at startup
 
    # ------------------------------------------------------------------
    #   PUBLIC API
    # ------------------------------------------------------------------
 
    async def search(self, query: str) -> str:
        """
        Retrieve relevant knowledge-base chunks for a caller utterance.
        Returns a formatted string ready to inject into the system prompt,
        or an empty string if RAG is disabled / nothing found.
 
        Mirrors agent assist trigger_analysis():
            relevant_policy = await rag_engine.search(current_text)
            policy_context = "\\n---\\n".join(relevant_policy) if relevant_policy else "No specific documents found."
        """
        if not self.enabled:
            return ""
 
        if not query or len(query.strip()) < 3:
            return ""
 
        try:
            chunks = await self._hybrid_search(query, self.top_k)
            if not chunks:
                return ""
            return "\n---\n".join(chunks)
        except Exception as e:
            logger.error("[RAG] search failed: %s", e)
            return ""
 
    # ------------------------------------------------------------------
    #   INTERNAL: EMBEDDING
    # ------------------------------------------------------------------
 
    def _get_openai_client(self):
        if self._openai_client is None:
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(api_key=self._openai_api_key)
        return self._openai_client
 
    async def _get_embedding(self, query: str) -> list[float]:
        try:
            client = self._get_openai_client()
            resp = await client.embeddings.create(
                input=query.replace("\n", " "),
                model=EMBEDDING_MODEL,
            )
            return resp.data[0].embedding
        except Exception as e:
            logger.error("[RAG] embedding error: %s", e)
            return []
 
    # ------------------------------------------------------------------
    #   INTERNAL: KB RESOLUTION  (same logic as agent assist)
    # ------------------------------------------------------------------
 
    async def _resolve_kb_ids(self, session: AsyncSession) -> list:
        """
        Resolve active knowledge-base IDs for this tenant.
        Cached on the instance after first call — same as agent assist.
        """
        if self._kb_ids:
            return self._kb_ids
 
        target_tid = self.tenant_id
        if isinstance(target_tid, str):
            try:
                target_tid = uuid.UUID(target_tid)
            except ValueError:
                logger.error("[RAG] invalid tenant_id UUID: %s", self.tenant_id)
                return []
 
        stmt = select(_KnowledgeBase.ai_kb_id).where(
            and_(
                _KnowledgeBase.tenant_id == target_tid,
                _KnowledgeBase.is_active == True,
            )
        )
        result = await session.execute(stmt)
        ids = [row[0] for row in result.all()]
 
        if not ids:
            logger.warning("[RAG] no active KBs for tenant %s", self.tenant_id)
            return []
 
        self._kb_ids = ids
        logger.info("[RAG] loaded %s KB(s) for tenant %s", len(ids), self.tenant_id)
        return self._kb_ids
 
    # ------------------------------------------------------------------
    #   INTERNAL: HYBRID SEARCH  (exact port from agent assist)
    # ------------------------------------------------------------------
 
    async def _hybrid_search(self, query: str, top_k: int) -> list[str]:
        """
        Hybrid vector + BM25 search, alpha=0.5.
        Identical logic to agent assist PGVectorRAG.search().
        """
        AsyncSessionLocal = _get_session_factory()
 
        async with AsyncSessionLocal() as session:
            kb_ids = await self._resolve_kb_ids(session)
            if not kb_ids:
                return []
 
            query_embedding = await self._get_embedding(query)
            if not query_embedding:
                return []
 
            # --- Vector similarity search ---
            vector_stmt = (
                select(
                    _RagChunk.chunk_id,
                    (1 - _RagEmbedding.embedding.cosine_distance(query_embedding)).label("similarity"),
                    _RagChunk.text_content,
                )
                .join(_RagEmbedding, _RagChunk.chunk_id == _RagEmbedding.chunk_id)
                .where(_RagEmbedding.ai_kb_id.in_(kb_ids))
                .order_by(text("similarity DESC"))
                .limit(top_k * 2)
            )
 
            # --- BM25 full-text search ---
            ts_query = func.plainto_tsquery("english", query)
            ts_vector = func.to_tsvector("english", _RagChunk.text_content)
            bm25_stmt = (
                select(
                    _RagChunk.chunk_id,
                    func.ts_rank_cd(ts_vector, ts_query).label("score"),
                    _RagChunk.text_content,
                )
                .join(_RagEmbedding, _RagChunk.chunk_id == _RagEmbedding.chunk_id)
                .where(
                    and_(
                        _RagEmbedding.ai_kb_id.in_(kb_ids),
                        ts_vector.op("@@")(ts_query),
                    )
                )
                .order_by(text("score DESC"))
                .limit(top_k * 2)
            )
 
            try:
                vec_res, bm25_res = await asyncio.gather(
                    session.execute(vector_stmt),
                    session.execute(bm25_stmt),
                )
 
                vec_rows = vec_res.all()
                bm25_rows = bm25_res.all()
 
                alpha = 0.5
                combined: dict[str, dict] = {}
 
                for row in vec_rows:
                    cid = str(row[0])
                    combined[cid] = {
                        "text": row[2],
                        "score": float(row[1]) * alpha,
                    }
 
                for row in bm25_rows:
                    cid = str(row[0])
                    score = float(row[1])
                    if cid in combined:
                        combined[cid]["score"] += score * (1 - alpha)
                    else:
                        combined[cid] = {
                            "text": row[2],
                            "score": score * (1 - alpha),
                        }
 
                final = sorted(
                    combined.values(), key=lambda x: x["score"], reverse=True
                )[:top_k]
 
                return [d["text"] for d in final]
 
            except Exception as e:
                logger.error("[RAG] hybrid search execution failed: %s", e)
                return []