"""Typed request/response models shared by REST, MCP and the voice runtime."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RetrievalRequest(BaseModel):
    """A tenant-scoped retrieval request.

    `tenant_id` is ALWAYS resolved server-side (JWT / trusted session mapping) —
    never taken from an untrusted client payload. `kb_ids` semantics:
      - [single id]  → search that KB
      - [id, id...]  → search those KBs (duplicates removed)
      - None / []    → search every active+ready KB the tenant is authorized for
    """

    tenant_id: str | None = None
    kb_ids: list[str] | None = None
    query: str = Field(min_length=1, max_length=2000)
    bot_id: str | None = None
    top_k: int = Field(default=6, ge=1, le=50)
    candidate_k: int = Field(default=24, ge=1, le=200)
    rerank_k: int = Field(default=12, ge=1, le=100)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    language: str | None = None
    include_global: bool = True
    # Diagnostic mode (test console): when nothing clears the relevance gate,
    # return the best below-threshold candidates instead of an empty list so
    # the caller can see *why* retrieval found nothing. Runtime callers keep
    # the default (False) and receive [] — never near-miss context.
    include_below_threshold: bool = False

    @field_validator("kb_ids", mode="before")
    @classmethod
    def _normalize_kb_ids(cls, value: Any) -> list[str] | None:
        """Accept a single string, a list, or None; dedupe preserving order."""
        if value is None:
            return None
        if isinstance(value, str):
            value = [value]
        seen: set[str] = set()
        out: list[str] = []
        for kb in value:
            kb = str(kb).strip()
            if kb and kb not in seen:
                seen.add(kb)
                out.append(kb)
        return out or None


class SourceRef(BaseModel):
    """A cited chunk with provenance metadata (never raw embeddings)."""

    kb_id: str
    document_id: str
    chunk_id: str
    chunk_index: int
    page_number: int | None = None
    section: str | None = None
    topic: str | None = None
    # Final fused score, normalized to [0, 1] for both fusion methods.
    score: float
    vector_score: float | None = None
    keyword_score: float | None = None
    rerank_score: float | None = None
    rank: int | None = None
    passed_gate: bool = True
    text: str
    document_name: str | None = None
    meta: dict | None = None


class RetrievalResult(BaseModel):
    used_knowledge_base: bool
    answerable: bool
    confidence: float
    query: str
    kb_ids: list[str]
    sources: list[SourceRef] = []
    duration_ms: float = 0.0
    skipped_reason: str | None = None
    # Stage counts/timings and the applied thresholds (never chunk content).
    diagnostics: dict | None = None


class IngestionStatus(BaseModel):
    document_id: str
    kb_id: str
    file_name: str
    status: str
    stage: str | None = None
    progress: float = 0.0
    attempts: int = 0
    failure_reason: str | None = None
    chunk_count: int = 0
    page_count: int = 0
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ChunkPayload(BaseModel):
    """Internal transfer object between the ingestion pipeline and vector store."""

    tenant_id: str | None
    kb_id: str
    document_id: str
    chunk_index: int
    content: str
    embedding_text: str | None = None
    content_hash: str
    token_count: int | None = None
    embedding: list[float] | None = None
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    page_number: int | None = None
    section: str | None = None
    topic: str | None = None
    chunk_type: str | None = None
    keywords: list[str] | None = None
    language: str | None = None
    meta: dict | None = None
