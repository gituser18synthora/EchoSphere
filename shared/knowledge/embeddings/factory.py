"""Embedding provider factory — provider chosen by configuration.

Availability is still governed by the platform catalog: a provider that is
inactive in ``provider_defs`` (kind=embedding) is refused even when the
environment configuration names it.
"""

import logging
from functools import lru_cache

from shared.config import get_settings
from shared.errors import ProviderNotAvailableError
from shared.knowledge.embeddings.base import EmbeddingProvider

logger = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _cached(provider_name: str, model: str, dimension: int) -> EmbeddingProvider:
    if provider_name == "openai":
        from shared.knowledge.embeddings.openai_provider import OpenAIEmbeddingProvider

        return OpenAIEmbeddingProvider(model=model, dimension=dimension)
    if provider_name == "mock":
        from shared.knowledge.embeddings.mock_provider import MockEmbeddingProvider

        return MockEmbeddingProvider(dimension=dimension)
    raise ValueError(f"Unknown embedding provider: {provider_name!r}")


def _governance_check(provider_name: str) -> None:
    """Refuse embedding providers deactivated under platform governance.

    Fails open only when the control-plane catalog is unreachable or not yet
    seeded (fresh dev checkouts); an explicit inactive row always refuses.
    """
    if provider_name == "mock":
        if get_settings().app_env == "production":
            raise ProviderNotAvailableError(
                "The mock embedding provider is not available in production."
            )
        return
    status = None
    try:
        from sqlalchemy import select

        from shared.db.mysql import get_sessionmaker
        from shared.models import ProviderDef

        session = get_sessionmaker()()
        try:
            status = session.execute(
                select(ProviderDef.status).where(
                    ProviderDef.kind == "embedding",
                    ProviderDef.code == provider_name,
                    ProviderDef.is_deleted.is_(False),
                )
            ).scalar_one_or_none()
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — catalog unavailable: bounded fail-open
        logger.warning("embedding governance check skipped (catalog unavailable)")
        return
    if status is not None and status != "active":
        raise ProviderNotAvailableError(
            f"Embedding provider '{provider_name}' is inactive under platform governance."
        )


def get_embedding_provider(
    provider: str | None = None,
    model: str | None = None,
    dimension: int | None = None,
) -> EmbeddingProvider:
    settings = get_settings()
    provider_name = provider or settings.embedding_provider
    _governance_check(provider_name)
    return _cached(
        provider_name,
        model or settings.embedding_model,
        dimension or settings.embedding_dimension,
    )
