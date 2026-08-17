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
import threading
import time
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from shared.config import get_settings
from shared.orchestration.async_tools import to_thread_abandonable
from shared.orchestration.phrases import canned
from shared.orchestration.router import classify_user_signal

logger = logging.getLogger(__name__)


class WorkflowState(TypedDict, total=False):
    """Typed workflow state — every workflow carries the full call identity."""

    tenant_id: str
    bot_id: str
    session_id: str
    workflow: str
    user_text: str
    language: str  # caller's current conversation locale ("hi-IN"); "" = en
    slots: dict[str, str]
    pending_slot: str | None
    just_filled: bool
    reply: str
    status: str  # collecting | confirming | executing | done | error | handoff
    retries: int
    audit: list[dict]
    # Definition-interpreter fields (DB-authored graphs):
    current_node: str | None
    awaiting: str | None
    node_retries: dict[str, int]
    trace: list[str]  # node ids visited THIS turn (reset every turn)
    handoff_queue: str | None  # handover node's configured queue, if any
    # Per-turn (recomputed every step, never carried over):
    off_script: bool  # the turn was NOT consumed — node unchanged, no reply
    awaiting_prompt: str | None  # question of the node the flow is paused at
    signal: str | None  # semantic signal of the caller's utterance
    # Input-only: the caller-supplied semantic signal for THIS turn (from the
    # Goal Engine's validated decision). Consumed by _step and cleared in the
    # returned state so a checkpointed value can never leak into a later turn;
    # when absent the legacy regex classification is the fallback.
    signal_override: str | None
    # Testing Studio: {tool_name: payload} replaces live HTTP in api nodes.
    mock_tool_results: dict | None
    # Digits heard so far at an ask node whose entity expects a numeric
    # identifier, keyed by node id — a caller dictating "six zero … <pause>
    # one zero double one" accumulates across turns instead of failing.
    pending_digits: dict[str, str]


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


# ── payment collection: MOP (mode-of-payment) slot-filling workflow ─────────
#
# Built for inbound loan-repayment support (mPokket POC). The flow mirrors the
# approved call script: payment type (full/partial) → MOP confirmation
# ("Debit Card ya UPI?") → summary confirmation → next-step guidance and the
# script's closing line. No payment integration exists, so the execute step
# records a payment COMMITMENT and instructs the caller — it never claims a
# payment was completed. Replies are Hinglish, matching the source script.

_PAY_SLOTS: list[tuple[str, str, str, dict[str, str]]] = [
    # (slot key, question, simpler retry question, {canonical: pattern})
    # Patterns cover Roman Hinglish AND Devanagari — Sarvam Saaras transcribes
    # Hindi speech in Devanagari script, Hinglish/English in Latin script.
    # NOTE: Devanagari alternatives sit OUTSIDE the \b group — Python's \b is
    # \w-based and Devanagari matras are not word characters, so a trailing \b
    # after a matra-final word (e.g. "पूरा") can never match.
    (
        "payment_type",
        "Kya aap apna overdue amount poora pay karna chahenge, ya partial payment karenge?",
        "Kripya boliye – poora payment ya partial?",
        {
            "full": r"\b(full|poora|pura|puri|complete|whole|saara|sara)\b"
                    r"|पूरा|पूरी|सारा|पूर्ण",
            "partial": r"\b(partial|part|aadha|adha|half|thoda|kuch|installment|instalment)\b"
                       r"|आधा|आधी|थोड़ा|थोड़ी|किस्त",
        },
    ),
    (
        "payment_method",
        "Kaunse madhyam se aapka payment hoga – Debit Card ya UPI?",
        "Kripya boliye – Debit Card ya UPI?",
        {
            "Debit Card": r"\b(debit|card|atm)\b|डेबिट|कार्ड|एटीएम",
            "UPI": r"\b(upi|bhim|paytm|g ?pay|google ?pay|phone ?pe|qr)\b"
                   r"|यूपीआई|यू ?पी ?आई|भीम|पेटीएम|फोन ?पे|गूगल ?पे",
        },
    ),
]

_PAY_YES = re.compile(
    r"\b(yes|yeah|correct|confirm|right|sure|ok(ay)?|haan|han ?ji|ji haan|ji|"
    r"bilkul|theek|sahi|zaroor|kar do|karo)\b"
    r"|हाँ|हां|जी|सही|ठीक|बिल्कुल|ज़रूर|जरूर", re.I,
)
_PAY_NO = re.compile(
    r"\b(no|nope|wrong|change|cancel|nahi|nahin|galat|badal)\b|नहीं|नही|गलत|बदल", re.I
)


