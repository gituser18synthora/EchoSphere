# orchestrator/call_state.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from voicebot.usage.tracker import CallUsageStats


@dataclass
class Turn:
    turn_id: int
    role: str  # "user" | "assistant"
    content: str
    intent: str | None = None
    confidence: float | None = None
    token_count: int = 0
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ActiveGoal:
    goal_name: str
    slots: dict[str, Any]
    started_at_turn: int
    paused: bool = False
    pause_reason: str | None = None

    def all_slots_filled(self) -> bool:
        return all(v is not None for v in self.slots.values())

    def unfilled_slots(self) -> list[str]:
        return [k for k, v in self.slots.items() if v is None]

    def filled_slots(self) -> dict[str, Any]:
        return {k: v for k, v in self.slots.items() if v is not None}


@dataclass
class CallState:
    call_id: str
    voicebot_id: str
    caller_phone: str
    tenant_id: str
    system_prompt: str = ""
    turn_count: int = 0
    detected_language: str = "en"
    sentiment_trend: str = "neutral"
    active_goal: ActiveGoal | None = None
    turns: list[Turn] = field(default_factory=list)
    running_summary: str | None = None
    running_summary_turn: int = 0
    call_start_time: datetime = field(default_factory=datetime.utcnow)
    privacy_deletion_requested: bool = False
    escalation_triggered: bool = False
    escalation_reason: str | None = None
    caller_graph: dict | None = None
    usage: CallUsageStats = field(default_factory=CallUsageStats)

    def call_duration_seconds(self) -> float:
        return (datetime.utcnow() - self.call_start_time).total_seconds()

    def call_duration_minutes(self) -> float:
        return self.call_duration_seconds() / 60

    def add_turn(
        self,
        role: str,
        content: str,
        intent: str | None = None,
        confidence: float | None = None,
    ) -> Turn:
        tc = int(len(content.split()) * 1.3)
        turn = Turn(
            turn_id=len(self.turns),
            role=role,
            content=content,
            intent=intent,
            confidence=confidence,
            token_count=tc,
        )
        self.turns.append(turn)
        return turn

    def transcript_as_dialogue(self) -> str:
        lines = []
        for t in self.turns:
            prefix = "Caller" if t.role == "user" else "Bot"
            lines.append(f"{prefix}: {t.content}")
        return "\n".join(lines)


# --- Result dataclasses ---


@dataclass
class IntentResult:
    intent: str
    confidence: float
    sentiment: str


@dataclass
class RouteDecision:
    route: str  # "goal" | "knowledge" | "escalation"
    intent: str
    goal_name: str | None = None


@dataclass
class KnowledgeResult:
    source: str
    content: str
    confidence: float
    metadata: dict


@dataclass
class GoalStepResult:
    status: str
    next_question: str | None = None
    completion_message: str | None = None
