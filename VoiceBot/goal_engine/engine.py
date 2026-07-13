"""
Goal Engine — Core Engine
 
Single entry point for the orchestrator.
Owns the registry of all handlers, manages the active goal lifecycle,
and drives the slot-filling loop.
 
Orchestrator integration:
    # At __init__:
    self.goal_engine = GoalEngine(self.llm_adapter, config)
 
    # In handle_utterance(), BEFORE the normal LLM path:
    goal_result = await self.goal_engine.handle_turn(text, self.call_state)
    if goal_result.stop_pipeline:
        audio = await self._speak(goal_result.response_text)
        await self._save_turn(text, goal_result.response_text, ...)
        return audio
    # else: fall through to normal LLM path
"""
import logging
from typing import Any
 
from voicebot.adapters.base import LLMAdapter
from voicebot.config_layer.models import VoicebotConfig
from voicebot.goal_engine.base_handler import BaseGoalHandler
from voicebot.goal_engine.handlers import (
    AnswerFAQHandler,
    BookAppointmentHandler,
    CaptureLeadHandler,
    RouteToHumanHandler,
    SendFollowupHandler,
)
from voicebot.goal_engine.models import GoalResult, GoalStatus
from voicebot.orchestrator.call_state import ActiveGoal, CallState
from voicebot.orchestrator.system_prompt import update_goal_context
 
logger = logging.getLogger(__name__)

_GOAL_COMPLETION_SIGNALS: dict[str, list[str]] = {
    "book_appointment": ["action_appointment_booking", "scheduled"],
    "capture_lead":     ["lead_captured", "has_fact"],
}

_GOAL_SLOT_GRAPH_MAP: dict[str, dict[str, list[str]]] = {
    "book_appointment": {
        "date":        ["fact_appointment_date"],
        "time":        ["fact_appointment_time"],
        "caller_name": ["person_caller"],
        "contact":     ["fact_phone", "fact_email"],
        "purpose":     ["fact_appointment_purpose"],
    },
    "capture_lead": {
        "caller_name": ["person_caller"],
        "email":       ["fact_email"],
        "phone":       ["fact_phone"],
        "interest":    ["fact_interest"],
    },
}
 
# ---------------------------------------------------------------------------
# Registry — maps intent label → handler class
# To add a new goal: add handler to handlers.py, register here. Done.
# ---------------------------------------------------------------------------
_HANDLER_REGISTRY: dict[str, type[BaseGoalHandler]] = {
    "book_appointment": BookAppointmentHandler,
    "capture_lead": CaptureLeadHandler,
    "answer_faq": AnswerFAQHandler,
    "route_to_human": RouteToHumanHandler,
    "send_followup": SendFollowupHandler,
}
 
# Single-turn goals: no slot filling — always fall through to LLM path.
_SINGLE_TURN_GOALS = {"answer_faq"}


def _goal_flag_enabled(goals: Any, flag_name: str | None) -> bool:
    """
    intent_config.goal_flag_map may reference legacy plural "capture_leads"
    while GoalsConfig exposes capture_lead (singular). Resolve both.
    """
    if not flag_name:
        return False
    if getattr(goals, flag_name, False):
        return True
    if flag_name == "capture_leads":
        return bool(getattr(goals, "capture_lead", False))
    return False