def _pay_extract_slot(state: WorkflowState) -> WorkflowState:
    slots = dict(state.get("slots") or {})
    text = (state.get("user_text") or "").strip()
    pending = state.get("pending_slot")
    retries = state.get("retries", 0)

    just_filled = False
    if text:
        # Callers often volunteer several details in one utterance ("main UPI
        # se poora pay karunga") — fill every open slot the turn mentions.
        for key, _q, _rq, patterns in _PAY_SLOTS:
            if key in slots:
                continue
            for canonical, pattern in patterns.items():
                if re.search(pattern, text, re.I):
                    slots[key] = canonical
                    just_filled = True
                    break
        if pending:
            retries = 0 if pending in slots else retries + 1

    next_slot = next((key for key, _, _, _ in _PAY_SLOTS if key not in slots), None)
    status = "collecting" if next_slot else "confirming"
    if retries > _MAX_SLOT_RETRIES:
        status = "handoff"
    return {
        **state,
        "slots": slots,
        "pending_slot": next_slot,
        "just_filled": just_filled,
        "retries": retries,
        "status": status,
    }


def _pay_ask_or_confirm(state: WorkflowState) -> WorkflowState:
    status = state.get("status")
    if status == "handoff":
        return {
            **state,
            "reply": "Mujhe aapki baat samajhne mein dikkat ho rahi hai. Aapko "
                     "hamare ek agent se connect kiya ja raha hai, kripya line par bane rahiye.",
        }
    if status == "collecting":
        pending = state.get("pending_slot")
        slot = next((s for s in _PAY_SLOTS if s[0] == pending), None)
        if slot is None:
            return {**state, "reply": "Kripya dobara boliye?"}
        # Retries use the simpler wording, never the same sentence again.
        question = slot[2] if state.get("retries", 0) > 0 else slot[1]
        prefix = "Maaf kijiye, baat samajh nahi aayi. " if state.get("retries", 0) > 0 else ""
        return {**state, "reply": f"{prefix}{question}"}
    slots = state.get("slots", {})
    type_txt = "poora amount" if slots.get("payment_type") == "full" else "partial payment"
    return {
        **state,
        "reply": f"Main confirm kar leti hoon – aap {type_txt} "
                 f"{slots.get('payment_method')} ke through pay karenge. Kya yeh sahi hai?",
    }


def _pay_handle_confirmation(state: WorkflowState) -> WorkflowState:
    text = state.get("user_text", "")
    if _PAY_NO.search(text):
        return {
            **state,
            "slots": {},
            "pending_slot": _PAY_SLOTS[0][0],
            "retries": 0,
            "status": "collecting",
            "reply": f"Koi baat nahi, dobara shuru karte hain. {_PAY_SLOTS[0][1]}",
        }
    if _PAY_YES.search(text):
        return {**state, "status": "executing"}
    return {
        **state,
        "reply": "Kripya haan boliye confirm karne ke liye, ya nahi boliye badalne ke liye.",
    }


def _pay_execute(state: WorkflowState) -> WorkflowState:
    """Record the payment commitment (no payment API exists — never claim
    completion) and give the script's next-step guidance and closing line."""
    audit = list(state.get("audit") or [])
    audit.append(
        {
            "action": "payment_commitment_recorded",
            "tenant_id": state.get("tenant_id"),
            "bot_id": state.get("bot_id"),
            "session_id": state.get("session_id"),
            "slots": state.get("slots"),
        }
    )
    slots = state.get("slots", {})
    method = slots.get("payment_method", "UPI")
    benefit = (
        " BHIM UPI ya Paytm UPI se payment karne par aapko discount ya cashback "
        "milne ke chances hain."
        if method == "UPI"
        else ""
    )
    return {
        **state,
        "status": "done",
        "audit": audit,
        "reply": (
            "Dhanyavaad! Kripya mPokket app kholkar "
            f"{method} ke through apna payment abhi complete kar dijiye.{benefit} "
            "Payment complete hote hi aapka profile update ho jayega aur extra "
            "penalty charges nahi lagenge. Main call par kabhi card number, PIN "
            "ya OTP nahi maangti — yeh details kisi ke saath share na karein. "
            "mPokket mein samay dene ke liye dhanyavaad, aapka din shubh ho!"
        ),
    }


def _pay_route_after_extract(state: WorkflowState) -> str:
    """Unlike the reference flow, the turn that completes the slots always
    gets the spoken summary first — only a turn that filled nothing while in
    'confirming' is treated as the caller's answer to that summary."""
    status = state.get("status")
    if status == "handoff":
        return "respond"
    if status == "confirming" and state.get("user_text") and not state.get("just_filled"):
        return "confirm"
    return "respond"


def build_payment_collection_graph(checkpointer) -> Any:
    graph = StateGraph(WorkflowState)
    graph.add_node("extract", _pay_extract_slot)
    graph.add_node("respond", _pay_ask_or_confirm)
    graph.add_node("confirm", _pay_handle_confirmation)
    graph.add_node("execute", _pay_execute)

    graph.set_entry_point("extract")
    graph.add_conditional_edges("extract", _pay_route_after_extract,
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
    # Inbound loan-repayment MOP flow (mPokket POC).
    "payment_collection": build_payment_collection_graph,
}


# ── generic definition interpreter: DB-authored node/edge graphs ─────────────
#
# Workflows designed in the Studio builder (Workflow.nodes/edges JSON) execute
# here. An intent route "workflow:<name>" is resolved against the bot's saved
# workflows first (by id, slugified name, or exact name); the hardcoded
# builders above remain as fallbacks so the reference flows keep working.
#
# Node kinds: start, message (speak & continue), ask (collect a variable via
# entity extraction), intent (branch on the caller's next utterance using edge
# labels), condition (branch on a collected variable), api (audited action —
# executed via the configured connection where wired, otherwise recorded and
# routed through its success edge), knowledge (answer from the tenant KB),
# handover (escalate & finish), end (finish). Unknown kinds pass through.

