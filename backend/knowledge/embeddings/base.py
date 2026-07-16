"""Embedding provider interface."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Async embedding provider. Implementations must be safe to share across tasks."""

    model: str
    dimension: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of document chunks. Must preserve input order."""
        ...

    async def embed_query(self, query: str) -> list[float]:
        """Embed a single search query."""
        ...

    async def health_check(self) -> dict:
        ...
