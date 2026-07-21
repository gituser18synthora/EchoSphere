"""Optional cross-encoder reranker (sentence-transformers).

Loaded lazily; the package is an optional heavy dependency (pulls torch).
Enable with RETRIEVAL_USE_RERANKER=true after installing:
    pip install sentence-transformers
"""

import asyncio
import logging
import threading

from shared.knowledge.schemas import SourceRef

logger = logging.getLogger(__name__)

_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-12-v2"
_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import CrossEncoder

                _model = CrossEncoder(_MODEL_NAME)
    return _model


def _score(query: str, sources: list[SourceRef]) -> list[float]:
    model = _get_model()
    return list(model.predict([(query, s.text) for s in sources]))


async def rerank(query: str, sources: list[SourceRef]) -> list[SourceRef]:
    if not sources:
        return sources
    scores = await asyncio.to_thread(_score, query, sources)
    ranked = sorted(zip(sources, scores, strict=True), key=lambda pair: -pair[1])
    return [src for src, _ in ranked]
