"""Deterministic hash-based embedding provider for tests and offline development.

Produces stable unit-norm vectors: identical text always embeds identically, and
token overlap yields correlated vectors, which makes similarity assertions
meaningful without any external API.
"""

import hashlib
import math
import re


class MockEmbeddingProvider:
    model = "mock-embedding"

    def __init__(self, dimension: int = 1536) -> None:
        self.dimension = dimension

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        # Unicode-aware tokens (Devanagari etc.); identical to the previous
        # [a-z0-9]+ split for plain-English text, so stored mock embeddings
        # of English chunks stay compatible.
        tokens = re.findall(r"[^\W_]+", text.lower())
        if not tokens:
            tokens = ["empty"]
        for token in tokens:
            digest = hashlib.sha256(token.encode()).digest()
            # Each token contributes to a few pseudo-random dimensions.
            for i in range(0, 12, 4):
                idx = int.from_bytes(digest[i : i + 3], "big") % self.dimension
                sign = 1.0 if digest[i + 3] % 2 else -1.0
                vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    async def embed_query(self, query: str) -> list[float]:
        return self._embed(query)

    async def health_check(self) -> dict:
        return {"ok": True, "model": self.model}
