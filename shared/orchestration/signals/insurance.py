"""Insurance / policy knowledge vocabulary (seeded demo tenants).

Words that mark a question-shaped utterance as a knowledge lookup for
policy-style products. Vocabulary only — no signals.
"""
from shared.orchestration.signals import SignalPack

PACK = SignalPack(
    name="insurance",
    knowledge_terms=("policy", "policies", "coverage", "premium", "claim", "renewal"),
)
