"""
Goal Engine — Data Models

All dataclasses and enums used across the goal layer.
Imported by handlers, engine, and orchestrator.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class GoalStatus(str, Enum):
    """Lifecycle state of a goal during a call."""
    ACTIVE = "active"        # currently collecting slots
    PAUSED = "paused"        # caller went off-topic; slots saved, waiting to resume
    COMPLETED = "completed"  # all slots filled, action executed
    CANCELLED = "cancelled"  # caller explicitly abandoned or escalated


class SlotType(str, Enum):
    """What kind of value a slot expects — used for extraction hints."""
    TEXT = "text"
    DATE = "date"          # normalized to YYYY-MM-DD
    TIME = "time"          # normalized to HH:MM
    PHONE = "phone"
    EMAIL = "email"
    NAME = "name"
    NUMBER = "number"


@dataclass
class SlotDefinition:
    """
    Defines one piece of information needed to complete a goal.
    Each handler declares its required slots as a list of these.
    """
    name: str                          # e.g. "date", "caller_name"
    description: str                   # shown to LLM for extraction
    slot_type: SlotType = SlotType.TEXT
    required: bool = True
    example: str = ""                  # helps the LLM normalizer


@dataclass
class GoalResult:
    """
    Returned from GoalEngine.handle_turn() on every utterance
    when a goal is active. Orchestrator reads this to decide
    what to speak and whether to continue the normal LLM path.
    """
    status: GoalStatus

    # Text the bot should speak this turn (question, confirmation, error).
    # Empty string means: no goal-specific response — continue normal LLM path.
    response_text: str = ""

    # Whether the orchestrator should STOP the normal pipeline this turn.
    # True  = goal handled this turn; speak response_text, save turn, done.
    # False = goal is paused / completed; continue to LLM for a general response.
    stop_pipeline: bool = False

    # Filled when status == COMPLETED — structured data for CRM / SMS / calendar.
    action_result: dict[str, Any] = field(default_factory=dict)

    # Internal — which goal name this result came from (for logging).
    goal_name: str = ""