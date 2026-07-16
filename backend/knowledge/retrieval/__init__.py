"""Hybrid retrieval: dense pgvector + keyword FTS, fused and confidence-gated."""

from backend.knowledge.retrieval.retriever import HybridRetriever

__all__ = ["HybridRetriever"]