_MAX_NODE_STEPS = 30
_MAX_ASK_RETRIES = 2
_ELSE_LABELS = ("else", "other", "default", "fallback", "no match", "otherwise")


def slugify_workflow_name(name: str) -> str:
    """"Payment plan journey" → payment_plan_journey (route-string form)."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (name or "").lower())).strip("_")


# Definition lookups run on EVERY workflow turn (via to_thread) — a short
# in-process TTL cache keeps the per-turn cost at a dict hit instead of a
# control-plane query. Staleness is bounded by the TTL only (a saved edit
# shows up within 30 s); no cross-process invalidation by design.
_DEFINITION_CACHE_TTL_SECONDS = 30.0
_definition_cache: dict[tuple[str, str, str], tuple[float, dict | None]] = {}
_definition_cache_lock = threading.Lock()


def load_workflow_definition(
    tenant_id: str, bot_id: str, workflow_name: str
) -> dict | None:
    """Latest saved workflow for the bot whose id/name matches the route name.

    Sync (called via to_thread). Returns None when no stored workflow matches —
    the caller then falls back to the hardcoded reference builders.
    """
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import Workflow

    target = (workflow_name or "").strip().lower()
    if not target:
        return None

    cache_key = (tenant_id, bot_id, workflow_name)
    now = time.monotonic()
    with _definition_cache_lock:
        cached = _definition_cache.get(cache_key)
        if cached is not None and now - cached[0] < _DEFINITION_CACHE_TTL_SECONDS:
            return cached[1]

    session = get_sessionmaker()()
    try:
        # Two-phase lookup: match on the cheap id/version/name projection
        # first, then load ONLY the matching row's full definition — never
        # every version's nodes/edges JSON.
        rows = session.execute(
            select(Workflow.id, Workflow.version, Workflow.name)
            .where(
                Workflow.tenant_id == tenant_id,
                Workflow.bot_id == bot_id,
                Workflow.is_deleted.is_(False),
            )
            .order_by(Workflow.version.desc())
        ).all()
        definition: dict | None = None
        for row_id, _version, name in rows:
            if not (
                row_id == workflow_name
                or slugify_workflow_name(name) == target
                or (name or "").strip().lower() == target
            ):
                continue
            w = (
                session.execute(select(Workflow).where(Workflow.id == row_id))
                .scalars()
                .first()
            )
            if w is None or not w.nodes:
                continue  # empty definition — keep looking, as before
            definition = {
                "id": w.id,
                "version": w.version,
                "name": w.name,
                "nodes": w.nodes or [],
                "edges": w.edges or [],
            }
            break
        with _definition_cache_lock:
            _definition_cache[cache_key] = (time.monotonic(), definition)
        return definition
    finally:
        session.close()


def _node_config(node: dict) -> dict:
    config = node.get("config")
    return config if isinstance(config, dict) else {}


def _node_text(node: dict, *keys: str, fallback_label: bool = True) -> str:
    config = _node_config(node)
    for key in keys:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(node.get("label") or "").strip() if fallback_label else ""


def _edge_tokens(label: str) -> list[str]:
    return [t.strip().lower() for t in re.split(r"[/,|]", label or "") if t.strip()]


_SENTENCE_SPLIT = re.compile(r"(?<=[।?!.])\s+")
_MAX_REASK_CHARS = 160


def _short_question(base: str, lang: str = "") -> str:
    """The re-askable core of a node's text: its last question sentence."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(base or "") if s.strip()]
    if not sentences:
        return canned("wf_more_detail", lang)
    question = next(
        (s for s in reversed(sentences) if s.endswith("?")), sentences[-1]
    )
    if len(question) > _MAX_REASK_CHARS:
        return canned("wf_more_detail", lang)
    return question


# ── semantic edge selection for intent nodes ────────────────────────────────
# A user turn advances an intent node ONLY when the node actually supports
# what the caller said: an edge whose label carries the same semantic signal
# (hardship edge for a hardship statement), or a literal token match. A
# complaint/question/hardship the node has NO edge for never advances the
# graph — the turn is reported as off-script so the caller gets a grounded
# contextual reply while the workflow stays at the same node. The old
# behavior (retry once, then blindly follow the FIRST edge) is gone: it made
# every unrecognized utterance walk the script sequentially.

# Signals that talk ABOUT the conversation (or ask something) rather than
# answering the pending question — never advanced by literal keyword luck.
_OFF_SCRIPT_SIGNALS = ("complaint", "clarify", "question")
# Flow-answer signals that may still advance via literal tokens when no edge
# declares the signal explicitly (the author spelled the words out instead).
_LITERAL_FALLBACK_SIGNALS = ("refusal", "callback", "payment_intent", "affirm")
# Signals meaningful enough that the utterance which TRIGGERED the workflow
# should be consumed by its first intent node instead of being ignored (a
# bare confirmation is not — it answered the greeting, not the first rung).
_ENTRY_SIGNALS = ("hardship", "refusal", "callback", "already_paid",
                  "wrong_person", "agent_request", "payment_intent")
