"""Embedding provider factory — provider chosen by configuration."""

from functools import lru_cache

from shared.config import get_settings
from shared.knowledge.embeddings.base import EmbeddingProvider


@lru_cache(maxsize=4)
def _cached(provider_name: str, model: str, dimension: int) -> EmbeddingProvider:
    if provider_name == "openai":
        from shared.knowledge.embeddings.openai_provider import OpenAIEmbeddingProvider

        return OpenAIEmbeddingProvider(model=model, dimension=dimension)
    if provider_name == "mock":
        from shared.knowledge.embeddings.mock_provider import MockEmbeddingProvider

        return MockEmbeddingProvider(dimension=dimension)
    raise ValueError(f"Unknown embedding provider: {provider_name!r}")


def get_embedding_provider(
    provider: str | None = None,
    model: str | None = None,
    dimension: int | None = None,
) -> EmbeddingProvider:
    settings = get_settings()
    return _cached(
        provider or settings.embedding_provider,
        model or settings.embedding_model,
        dimension or settings.embedding_dimension,
    )
