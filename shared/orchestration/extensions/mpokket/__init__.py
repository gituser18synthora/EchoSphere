"""mPokket extension: inbound loan-repayment MOP (mode-of-payment) flow.

Tenant-specific graph builder, formerly hard-coded inside the workflow
engine. Registered as ``payment_collection``; the flow's Hinglish script,
slot patterns and closing lines are this tenant's business and live here.
"""
from __future__ import annotations

import re
import time
from typing import Any

from langgraph.graph import END, StateGraph

from shared.orchestration.extensions import register_graph_builder
from shared.orchestration.workflow_state import WorkflowState

_MAX_SLOT_RETRIES = 2


def _route_after_confirm(state: WorkflowState) -> str:
    return "execute" if state.get("status") == "executing" else "end"


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


register_graph_builder("payment_collection", build_payment_collection_graph)
