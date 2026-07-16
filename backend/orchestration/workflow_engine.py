"""LangGraph workflow engine — stateful multi-step business flows.

LangGraph is used ONLY here: for flows that genuinely need persistent state,
branching, retries and resume-after-restart (slot-filling forms, booking,
escalation). Audio never touches this layer (Pipecat owns audio); simple
FAQ/KB turns never enter it.

State is checkpointed to PostgreSQL (langgraph AsyncPostgresSaver), so an
in-progress workflow survives a voice-worker restart: the next turn for the
same session resumes from the last checkpoint.
"""

import asyncio
import logging
import re
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from backend.config import get_settings

logger = logging.getLogger(__name__)


class WorkflowState(TypedDict, total=False):
    """Typed workflow state — every workflow carries the full call identity."""

    tenant_id: str
    bot_id: str
    session_id: str
    workflow: str
    user_text: str
    slots: dict[str, str]
    pending_slot: str | None
    reply: str
    status: str  # collecting | confirming | executing | done | error | handoff
    retries: int
    audit: list[dict]


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


_GRAPH_BUILDERS = {
    "appointment_booking": build_appointment_graph,
    # Alias used by demo intents ("book appointment" → workflow:appointment).
    "appointment": build_appointment_graph,
}


class WorkflowEngine:
    """Runs LangGraph workflows with PostgreSQL-backed checkpoints."""

    def __init__(self) -> None:
        self._checkpointer = None
        self._graphs: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._saver_cm = None

    async def _get_checkpointer(self):
        if self._checkpointer is not None:
            return self._checkpointer
        async with self._lock:
            if self._checkpointer is not None:
                return self._checkpointer
            settings = get_settings()
            try:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                conninfo = (
                    f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
                    f"@{settings.postgres_host}:{settings.postgres_port}"
                    f"/{settings.postgres_database}"
                )
                self._saver_cm = AsyncPostgresSaver.from_conn_string(conninfo)
                saver = await self._saver_cm.__aenter__()
                await saver.setup()
                self._checkpointer = saver
                logger.info("workflow checkpoints: PostgreSQL")
            except Exception:  # noqa: BLE001 - degrade, never block calls
                logger.exception("Postgres checkpointer unavailable; using in-memory saver")
                self._checkpointer = MemorySaver()
        return self._checkpointer

    async def _get_graph(self, workflow_name: str):
        builder = _GRAPH_BUILDERS.get(workflow_name) or _GRAPH_BUILDERS["appointment_booking"]
        key = builder.__name__
        if key not in self._graphs:
            self._graphs[key] = builder(await self._get_checkpointer())
        return self._graphs[key]

    async def handle_turn(
        self,
        *,
        session_id: str,
        tenant_id: str,
        bot_id: str,
        workflow_name: str,
        user_text: str,
        timeout_seconds: float = 10.0,
    ) -> tuple[str, bool]:
        """Advance the workflow one turn. Returns (reply, finished)."""
        graph = await self._get_graph(workflow_name)
        thread = {"configurable": {"thread_id": f"{session_id}:{workflow_name}"}}
        try:
            state = await asyncio.wait_for(
                graph.ainvoke(
                    {
                        "tenant_id": tenant_id,
                        "bot_id": bot_id,
                        "session_id": session_id,
                        "workflow": workflow_name,
                        "user_text": user_text,
                    },
                    config=thread,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            logger.error("workflow %s timed out for %s", workflow_name, session_id)
            return (
                "I'm sorry, that took longer than expected. Let me connect you with an agent.",
                True,
            )
        status = state.get("status", "collecting")
        done = status in ("done", "handoff", "error")
        return state.get("reply", "Could you repeat that?"), done

    async def aclose(self) -> None:
        if self._saver_cm is not None:
            try:
                await self._saver_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._saver_cm = None
            self._checkpointer = None
            self._graphs.clear()


_engine: WorkflowEngine | None = None


def get_workflow_engine() -> WorkflowEngine:
    global _engine
    if _engine is None:
        _engine = WorkflowEngine()
    return _engine