class GoalEngine:
    """
    Manages the full multi-turn goal lifecycle for one call.
 
    State is held entirely in call_state.active_goal — the engine itself
    is stateless across turns. This makes it safe for concurrent calls.
 
    Turn flow (called by orchestrator on every utterance):
 
    ┌─ No active goal ─────────────────────────────────────────────────────┐
    │  intent maps to enabled goal? → start_goal() → ask first question   │
    │  intent is single-turn goal? → return stop_pipeline=False (LLM)     │
    │  intent has no goal mapping? → return stop_pipeline=False (LLM)     │
    └───────────────────────────────────────────────────────────────────────┘
 
    ┌─ Active goal ─────────────────────────────────────────────────────────┐
    │  extract slots from utterance                                         │
    │  off_topic? → pause goal → return stop_pipeline=False (LLM handles)  │
    │  merge new slots into active_goal.slots                               │
    │  all required slots filled? → execute_action → confirm → complete    │
    │  still unfilled? → ask next question → stop_pipeline=True            │
    └───────────────────────────────────────────────────────────────────────┘
    """
 
    def __init__(self, llm_adapter: LLMAdapter, config: VoicebotConfig):
        self._llm = llm_adapter
        self._config = config
 
        # Instantiate only enabled handlers — saves memory and avoids
        # accidentally routing to a disabled goal.
        goals = config.goals
        flag_map = config.intent_config.goal_flag_map  # intent → flag name
        self._handlers: dict[str, BaseGoalHandler] = {}
 
        for intent_label, handler_cls in _HANDLER_REGISTRY.items():
            flag_name = flag_map.get(intent_label)
            if flag_name and _goal_flag_enabled(goals, flag_name):
                self._handlers[intent_label] = handler_cls()
                logger.debug("[GoalEngine] Handler registered: %s", intent_label)
 
        logger.info(
            "[GoalEngine] Initialized | enabled_goals=%s",
            list(self._handlers.keys()),
        )
 
    # ------------------------------------------------------------------
    # Public API — called by orchestrator
    # ------------------------------------------------------------------
 
    def is_goal_intent(self, intent: str) -> bool:
        """True if this intent maps to an enabled goal handler."""
        return intent in self._handlers
 
    async def handle_turn(
        self,
        text: str,
        intent: str,
        call_state: CallState,
    ) -> GoalResult:
        """
        Main entry point — called every turn by the orchestrator.
 
        Returns GoalResult. Orchestrator checks stop_pipeline:
          True  → speak response_text, save turn, return (skip LLM)
          False → continue normal LLM path
        """
        # --- Resume paused goal if caller seems back on track ---
        if (
            call_state.active_goal
            and call_state.active_goal.paused
            and intent == call_state.active_goal.goal_name
        ):
            call_state.active_goal.paused = False
            call_state.active_goal.pause_reason = None
            logger.info(
                "[GoalEngine] Resuming paused goal: %s",
                call_state.active_goal.goal_name,
            )
 
        # --- Active goal: continue slot filling ---
        if call_state.active_goal and not call_state.active_goal.paused:
            return await self._continue_goal(text, call_state)
 
        # --- No active goal: check if intent should start one ---
        if intent in self._handlers:
            # Single-turn goals (answer_faq): never start an active goal;
            # let the LLM + knowledge router handle them naturally.
            if intent in _SINGLE_TURN_GOALS:
                logger.info("[GoalEngine] Single-turn goal, passing to LLM: %s", intent)
                return GoalResult(
                    status=GoalStatus.COMPLETED,
                    response_text="",
                    stop_pipeline=False,
                    goal_name=intent,
                )
            return await self._start_goal(intent, call_state)
 
        # --- No goal match: let orchestrator continue normally ---
        return GoalResult(
            status=GoalStatus.CANCELLED,
            response_text="",
            stop_pipeline=False,
        )
 
    # ------------------------------------------------------------------
    # Internal — goal lifecycle
    # ------------------------------------------------------------------

    def _goal_already_completed(self, intent: str, caller_graph: dict) -> bool:
        """
        Returns True if the caller graph has clear evidence this goal
        was fully completed in a previous call.
        Checks node_ids and edge relations against _GOAL_COMPLETION_SIGNALS.
        """
        signals = _GOAL_COMPLETION_SIGNALS.get(intent, [])
        if not signals:
            return False
        nodes = {n["node_id"]: n for n in caller_graph.get("nodes", [])}
        edges = caller_graph.get("edges", [])
        for node_id in nodes:
            if any(signal in node_id for signal in signals):
                return True
        for edge in edges:
            if edge.get("relation") in signals:
                return True
        return False

    def _prefill_slots_from_graph(
        self,
        intent: str,
        initial_slots: dict,
        caller_graph: dict,
    ) -> dict:
        """
        Returns a dict of slot_name → value for any slots that can be
        filled from prior call data stored in the caller graph nodes.
        Uses _GOAL_SLOT_GRAPH_MAP to match slot names to node_ids.
        """
        slot_map = _GOAL_SLOT_GRAPH_MAP.get(intent, {})
        if not slot_map:
            return {}
        nodes = {n["node_id"]: n for n in caller_graph.get("nodes", [])}
        prefilled = {}
        for slot_name, node_keys in slot_map.items():
            if slot_name not in initial_slots:
                continue
            for key in node_keys:
                node = nodes.get(key)
                if node and node.get("value"):
                    prefilled[slot_name] = node["value"]
                    break
        return prefilled
 
    async def _start_goal(
        self,
        intent: str,
        call_state: CallState,
    ) -> GoalResult:
        handler = self._handlers[intent]
        caller_graph = getattr(call_state, "caller_graph", None)

        # 1. If goal was fully completed in a prior call, skip it entirely.
        #    Return stop_pipeline=False so the LLM handles it naturally
        #    using the caller graph context already in the system prompt.
        if caller_graph and self._goal_already_completed(intent, caller_graph):
            logger.info(
                "[GoalEngine] Goal '%s' already completed in prior call "
                "— skipping | call_id=%s",
                intent, call_state.call_id,
            )
            return GoalResult(
                status=GoalStatus.COMPLETED,
                response_text="",
                stop_pipeline=False,
                goal_name=intent,
            )

        # 2. Build initial slots and pre-fill from caller graph where possible.
        initial_slots = {s.name: None for s in handler.slot_definitions}
        if caller_graph:
            prefilled = self._prefill_slots_from_graph(intent, initial_slots, caller_graph)
            initial_slots.update(prefilled)
            if prefilled:
                logger.info(
                    "[GoalEngine] Pre-filled slots from caller graph "
                    "| goal=%s | slots=%s",
                    intent, prefilled,
                )

        call_state.active_goal = ActiveGoal(
            goal_name=intent,
            slots=initial_slots,
            started_at_turn=call_state.turn_count,
        )
        call_state.system_prompt = update_goal_context(
            call_state.system_prompt,
            call_state.active_goal,
        )

        # 3. If all required slots already pre-filled, skip to action immediately.
        required_unfilled = [
            s.name for s in handler.slot_definitions
            if s.required and initial_slots.get(s.name) is None
        ]
        if not required_unfilled:
            logger.info(
                "[GoalEngine] All required slots pre-filled from prior call "
                "— executing action immediately | goal=%s",
                intent,
            )
            return await self._complete_goal(handler, call_state)

        # 4. Still has unfilled required slots — ask first question as normal.
        first_q = handler.first_question()
        logger.info(
            "[GoalEngine] Goal started | goal=%s | call_id=%s "
            "| pre-filled=%s | still_needed=%s",
            intent, call_state.call_id,
            [k for k, v in initial_slots.items() if v is not None],
            required_unfilled,
        )
        return GoalResult(
            status=GoalStatus.ACTIVE,
            response_text=first_q,
            stop_pipeline=True,
            goal_name=intent,
        )
 
    async def _continue_goal(
        self,
        text: str,
        call_state: CallState,
    ) -> GoalResult:
        """
        Extract slots from caller's utterance and advance the goal.
        Three outcomes:
          1. Off-topic → pause goal, fall through to LLM
          2. More slots needed → ask next question, stop pipeline
          3. All required slots filled → execute action, confirm, complete
        """
        active = call_state.active_goal
        handler = self._handlers.get(active.goal_name)
 
        if not handler:
            # Handler no longer registered (config changed mid-call) — cancel
            logger.warning("[GoalEngine] Handler missing for %s — cancelling", active.goal_name)
            call_state.active_goal = None
            call_state.system_prompt = update_goal_context(call_state.system_prompt, None)
            return GoalResult(
                status=GoalStatus.CANCELLED,
                response_text="",
                stop_pipeline=False,
                goal_name=active.goal_name,
            )
 
        # Extract slots via LLM
        extracted, off_topic = await handler.extract_slots(
            self._llm, text, active, call_state,
        )
 
        # Merge extracted values into active slots
        for slot_name, value in extracted.items():
            if value is not None:
                active.slots[slot_name] = value
                logger.debug(
                    "[GoalEngine] Slot filled | goal=%s | slot=%s | value=%s",
                    active.goal_name, slot_name, value,
                )
 
        # Off-topic: pause goal, let LLM handle this turn naturally
        if off_topic:
            active.paused = True
            active.pause_reason = "off_topic"
            call_state.system_prompt = update_goal_context(
                call_state.system_prompt, active,
            )
            logger.info(
                "[GoalEngine] Goal paused (off-topic) | goal=%s | filled=%s",
                active.goal_name,
                active.filled_slots(),
            )
            return GoalResult(
                status=GoalStatus.PAUSED,
                response_text="",
                stop_pipeline=False,
                goal_name=active.goal_name,
            )
 
        # Update system prompt with new slot state
        call_state.system_prompt = update_goal_context(
            call_state.system_prompt, active,
        )
 
        # Check if all required slots are filled
        required_unfilled = [
            s.name for s in handler.slot_definitions
            if s.required and active.slots.get(s.name) is None
        ]
 
        if required_unfilled:
            # Still needs more slots — ask next question
            next_q = handler.next_question(required_unfilled)
            logger.info(
                "[GoalEngine] Asking for next slot | goal=%s | next=%s",
                active.goal_name, required_unfilled[0],
            )
            return GoalResult(
                status=GoalStatus.ACTIVE,
                response_text=next_q,
                stop_pipeline=True,
                goal_name=active.goal_name,
            )
 
        # All required slots filled — execute the action
        return await self._complete_goal(handler, call_state)
 
    async def _complete_goal(
        self,
        handler: BaseGoalHandler,
        call_state: CallState,
    ) -> GoalResult:
        """
        Fire the external action, speak confirmation, clean up goal state.
        """
        active = call_state.active_goal
        slots = active.slots
 
        try:
            action_result = await handler.execute_action(slots, call_state)
            confirmation = handler.confirmation_message(slots)
 
            logger.info(
                "[GoalEngine] Goal completed | goal=%s | call_id=%s | result=%s",
                active.goal_name,
                call_state.call_id,
                action_result,
            )
 
        except Exception as e:
            logger.error(
                "[GoalEngine] Action failed | goal=%s | error=%s",
                active.goal_name, e,
            )
            action_result = {"error": str(e)}
            confirmation = handler.action_error_message()
 
        # Clear active goal — remove goal context from system prompt
        goal_name = active.goal_name
        call_state.active_goal = None
        call_state.system_prompt = update_goal_context(call_state.system_prompt, None)
 
        return GoalResult(
            status=GoalStatus.COMPLETED,
            response_text=confirmation,
            stop_pipeline=True,
            action_result=action_result,
            goal_name=goal_name,
        )
 
    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------
 
    def get_enabled_goals(self) -> list[str]:
        """Return list of enabled goal intent labels. Used for logging."""
        return list(self._handlers.keys())