"""Orchestrator package: VoiceBotOrchestrator, call state, pipeline, intent, guardrails."""

from typing import TYPE_CHECKING

from orchestrator.call_state import (
    CallState,
    Turn,
    ActiveGoal,
    IntentResult,
    RouteDecision,
    KnowledgeResult,
    GoalStepResult,
)
from orchestrator.exceptions import (
    OrchestratorNotInitializedError,
    PipelineStepError,
)

if TYPE_CHECKING:
    from orchestrator.orchestrator import VoiceBotOrchestrator

__all__ = [
    "VoiceBotOrchestrator",
    "CallState",
    "Turn",
    "ActiveGoal",
    "IntentResult",
    "RouteDecision",
    "KnowledgeResult",
    "GoalStepResult",
    "OrchestratorNotInitializedError",
    "PipelineStepError",
]


def __getattr__(name: str):
    if name == "VoiceBotOrchestrator":
        from orchestrator.orchestrator import VoiceBotOrchestrator

        return VoiceBotOrchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
