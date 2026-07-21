"""Vector store abstraction. pgvector is the only production implementation."""

from shared.knowledge.vector_store.base import VectorStore
from shared.knowledge.vector_store.pgvector_store import PgVectorStore

__all__ = ["VectorStore", "PgVectorStore"]
