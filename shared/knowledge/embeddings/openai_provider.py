"""OpenAI embedding provider — batched, async, with dimension enforcement."""

import logging

from openai import AsyncOpenAI

from shared.config import get_settings

logger = logging.getLogger(__name__)


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        dimension: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        settings = get_settings()
        key = api_key or settings.resolve_secret(settings.embedding_api_key_reference)
        if not key:
            raise ValueError(
                "OpenAI embedding provider requires an API key "
                "(EMBEDDING_API_KEY_REFERENCE, default env:OPENAI_API_KEY)"
            )
        self._client = AsyncOpenAI(api_key=key)
        self.model = model or settings.embedding_model
        self.dimension = dimension or settings.embedding_dimension
        self.batch_size = batch_size or settings.embedding_batch_size

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results: list[list[float]] = []
        # Real batching: never send an unbounded list in one request.
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            # The API rejects empty strings; substitute a single space and let
            # the caller decide whether to keep the (meaningless) vector.
            safe_batch = [t if t.strip() else " " for t in batch]
            response = await self._client.embeddings.create(model=self.model, input=safe_batch)
            vectors = [item.embedding for item in response.data]
            for vec in vectors:
                if len(vec) != self.dimension:
                    raise ValueError(
                        f"Embedding dimension mismatch: model {self.model} returned "
                        f"{len(vec)}, expected {self.dimension}"
                    )
            results.extend(vectors)
        return results

    async def embed_query(self, query: str) -> list[float]:
        vectors = await self.embed_documents([query])
        return vectors[0]

    async def health_check(self) -> dict:
        try:
            await self.embed_query("health check")
            return {"ok": True, "model": self.model}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": exc.__class__.__name__}
