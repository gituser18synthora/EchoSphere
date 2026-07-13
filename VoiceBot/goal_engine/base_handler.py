"""
Goal Engine — Base Handler

Every goal (BookAppointment, CaptureLead, etc.) extends BaseGoalHandler.
The handler defines: what slots to collect, how to extract them, and
what external action to fire when complete.

Orchestrator never calls handlers directly — it goes through GoalEngine.
"""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from voicebot.adapters.base import LLMAdapter
from voicebot.goal_engine.models import GoalResult, GoalStatus, SlotDefinition
from voicebot.orchestrator.call_state import ActiveGoal, CallState

logger = logging.getLogger(__name__)


class BaseGoalHandler(ABC):
    """
    Abstract base for all goal handlers.

    Lifecycle per call:
        GoalEngine.start()     → creates ActiveGoal, asks first slot question
        GoalEngine.continue()  → extracts slots, asks next question
        GoalEngine.complete()  → all slots filled → execute_action() → confirm
    """

    # ------------------------------------------------------------------ #
    # Subclass contract                                                    #
    # ------------------------------------------------------------------ #

    @property
    @abstractmethod
    def goal_name(self) -> str:
        """Unique identifier — matches intent label. e.g. 'book_appointment'."""

    @property
    @abstractmethod
    def slot_definitions(self) -> list[SlotDefinition]:
        """
        Ordered list of slots to collect.
        Order matters — first unfilled slot is asked first.
        """

    @abstractmethod
    async def execute_action(
        self,
        slots: dict[str, Any],
        call_state: CallState,
    ) -> dict[str, Any]:
        """
        Called once all slots are filled.
        Performs the external action (CRM write, calendar booking, SMS send...).
        Returns a dict that is stored in GoalResult.action_result.
        Raise exceptions freely — GoalEngine catches and handles them.
        """

    @abstractmethod
    def confirmation_message(self, slots: dict[str, Any]) -> str:
        """
        Human-readable confirmation spoken to the caller after execute_action().
        e.g. "Great! I've booked your appointment for Tuesday at 2pm."
        """

    # ------------------------------------------------------------------ #
    # Optional overrides                                                   #
    # ------------------------------------------------------------------ #

    def first_question(self) -> str:
        """
        Opening question when the goal is first activated.
        Defaults to asking for the first required slot.
        Override to provide a more natural opener.
        """
        first = next(
            (s for s in self.slot_definitions if s.required), None
        )
        if first:
            return f"I'd be happy to help! Could you please share your {first.description.lower()}?"
        return "I'd be happy to help! Could you provide more details?"

    def next_question(self, unfilled: list[str]) -> str:
        """
        Question to ask for the next unfilled slot.
        Default asks for the first slot in the unfilled list.
        Override for more natural multi-slot questions.
        """
        if not unfilled:
            return ""
        slot_name = unfilled[0]
        slot_def = next((s for s in self.slot_definitions if s.name == slot_name), None)
        desc = slot_def.description if slot_def else slot_name.replace("_", " ")
        return f"Could you please provide your {desc.lower()}?"

    def action_error_message(self) -> str:
        """
        Spoken when execute_action() raises an exception.
        Override to customize per goal.
        """
        return (
            "I'm sorry, I ran into a small issue completing that. "
            "Let me connect you with someone who can help directly."
        )

    # ------------------------------------------------------------------ #
    # Slot extraction (shared, LLM-driven)                                 #
    # ------------------------------------------------------------------ #

    async def extract_slots(
        self,
        llm: LLMAdapter,
        text: str,
        active_goal: ActiveGoal,
        call_state: "CallState | None" = None,
    ) -> tuple[dict[str, Any], bool]:
        """
        Use LLM to extract slot values from caller's utterance.
        Returns (extracted_values_dict, is_off_topic).

        extracted_values_dict: {slot_name: value_or_None}
        is_off_topic: True if caller seems to be asking something unrelated.
        """
        filled = active_goal.filled_slots()
        unfilled_names = active_goal.unfilled_slots()

        # Build slot descriptions for unfilled slots only
        unfilled_defs = [
            s for s in self.slot_definitions if s.name in unfilled_names
        ]
        unfilled_block = "\n".join(
            f"- {s.name}: {s.description}"
            + (f" (e.g. {s.example})" if s.example else "")
            for s in unfilled_defs
        )

        filled_block = (
            "\n".join(f"- {k}: {v}" for k, v in filled.items())
            if filled
            else "None yet"
        )

        # Build the response template outside the f-string to avoid
        # nested f-string quoting issues (SyntaxError in Python < 3.12).
        slots_template = ", ".join(f'"{n}": null' for n in unfilled_names)
        response_template = f'{{"extracted": {{{slots_template}}}, "off_topic": false}}'

        prompt = (
            f"You are extracting information from a caller's response for: {self.goal_name}\n\n"
            f"Information still needed:\n{unfilled_block}\n\n"
            f"Information already collected:\n{filled_block}\n\n"
            f"Caller said: \"{text}\"\n\n"
            "Instructions:\n"
            "- Extract any information that matches the unfilled slots\n"
            "- Return ONLY valid JSON. No markdown. No explanation.\n"
            "- Use null for any slot not mentioned in this utterance\n"
            "- If the caller seems to be changing topic or asking something unrelated, "
            "set \"off_topic\" to true\n"
            "- Normalize dates to YYYY-MM-DD format, times to HH:MM 24h format\n"
            "- Normalize phone numbers: digits only, no spaces/dashes\n\n"
            f"Respond with:\n{response_template}"
        )

        try:
            response = await llm.generate(
                messages=[{"role": "user", "content": prompt}],
                system_prompt="You are a slot extractor. Return only valid JSON. No markdown.",
                max_tokens=150,
                temperature=0.0,
            )
            if call_state is not None:
                call_state.usage.record_llm(response)
            raw = (response.text or "").strip()
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]

            data = json.loads(raw.strip())
            extracted = data.get("extracted", {})
            off_topic = bool(data.get("off_topic", False))

            # Keep only slots that actually exist in our definition
            valid = {
                k: v for k, v in extracted.items()
                if k in unfilled_names and v is not None
            }
            return valid, off_topic

        except Exception as e:
            logger.error("[GoalHandler] Slot extraction failed: %s", e)
            return {}, False