_COMPATIBLE_SIGNALS = {"affirm": ("payment_intent",),
                       "payment_intent": ("affirm",)}


def _edge_meta(edges: list[dict]) -> list[tuple[dict, list[str], set[str]]]:
    """(edge, non-else tokens, semantic signals its tokens carry) per edge."""
    meta = []
    for edge in edges:
        tokens = [t for t in _edge_tokens(edge.get("label", ""))
                  if t not in _ELSE_LABELS]
        signals = {s for s in (classify_user_signal(t) for t in tokens) if s}
        meta.append((edge, tokens, signals))
    return meta


def _token_score(tokens: list[str], lowered: str) -> int:
    """Longest edge token contained in the utterance (0 = none). Longer
    tokens are more specific: "पैसे नहीं" (hardship) must beat "नहीं"."""
    return max((len(t) for t in tokens if t and t in lowered), default=0)


def _choose_intent_edge(
    meta: list[tuple[dict, list[str], set[str]]], text: str, signal: str | None
) -> tuple[dict | None, str]:
    """Pick the outgoing edge the utterance actually supports.

    Returns (edge, reason); edge None means nothing matched and reason says
    why: "off_script" (a signal the node has no edge for — do not advance,
    let the brain answer it) or "no_match" (no signal, no literal match —
    the caller may retry or take an authored else/fallback edge).
    """
    lowered = (text or "").lower()
    if signal:
        wanted = (signal, *_COMPATIBLE_SIGNALS.get(signal, ()))
        supporting = [
            (edge, _token_score(tokens, lowered))
            for edge, tokens, signals in meta
            if any(w in signals for w in wanted)
        ]
        if supporting:
            return max(supporting, key=lambda pair: pair[1])[0], "signal"
        if signal not in _LITERAL_FALLBACK_SIGNALS:
            return None, "off_script"
    best, best_score = None, 0
    for edge, tokens, _signals in meta:
        score = _token_score(tokens, lowered)
        if score > best_score:
            best, best_score = edge, score
    if best is not None:
        return best, "token"
    return None, "no_match"


def _pick_edge_by_flag(out_edges: list[dict], result: bool) -> dict | None:
    """condition branching: true/yes edge vs false/no edge, positional fallback."""
    truthy, falsy = ("true", "yes"), ("false", "no")
    wanted = truthy if result else falsy
    for edge in out_edges:
        if any(t in wanted for t in _edge_tokens(edge.get("label", ""))):
            return edge
    if len(out_edges) >= 2:
        return out_edges[0] if result else out_edges[1]
    return out_edges[0] if out_edges else None


def _evaluate_condition(config: dict, slots: dict) -> bool:
    variable = str(config.get("variable") or "")
    operator = str(config.get("operator") or "exists").lower()
    expected = config.get("value")
    actual = slots.get(variable)
    if operator in ("exists", "filled"):
        return actual is not None and str(actual).strip() != ""
    if actual is None:
        return False
    actual_s, expected_s = str(actual).strip().lower(), str(expected or "").strip().lower()
    if operator in ("equals", "eq", "is"):
        return actual_s == expected_s
    if operator in ("not_equals", "ne", "not"):
        return actual_s != expected_s
    if operator == "contains":
        return expected_s in actual_s
    try:
        actual_n, expected_n = float(actual_s), float(expected_s)
    except (TypeError, ValueError):
        return False
    if operator in ("gte", ">="):
        return actual_n >= expected_n
    if operator in ("lte", "<="):
        return actual_n <= expected_n
    if operator in ("gt", ">"):
        return actual_n > expected_n
    if operator in ("lt", "<"):
        return actual_n < expected_n
    return False


async def _knowledge_answer(state: WorkflowState, node: dict, slots: dict) -> str | None:
    """Answer a knowledge node from the tenant's KB; None when unanswerable."""
    config = _node_config(node)
    query = str(config.get("query") or "").strip()
    if query:
        try:
            query = query.format_map({**slots})
        except (KeyError, ValueError):
            pass
    query = query or (state.get("user_text") or "").strip()
    if not query:
        return None
    try:
        from shared.knowledge.schemas import RetrievalRequest
        from shared.knowledge.service import get_knowledge_service

        result = await get_knowledge_service().search(
            RetrievalRequest(
                tenant_id=state.get("tenant_id", ""),
                bot_id=state.get("bot_id"),
                query=query,
            )
        )
        if result.answerable and result.sources:
            return result.sources[0].text[:400]
    except Exception:  # noqa: BLE001 — a KB hiccup must not kill the flow
        logger.exception("workflow knowledge node retrieval failed")
    return None


