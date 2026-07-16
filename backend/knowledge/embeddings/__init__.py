"""Embedding providers. `get_embedding_provider()` returns the configured one."""

from backend.knowledge.embeddings.base import EmbeddingProvider
from backend.knowledge.embeddings.factory import get_embedding_provider

__all__ = ["EmbeddingProvider", "get_embedding_provider"]
