"""Embedding providers. `get_embedding_provider()` returns the configured one."""

from shared.knowledge.embeddings.base import EmbeddingProvider
from shared.knowledge.embeddings.factory import get_embedding_provider

__all__ = ["EmbeddingProvider", "get_embedding_provider"]
