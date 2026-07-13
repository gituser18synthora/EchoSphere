"""
Goal Engine — Concrete Handlers

One handler per goal flag in GoalsConfig.
Each handler is registered in GoalEngine.REGISTRY keyed by intent label.

Goal flag          Intent label        Handler
book_appointments  book_appointment    BookAppointmentHandler
capture_leads      capture_lead        CaptureLeadHandler
answer_faqs        answer_faq          AnswerFAQHandler
route_to_human     route_to_human      RouteToHumanHandler
send_sms_followup  send_followup       SendFollowupHandler

Adding a new goal:
  1. Create a handler class here extending BaseGoalHandler
  2. Register it in GoalEngine.REGISTRY
  3. Add the bool flag to GoalsConfig
  4. Map intent → flag in VoicebotConfig.intent_config.goal_flag_map
  Zero orchestrator changes needed.
"""

import logging
from typing import Any

from voicebot.goal_engine.base_handler import BaseGoalHandler
from voicebot.goal_engine.models import SlotDefinition, SlotType
from voicebot.orchestrator.call_state import CallState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Book Appointment
# ---------------------------------------------------------------------------

class BookAppointmentHandler(BaseGoalHandler):
    """
    Collects: date, time, caller_name, contact, purpose.
    Action: writes to CRM calendar API (or logs if not configured).
    """

    goal_name = "book_appointment"

    slot_definitions = [
        SlotDefinition(
            name="date",
            description="Date for the appointment",
            slot_type=SlotType.DATE,
            example="next Tuesday, March 15th",
        ),
        SlotDefinition(
            name="time",
            description="Preferred time for the appointment",
            slot_type=SlotType.TIME,
            example="2pm, morning, 14:00",
        ),
        SlotDefinition(
            name="caller_name",
            description="Caller's full name",
            slot_type=SlotType.NAME,
            example="John Smith",
        ),
        SlotDefinition(
            name="contact",
            description="Phone number or email for confirmation",
            slot_type=SlotType.TEXT,
            example="555-0123 or john@email.com",
        ),
        SlotDefinition(
            name="purpose",
            description="Reason or purpose for the appointment",
            slot_type=SlotType.TEXT,
            required=False,
            example="dental checkup, product demo",
        ),
    ]

    def first_question(self) -> str:
        return (
            "I'd be happy to help you book an appointment! "
            "What date works best for you?"
        )

    def next_question(self, unfilled: list[str]) -> str:
        questions = {
            "date": "What date would you like the appointment?",
            "time": "What time works best for you?",
            "caller_name": "May I have your full name please?",
            "contact": "What's the best phone number or email to confirm the appointment?",
            "purpose": "And what is the appointment for? You can skip this if you prefer.",
        }
        if unfilled:
            return questions.get(unfilled[0], f"Could you share your {unfilled[0]}?")
        return ""

    async def execute_action(
        self,
        slots: dict[str, Any],
        call_state: CallState,
    ) -> dict[str, Any]:
        """
        In production: call CRM calendar API here.
        Currently logs the booking and returns structured result.
        """
        logger.info(
            "[BookAppointment] Booking | call_id=%s | date=%s | time=%s | name=%s",
            call_state.call_id,
            slots.get("date"),
            slots.get("time"),
            slots.get("caller_name"),
        )
        # TODO: integrate with CRM calendar API using call_state config
        return {
            "booked": True,
            "date": slots.get("date"),
            "time": slots.get("time"),
            "name": slots.get("caller_name"),
            "contact": slots.get("contact"),
            "purpose": slots.get("purpose"),
        }

    def confirmation_message(self, slots: dict[str, Any]) -> str:
        date = slots.get("date", "the requested date")
        time = slots.get("time", "the requested time")
        name = slots.get("caller_name", "you")
        return (
            f"Perfect! I've booked your appointment for {date} at {time}, {name}. "
            "You'll receive a confirmation shortly. Is there anything else I can help you with?"
        )


# ---------------------------------------------------------------------------
# 2. Capture Lead
# ---------------------------------------------------------------------------

class CaptureLeadHandler(BaseGoalHandler):
    """
    Collects: name, email, phone, interest.
    Action: pushes lead to CRM (Salesforce / HubSpot / Zoho / custom).
    """

    goal_name = "capture_lead"

    slot_definitions = [
        SlotDefinition(
            name="caller_name",
            description="Caller's full name",
            slot_type=SlotType.NAME,
            example="Jane Doe",
        ),
        SlotDefinition(
            name="email",
            description="Email address",
            slot_type=SlotType.EMAIL,
            example="jane@company.com",
        ),
        SlotDefinition(
            name="phone",
            description="Phone number",
            slot_type=SlotType.PHONE,
            example="555-0199",
        ),
        SlotDefinition(
            name="interest",
            description="What product or service they are interested in",
            slot_type=SlotType.TEXT,
            required=False,
            example="enterprise plan, product demo",
        ),
    ]

    def first_question(self) -> str:
        return (
            "I'd love to get you more information! "
            "Could I start with your full name please?"
        )

    def next_question(self, unfilled: list[str]) -> str:
        questions = {
            "caller_name": "Could I get your full name?",
            "email": "What's the best email address to reach you?",
            "phone": "And your phone number?",
            "interest": "What are you most interested in? You can skip this if you prefer.",
        }
        if unfilled:
            return questions.get(unfilled[0], f"Could you share your {unfilled[0]}?")
        return ""

    async def execute_action(
        self,
        slots: dict[str, Any],
        call_state: CallState,
    ) -> dict[str, Any]:
        """
        In production: push lead to CRM based on crm_integration_type.
        """
        logger.info(
            "[CaptureLead] Lead captured | call_id=%s | name=%s | email=%s",
            call_state.call_id,
            slots.get("caller_name"),
            slots.get("email"),
        )
        # TODO: route to CRM adapter based on config.goals.crm_integration_type
        return {
            "lead_captured": True,
            "name": slots.get("caller_name"),
            "email": slots.get("email"),
            "phone": slots.get("phone"),
            "interest": slots.get("interest"),
        }

    def confirmation_message(self, slots: dict[str, Any]) -> str:
        name = slots.get("caller_name", "there")
        return (
            f"Thank you {name}! A representative will reach out to you shortly. "
            "Is there anything else I can help you with?"
        )