def _ask_entity(node: dict, variable: str) -> dict:
    """Entity descriptor for an ask node, feeding the shared entity extractor."""
    config = _node_config(node)
    entity = config.get("entity")
    if isinstance(entity, dict) and entity:
        return {"name": variable, **entity}
    return {
        "name": variable,
        "dataType": str(config.get("entityType") or config.get("dataType") or "text"),
        "regexPattern": config.get("pattern"),
        "allowedValues": config.get("allowedValues"),
        "synonyms": config.get("synonyms"),
    }


def _ask_is_free_text(node: dict, variable: str) -> bool:
    """A free-text ask accepts ANY utterance as its answer — it needs the
    off-script guard so a complaint or question is not swallowed as a slot."""
    entity = _ask_entity(node, variable)
    has_matcher = bool(
        entity.get("regexPattern") or entity.get("allowedValues") or entity.get("synonyms")
    )
    return str(entity.get("dataType") or "text") == "text" and not has_matcher


def _ask_expects_digits(node: dict, variable: str) -> bool:
    """Whether this ask collects a numeric identifier (booking ID, OTP, …)."""
    from shared.orchestration.entity_extractor import _expects_digits

    return _expects_digits(_ask_entity(node, variable))


# A dictated identifier can be held across turns while the caller pauses;
# anything longer than this is no longer an identifier being dictated.
_MAX_PENDING_DIGITS = 32


def _extract_ask_value(node: dict, variable: str, text: str) -> str | None:
    from shared.orchestration.entity_extractor import extract_entity

    entity = _ask_entity(node, variable)
    data_type = str(entity.get("dataType") or "text")
    has_matcher = bool(
        entity.get("regexPattern") or entity.get("allowedValues") or entity.get("synonyms")
    )
    if data_type == "text" and not has_matcher:
        # Free-text answer: take the utterance as-is.
        return text.strip() or None
    extracted = extract_entity(text, entity)
    if not extracted.get("matched"):
        return None
    return str(extracted.get("value") or extracted.get("maskedValue") or "").strip() or None


