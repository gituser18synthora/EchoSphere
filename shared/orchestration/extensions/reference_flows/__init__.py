"""Reference slot-filling flow: appointment booking (demo tenants).

A hand-built LangGraph kept as a worked example of the builder API. Registered
as ``appointment_booking`` / ``appointment`` with the extensions registry;
the engine itself knows nothing about it.
"""
from __future__ import annotations

import re
from typing import Any

from langgraph.graph import END, StateGraph

from shared.orchestration.extensions import register_graph_builder
from shared.orchestration.workflow_state import WorkflowState


# ── appointment booking: the reference slot-filling workflow ───────────────

_SLOTS: list[tuple[str, str, str]] = [
    # (slot key, question, validation regex)
    ("name", "May I have your full name, please?", r"[A-Za-z][A-Za-z .'-]{1,60}$"),
    ("phone", "What is the best phone number to reach you?", r"(\+?\d[\d ()-]{8,14}\d)"),
    ("date", "What date works best for your appointment?",
     r"\b(\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?|today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}(st|nd|rd|th)?\s+\w+)\b"),
    ("time", "And what time would you prefer?",
     r"\b(\d{1,2}([:.]\d{2})?\s*(am|pm|a\.m\.|p\.m\.)?|morning|afternoon|evening|noon)\b"),
]
_MAX_SLOT_RETRIES = 2

_CONFIRM_YES = re.compile(r"\b(yes|yeah|correct|confirm|right|sure|ok(ay)?|haan)\b", re.I)
_CONFIRM_NO = re.compile(r"\b(no|nope|wrong|change|cancel|nahi)\b", re.I)


def _extract_slot(state: WorkflowState) -> WorkflowState:
    slots = dict(state.get("slots") or {})
    text = state.get("user_text", "").strip()
    pending = state.get("pending_slot")
    retries = state.get("retries", 0)

    if pending and text:
        pattern = next((p for key, _, p in _SLOTS if key == pending), None)
        match = re.search(pattern, text, re.I) if pattern else None
        if match:
            slots[pending] = match.group(0).strip()
            retries = 0
        else:
            retries += 1

    next_slot = next((key for key, _, _ in _SLOTS if key not in slots), None)
    status = "collecting" if next_slot else "confirming"
    if retries > _MAX_SLOT_RETRIES:
        status = "handoff"
    return {
        **state,
        "slots": slots,
        "pending_slot": next_slot,
        "retries": retries,
        "status": status,
    }


def _ask_or_confirm(state: WorkflowState) -> WorkflowState:
    status = state.get("status")
    if status == "handoff":
        return {
            **state,
            "reply": "I'm having trouble capturing that. Let me connect you with a "
                     "colleague who can book this for you.",
        }
    if status == "collecting":
        pending = state.get("pending_slot")
        question = next((q for key, q, _ in _SLOTS if key == pending), "Could you repeat that?")
        retry_prefix = "Sorry, I didn't catch that. " if state.get("retries", 0) > 0 else ""
        return {**state, "reply": f"{retry_prefix}{question}"}
    slots = state.get("slots", {})
    summary = (
        f"Let me confirm: an appointment for {slots.get('name')} on {slots.get('date')} "
        f"at {slots.get('time')}, contact number {slots.get('phone')}. Shall I book it?"
    )
    return {**state, "reply": summary}


def _handle_confirmation(state: WorkflowState) -> WorkflowState:
    text = state.get("user_text", "")
    if _CONFIRM_NO.search(text):
        # Restart collection but keep identity fields (idempotent, auditable).
        return {
            **state,
            "slots": {},
            "pending_slot": _SLOTS[0][0],
            "retries": 0,
            "status": "collecting",
            "reply": f"No problem, let's start over. {_SLOTS[0][1]}",
        }
    if _CONFIRM_YES.search(text):
        return {**state, "status": "executing"}
    return {**state, "reply": "Please say yes to confirm the booking, or no to change it."}


def _execute_booking(state: WorkflowState) -> WorkflowState:
    """The external action. Idempotent: keyed by session, executed once."""
    audit = list(state.get("audit") or [])
    audit.append(
        {
            "action": "appointment_booked",
            "tenant_id": state.get("tenant_id"),
            "bot_id": state.get("bot_id"),
            "session_id": state.get("session_id"),
            "slots": state.get("slots"),
        }
    )
    slots = state.get("slots", {})
    return {
        **state,
        "status": "done",
        "audit": audit,
        "reply": (
            f"Your appointment is booked for {slots.get('date')} at {slots.get('time')}. "
            "You'll receive a confirmation shortly. Anything else I can help with?"
        ),
    }


def _route_after_extract(state: WorkflowState) -> str:
    status = state.get("status")
    if status == "handoff":
        return "respond"
    if status == "confirming" and state.get("user_text") and not state.get("pending_slot"):
        # Already collected everything → this turn answers the confirmation.
        return "confirm"
    return "respond"


def _route_after_confirm(state: WorkflowState) -> str:
    return "execute" if state.get("status") == "executing" else "end"


def build_appointment_graph(checkpointer) -> Any:
    graph = StateGraph(WorkflowState)
    graph.add_node("extract", _extract_slot)
    graph.add_node("respond", _ask_or_confirm)
    graph.add_node("confirm", _handle_confirmation)
    graph.add_node("execute", _execute_booking)

    graph.set_entry_point("extract")
    graph.add_conditional_edges("extract", _route_after_extract,
                                {"respond": "respond", "confirm": "confirm"})
    graph.add_edge("respond", END)
    graph.add_conditional_edges("confirm", _route_after_confirm,
                                {"execute": "execute", "end": END})
    graph.add_edge("execute", END)
    return graph.compile(checkpointer=checkpointer)


register_graph_builder("appointment_booking", build_appointment_graph)
# Alias used by demo intents ("book appointment" → workflow:appointment).
register_graph_builder("appointment", build_appointment_graph)
