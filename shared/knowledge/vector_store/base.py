"""VectorStore protocol — keeps retrieval business logic storage-agnostic.

Only pgvector is implemented in this phase; the protocol exists so a future
store can be swapped in without touching retrieval or service code.
"""

from typing import Protocol

from shared.knowledge.schemas import ChunkPayload, SourceRef


class VectorStore(Protocol):
    async def upsert_chunks(self, chunks: list[ChunkPayload]) -> int:
        """Idempotently insert/update chunks (batched). Returns rows written."""
        ...

    async def dense_search(
        self,
        *,
        tenant_id: str | None,
        kb_ids: list[str],
        query_embedding: list[float],
        limit: int,
        include_global: bool = True,
    ) -> list[SourceRef]:
        ...

    async def keyword_search(
        self,
        *,
        tenant_id: str | None,
        kb_ids: list[str],
        query: str,
        limit: int,
        include_global: bool = True,
    ) -> list[SourceRef]:
        ...

    async def delete_document(self, tenant_id: str | None, document_id: str) -> int:
        ...

    async def delete_knowledge_base(self, tenant_id: str | None, kb_id: str) -> int:
        ...

    async def health_check(self) -> dict:
        ...