# ---------------------------------------------------------------------------
# 3. Answer FAQ
# ---------------------------------------------------------------------------

class AnswerFAQHandler(BaseGoalHandler):
    """
    Single-turn goal — no slots to collect.
    The answer comes from the knowledge router (RAG/FAQ), not this handler.
    This handler exists so the intent routes through GoalEngine cleanly,
    but it immediately returns stop_pipeline=False so the orchestrator
    falls through to the normal LLM/knowledge path.

    The GoalEngine treats answer_faq specially — it never sets an active
    goal, it just signals: "let the LLM + knowledge router handle this."
    """

    goal_name = "answer_faq"
    slot_definitions = []   # no slots — single turn

    def first_question(self) -> str:
        return ""  # never called for single-turn goals

    def next_question(self, unfilled: list[str]) -> str:
        return ""

    async def execute_action(
        self,
        slots: dict[str, Any],
        call_state: CallState,
    ) -> dict[str, Any]:
        return {"answered": True}

    def confirmation_message(self, slots: dict[str, Any]) -> str:
        return ""  # LLM generates the actual FAQ answer


# ---------------------------------------------------------------------------
# 4. Route to Human
# ---------------------------------------------------------------------------

class RouteToHumanHandler(BaseGoalHandler):
    """
    Optionally collects reason/department, then triggers SIP transfer.
    If no slots needed, transfers immediately.
    """

    goal_name = "route_to_human"

    slot_definitions = [
        SlotDefinition(
            name="reason",
            description="Reason for transfer or department needed",
            slot_type=SlotType.TEXT,
            required=False,
            example="billing, technical support, sales",
        ),
    ]

    def first_question(self) -> str:
        return (
            "Of course! I'll connect you with one of our team members. "
            "Could you briefly tell me what you need help with so I can direct you correctly?"
        )

    def next_question(self, unfilled: list[str]) -> str:
        return "What department or type of help do you need?"

    async def execute_action(
        self,
        slots: dict[str, Any],
        call_state: CallState,
    ) -> dict[str, Any]:
        """
        Signals the telephony layer to perform a SIP REFER / warm transfer.
        In production: trigger SIP transfer via telephony API.
        """
        reason = slots.get("reason", "general")
        logger.info(
            "[RouteToHuman] Transfer triggered | call_id=%s | reason=%s",
            call_state.call_id,
            reason,
        )
        # TODO: integrate with SIP/telephony API for warm transfer
        return {
            "transfer_triggered": True,
            "reason": reason,
            "escalation_type": "route_to_human",
        }

    def confirmation_message(self, slots: dict[str, Any]) -> str:
        return (
            "Connecting you now. Please hold for a moment while I transfer you. "
            "Thank you for your patience!"
        )

    def action_error_message(self) -> str:
        return (
            "I'm sorry, I wasn't able to connect you automatically. "
            "Please call our main line directly and we'll be happy to assist."
        )


# ---------------------------------------------------------------------------
# 5. Send SMS / WhatsApp Follow-up
# ---------------------------------------------------------------------------

class SendFollowupHandler(BaseGoalHandler):
    """
    Collects phone (or uses caller ID) and context, then sends SMS/WhatsApp.
    """

    goal_name = "send_followup"

    slot_definitions = [
        SlotDefinition(
            name="phone",
            description="Phone number to send the follow-up message to",
            slot_type=SlotType.PHONE,
            required=False,  # can default to caller_phone from call_state
            example="555-0123",
        ),
        SlotDefinition(
            name="followup_type",
            description="Type of information to send (pricing, brochure, link, summary)",
            slot_type=SlotType.TEXT,
            required=False,
            example="pricing details, product brochure",
        ),
    ]

    def first_question(self) -> str:
        return (
            "I'll send you the details right away! "
            "Should I send it to the number you're calling from, "
            "or would you prefer a different number?"
        )

    def next_question(self, unfilled: list[str]) -> str:
        questions = {
            "phone": "What number should I send the message to?",
            "followup_type": "What information would you like me to send you?",
        }
        if unfilled:
            return questions.get(unfilled[0], "Could you share any other details?")
        return ""

    async def execute_action(
        self,
        slots: dict[str, Any],
        call_state: CallState,
    ) -> dict[str, Any]:
        """
        In production: call Twilio SMS / WhatsApp API.
        Falls back to caller_phone if no phone slot was provided.
        """
        phone = slots.get("phone") or call_state.caller_phone
        followup_type = slots.get("followup_type", "information")

        logger.info(
            "[SendFollowup] SMS triggered | call_id=%s | to=%s | type=%s",
            call_state.call_id,
            phone,
            followup_type,
        )
        # TODO: integrate with Twilio SMS / WhatsApp API
        return {
            "sms_sent": True,
            "to": phone,
            "followup_type": followup_type,
        }

    def confirmation_message(self, slots: dict[str, Any]) -> str:
        phone = slots.get("phone", "your number")
        return (
            f"Done! I've sent you a message with the details to {phone}. "
            "Is there anything else I can help you with?"
        )

    def action_error_message(self) -> str:
        return (
            "I'm sorry, I wasn't able to send that message. "
            "Would you like me to try again or help you with something else?"
        )