def build_definition_graph(definition: dict, checkpointer) -> Any:
    """Compile a saved node/edge document into a single-step LangGraph.

    One LangGraph node advances the interpreter until the flow needs caller
    input (ask/intent) or terminates — LangGraph supplies the per-thread
    checkpointing so slots and the current position survive across turns
    (and across worker restarts when Postgres is available).
    """
    nodes_by_id: dict[str, dict] = {
        str(n.get("id")): n for n in definition.get("nodes") or [] if n.get("id")
    }
    edges_from: dict[str, list[dict]] = {}
    for edge in definition.get("edges") or []:
        src = str(edge.get("from") or "")
        if src and str(edge.get("to") or "") in nodes_by_id:
            edges_from.setdefault(src, []).append(edge)
    # (edge, tokens, semantic signals) per source node, computed once.
    edge_meta_from: dict[str, list[tuple[dict, list[str], set[str]]]] = {
        src: _edge_meta(edges) for src, edges in edges_from.items()
    }

    start_node = next(
        (n for n in (definition.get("nodes") or []) if n.get("kind") == "start"), None
    ) or next(iter((definition.get("nodes") or [])), None)

    def _next_of(node_id: str) -> str | None:
        out = edges_from.get(node_id) or []
        return str(out[0].get("to")) if out else None

    def _question(node: dict, retrying: bool, lang: str = "") -> str:
        base = _node_text(node, "question", "prompt", "text")
        if not base:
            return canned("wf_more_detail", lang)
        if not retrying:
            return base
        # A retry must never re-read the node's full scripted text — callers
        # heard it seconds ago, and long pitch nodes turned every retry into
        # the same monologue again. Re-ask with just the node's actual
        # QUESTION (its last interrogative sentence), or a generic short
        # re-prompt when the node text has no question to extract.
        return canned("wf_retry_prefix", lang) + _short_question(base, lang)

    async def _step(state: WorkflowState) -> WorkflowState:
        # Generic engine strings follow the caller's conversation language —
        # workflow-authored node text is spoken as authored.
        lang = state.get("language") or ""
        slots = dict(state.get("slots") or {})
        node_retries = dict(state.get("node_retries") or {})
        pending_digits = dict(state.get("pending_digits") or {})
        audit = list(state.get("audit") or [])
        trace: list[str] = []
        replies: list[str] = []
        status = "collecting"
        handoff_queue: str | None = None
        text = (state.get("user_text") or "").strip()
        # Semantic signal: the Goal Engine's validated decision (passed per
        # turn) is primary; the regex classifier is the deterministic
        # fallback when no decision reached this turn.
        signal = (state.get("signal_override") or None) if text else None
        if signal is None and text:
            signal = classify_user_signal(text)
        off_script = False
        current = state.get("current_node")
        awaiting = state.get("awaiting")

        if current not in nodes_by_id:
            current = str(start_node.get("id")) if start_node else None
            awaiting = None

        # The utterance that STARTED the workflow (no node was awaiting it)
        # may be consumed by the first intent node the walk reaches — a
        # caller entering the flow with "paise nahi hain" must land on the
        # hardship branch, not hear rung one's pitch. Single use.
        entry_text = text if (not awaiting and text) else ""

        # 1. Feed the caller's utterance to the node that was waiting for it.
        if awaiting and awaiting in nodes_by_id:
            node = nodes_by_id[awaiting]
            kind = node.get("kind")
            trace.append(awaiting)
            if not text:
                replies.append(_question(node, retrying=False, lang=lang))
                current = None  # stay awaiting
            elif kind == "ask":
                config = _node_config(node)
                variable = str(config.get("variable") or node.get("id"))
                guarded = signal in _OFF_SCRIPT_SIGNALS or signal == "agent_request"
                # Numeric-identifier dictation: digits held from earlier turns
                # of THIS ask continue the same identifier, so "six zero …
                # <pause> one zero double one" resolves as one value. The
                # buffer is tried FIRST — a short continuation chunk ("1011")
                # can look like a complete answer on its own.
                from shared.orchestration.spoken_numbers import (
                    digits_dominant,
                    spoken_digit_sequence,
                )

                expects_digits = _ask_expects_digits(node, variable)
                dictated = expects_digits and digits_dominant(text)
                buffered = pending_digits.get(awaiting, "") if expects_digits else ""
                fresh = spoken_digit_sequence(text) if dictated else ""
                combined = (buffered + fresh)[:_MAX_PENDING_DIGITS]
                value = None
                accumulated = False
                if buffered and fresh:
                    value = _extract_ask_value(node, variable, combined)
                    accumulated = value is not None
                if value is None and not (guarded and _ask_is_free_text(node, variable)):
                    value = _extract_ask_value(node, variable, text)
                if value is not None:
                    slots[variable] = value
                    pending_digits.pop(awaiting, None)
                    node_retries.pop(awaiting, None)
                    entry = {"action": "slot_filled", "node": awaiting,
                             "variable": variable}
                    if accumulated:
                        entry["accumulated_digits"] = len(combined)
                    audit.append(entry)
                    current, awaiting = _next_of(awaiting), None
                elif dictated and fresh and combined != buffered:
                    # A partial identifier: hold what was heard, keep the ask
                    # open, and do not burn a retry — the caller is making
                    # progress, not failing to answer.
                    pending_digits[awaiting] = combined
                    audit.append({"action": "digits_partial", "node": awaiting,
                                  "held_digits": len(combined)})
                    replies.append(canned("wf_digits_partial", lang))
                    current = None  # stay awaiting
                elif signal is not None:
                    # The caller said something meaningful that the ask's
                    # matcher did not extract (hardship, complaint, a
                    # question — or a bare "ठीक है" at a choice question).
                    # Do not burn a retry, advance, or speak a canned
                    # "didn't catch that"; the brain answers in context and
                    # the ask still accepts the next answer.
                    audit.append({"action": "off_script", "node": awaiting,
                                  "signal": signal})
                    off_script = True
                    current = None  # stay awaiting
                else:
                    retries = node_retries.get(awaiting, 0) + 1
                    node_retries[awaiting] = retries
                    if retries > _MAX_ASK_RETRIES:
                        fallback = next(
                            (e for e in edges_from.get(awaiting, [])
                             if any(t in ("fallback", "handoff")
                                    for t in _edge_tokens(e.get("label", "")))),
                            None,
                        )
                        if fallback is not None:
                            current, awaiting = str(fallback.get("to")), None
                        else:
                            status = "handoff"
                            replies.append(canned("wf_handover", lang))
                            current, awaiting = None, None
                    else:
                        replies.append(_question(node, retrying=True, lang=lang))
                        current = None  # stay awaiting
            elif kind == "intent":
                out_edges = edges_from.get(awaiting, [])
                chosen, why = _choose_intent_edge(
                    edge_meta_from.get(awaiting, []), text, signal
                )
                if chosen is None and why == "off_script":
                    audit.append({"action": "off_script", "node": awaiting,
                                  "signal": signal})
                    off_script = True
                    current = None  # stay awaiting
                elif chosen is None:  # no signal, no literal match
                    retries = node_retries.get(awaiting, 0) + 1
                    node_retries[awaiting] = retries
                    if retries > 1:
                        # Only an AUTHORED fallback advances an unmatched
                        # turn — never the positional first edge.
                        chosen = next(
                            (e for e in out_edges
                             if any(t in _ELSE_LABELS
                                    for t in _edge_tokens(e.get("label", "")))),
                            None,
                        )
                    if chosen is None:
                        # First unmatched turn (and any later one with no
                        # authored else edge): the caller said something the
                        # node does not understand — a canned "didn't catch
                        # that" + re-read of the pitch is exactly the repeat
                        # loop callers complain about. Report off-script so
                        # the brain answers the actual message in context;
                        # the node stays and can still advance next turn.
                        audit.append({"action": "off_script",
                                      "node": awaiting, "signal": signal,
                                      "reason": "no_match"})
                        off_script = True
                        current = None
                    else:
                        why = "else"
                if chosen is not None:
                    audit.append({"action": "intent_branch", "node": awaiting,
                                  "edge": chosen.get("label") or chosen.get("id"),
                                  "matched": why, "signal": signal})
                    node_retries.pop(awaiting, None)
                    current, awaiting = str(chosen.get("to")), None
            else:  # a stale awaiting pointer — resume from that node
                awaiting = None

        # 2. Walk the graph until we need input or the flow terminates.
        steps = 0
        while current and current in nodes_by_id and steps < _MAX_NODE_STEPS:
            steps += 1
            node = nodes_by_id[current]
            kind = node.get("kind")
            if not trace or trace[-1] != current:
                trace.append(current)

            if kind == "message":
                spoken = _node_text(node, "text", "message")
                if spoken:
                    replies.append(spoken)
                current = _next_of(current)
            elif kind == "ask":
                replies.append(_question(node, retrying=False, lang=lang))
                awaiting, current = current, None
            elif kind == "intent":
                if entry_text:
                    # First intent node after a workflow entry: the utterance
                    # that triggered the flow carries meaning of its own —
                    # consume it when an edge explicitly supports its signal.
                    # entry_text IS this turn's text, so the (decision-first)
                    # signal computed above applies to it directly.
                    entry_signal = signal
                    entry_text = ""  # single use, matched or not
                    if entry_signal in _ENTRY_SIGNALS:
                        chosen, why = _choose_intent_edge(
                            edge_meta_from.get(current, []), text, entry_signal
                        )
                        if chosen is not None and why == "signal":
                            audit.append({"action": "intent_entry_branch",
                                          "node": current,
                                          "edge": chosen.get("label") or chosen.get("id"),
                                          "signal": entry_signal})
                            current = str(chosen.get("to"))
                            continue
                prompt = _node_text(node, "prompt", "question", "text",
                                    fallback_label=False)
                replies.append(prompt or "How can I help you today?")
                awaiting, current = current, None
            elif kind == "condition":
                result = _evaluate_condition(_node_config(node), slots)
                edge = _pick_edge_by_flag(edges_from.get(current, []), result)
                audit.append({"action": "condition", "node": current,
                              "result": result,
                              "edge": (edge or {}).get("label") or (edge or {}).get("id")})
                current = str(edge.get("to")) if edge else None
            elif kind == "api":
                config = _node_config(node)
                tool = str(
                    config.get("connection") or config.get("connectionId")
                    or config.get("name") or node.get("label") or ""
                ).strip()
                succeeded = False
                if tool:
                    # Live execution through the backend-validated executor:
                    # tenant/bot scoping, schema, idempotency, timeout/retry
                    # and masking are enforced there, not here.
                    from shared.orchestration.tool_executor import get_tool_executor

                    result = await get_tool_executor().execute(
                        tenant_id=state.get("tenant_id", ""),
                        bot_id=state.get("bot_id", ""),
                        tool=tool,
                        args={k: v for k, v in slots.items()
                              if not isinstance(v, (dict, list))},
                        workflow=state.get("workflow"),
                        session_id=str(state.get("session_id") or ""),
                        customer_verified=bool(slots.get("customer_verified")),
                        mock_results=state.get("mock_tool_results"),
                    )
                    succeeded = result.ok
                    # Mapped response fields become slots for later condition
                    # nodes ("payment_status equals completed" etc.).
                    for key, value in (result.mapped or {}).items():
                        slots.setdefault(str(key), value)
                    audit.append({"action": "api_call", "node": current,
                                  "name": tool, "ok": result.ok,
                                  "status": result.status,
                                  "mocked": result.mocked})
                else:
                    audit.append({"action": "api_call_skipped", "node": current,
                                  "reason": "no connection configured"})
                spoken = _node_text(node, "text", fallback_label=False)
                if spoken:
                    replies.append(spoken)
                out_edges = edges_from.get(current, [])
                wanted = (
                    ("success", "ok", "done") if succeeded
                    else ("failure", "failed", "error", "fallback")
                )
                edge = next(
                    (e for e in out_edges
                     if any(t in wanted for t in _edge_tokens(e.get("label", "")))),
                    out_edges[0] if out_edges else None,
                )
                current = str(edge.get("to")) if edge else None
            elif kind == "knowledge":
                answer = await _knowledge_answer(state, node, slots)
                answered = answer is not None
                replies.append(
                    answer
                    or _node_text(node, "fallbackText", fallback_label=False)
                    or canned("wf_kb_miss", lang)
                )
                audit.append({"action": "knowledge", "node": current,
                              "answered": answered})
                out_edges = edges_from.get(current, [])
                wanted = ("answered", "found") if answered else ("no answer", "not found", "fallback")
                edge = next(
                    (e for e in out_edges
                     if any(t in wanted for t in _edge_tokens(e.get("label", "")))),
                    out_edges[0] if out_edges else None,
                )
                current = str(edge.get("to")) if edge else None
            elif kind == "handover":
                spoken = _node_text(node, "text", fallback_label=False) or canned(
                    "wf_handover", lang
                )
                replies.append(spoken)
                queue = _node_config(node).get("queue")
                audit.append({"action": "handover", "node": current,
                              "queue": queue})
                handoff_queue = str(queue) if queue else None
                status = "handoff"
                current = None
            elif kind == "end":
                spoken = _node_text(node, "text", fallback_label=False)
                if spoken:
                    replies.append(spoken)
                status = "done"
                current = None
            else:  # start / unknown kinds pass through
                current = _next_of(current)
                if current is None and kind not in ("start",):
                    status = "done"

            if current is None and awaiting is None and status == "collecting":
                status = "done"

        if steps >= _MAX_NODE_STEPS:
            logger.warning("workflow definition %s exceeded step budget", definition.get("id"))
            status = "error"
            replies.append(canned("wf_error", lang))

        reply_text = " ".join(r for r in replies if r).strip()
        if not reply_text and not off_script:
            reply_text = "Is there anything else I can help you with?"
        awaiting_prompt = (
            _node_text(nodes_by_id[awaiting], "question", "prompt", "text",
                       fallback_label=False)
            if awaiting and awaiting in nodes_by_id else None
        )
        return {
            **state,
            "slots": slots,
            "node_retries": node_retries,
            "pending_digits": pending_digits,
            "audit": audit,
            "trace": trace,
            "current_node": awaiting or current,
            "awaiting": awaiting,
            "handoff_queue": handoff_queue,
            "off_script": off_script,
            "awaiting_prompt": awaiting_prompt,
            "signal": signal,
            "signal_override": None,  # input-only; never survives the turn
            "status": status if not awaiting else "collecting",
            "reply": reply_text,
        }

    graph = StateGraph(WorkflowState)
    graph.add_node("step", _step)
    graph.set_entry_point("step")
    graph.add_edge("step", END)
    return graph.compile(checkpointer=checkpointer)


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

    async def _get_builder_graph(self, workflow_name: str):
        builder = _GRAPH_BUILDERS[workflow_name]
        key = builder.__name__
        if key not in self._graphs:
            self._graphs[key] = builder(await self._get_checkpointer())
        return self._graphs[key]

    async def _get_definition_graph(self, definition: dict):
        # Keyed by id+version: a saved edit compiles a fresh graph immediately.
        key = f"def:{definition['id']}:v{definition['version']}"
        if key not in self._graphs:
            self._graphs[key] = build_definition_graph(
                definition, await self._get_checkpointer()
            )
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
        result = await self.handle_turn_detailed(
            session_id=session_id,
            tenant_id=tenant_id,
            bot_id=bot_id,
            workflow_name=workflow_name,
            user_text=user_text,
            timeout_seconds=timeout_seconds,
        )
        return result["reply"], result["done"]

    async def handle_turn_detailed(
        self,
        *,
        session_id: str,
        tenant_id: str,
        bot_id: str,
        workflow_name: str,
        user_text: str,
        timeout_seconds: float = 10.0,
        language: str | None = None,
        mock_tool_results: dict | None = None,
        signal: str | None = None,
    ) -> dict:
        """Advance one turn and return the full execution detail.

        Resolution order: the bot's SAVED workflow definitions (matched by id,
        slugified name or exact name) run first; the hardcoded reference
        builders remain as fallbacks; an unknown name ends the flow with a
        clear reply instead of silently running an unrelated graph.

        ``signal`` is the semantic signal of the utterance as decided by the
        Goal Engine (validated). When provided, intent-node edge selection
        routes on it instead of re-deriving meaning from regex patterns.
        """
        definition: dict | None = None
        try:
            definition = await to_thread_abandonable(
                load_workflow_definition, tenant_id, bot_id, workflow_name
            )
        except Exception:  # noqa: BLE001 — control-plane DB down ≠ dead call
            logger.exception("workflow definition lookup failed for %s", workflow_name)

        if definition is not None:
            graph = await self._get_definition_graph(definition)
            source = "definition"
        elif workflow_name in _GRAPH_BUILDERS:
            graph = await self._get_builder_graph(workflow_name)
            source = "builtin"
        else:
            logger.warning(
                "unknown workflow '%s' for bot %s — no saved definition or builder",
                workflow_name, bot_id,
            )
            return {
                "reply": canned("wf_missing", language),
                "done": True, "status": "error", "source": "missing",
                "workflowId": None, "trace": [], "slots": {},
            }

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
                        "language": language or "",
                        "mock_tool_results": mock_tool_results,
                        "signal_override": signal,
                    },
                    config=thread,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            logger.error("workflow %s timed out for %s", workflow_name, session_id)
            return {
                "reply": canned("wf_timeout", language),
                "done": True, "status": "error", "source": source,
                "workflowId": (definition or {}).get("id"), "trace": [], "slots": {},
            }
        status = state.get("status", "collecting")
        done = status in ("done", "handoff", "error")
        off_script = bool(state.get("off_script"))
        return {
            "reply": "" if off_script
                     else (state.get("reply") or canned("wf_repeat", language)),
            "done": done,
            "status": status,
            "source": source,
            "workflowId": (definition or {}).get("id"),
            "trace": list(state.get("trace") or []),
            "slots": dict(state.get("slots") or {}),
            "handoffQueue": state.get("handoff_queue"),
            # Off-script: the turn was NOT consumed — the workflow stays at
            # the same node and the caller (brain) must answer contextually.
            "offScript": off_script,
            "nodePrompt": state.get("awaiting_prompt"),
            "signal": state.get("signal"),
        }

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
