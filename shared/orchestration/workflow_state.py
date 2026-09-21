"""Checkpointed workflow state shared by the interpreter and graph builders.

Kept in its own module so extensions (reference flows, tenant graph
builders) can type their nodes without importing the engine.
"""
from __future__ import annotations

from typing import TypedDict


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
    context_response: bool  # authored node delegates this reply to the LLM
    awaiting_prompt: str | None  # question of the node the flow is paused at
    signal: str | None  # semantic signal of the caller's utterance
    # Response-delivery contract for THIS turn's reply (see response_modes):
    response_mode: str  # fixed | exact | llm_grounded (aggregated per turn)
    response_directives: list[str]  # grounded nodes' response goals
    response_must_include: list[str]  # literals that must survive generation
    # Delivery feedback. ``spoken_nodes`` accumulates every node whose
    # authored text was spoken on this session; ``spoken_this_turn`` is the
    # per-turn subset (reported to the caller as ``spokenNodes``).
    # ``heard_nodes`` is INPUT from the delivery channel: node ids whose reply
    # audio the caller heard to completion (the voice brain tracks barge-ins
    # and TTS completion). Absent/None = the channel cannot observe delivery
    # (text chat, simulate) → every spoken node counts as heard. Grounded
    # nodes select ``responseDirectiveVariants`` on it.
    spoken_nodes: list[str]
    spoken_this_turn: list[str]
    heard_nodes: list[str] | None
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
    # Per-turn: set when the flow is paused at an ask node collecting a
    # numeric identifier — {node, variable, entity, held_digits}. The brain
    # uses it to run identifier-collection mode (tolerant inter-digit pause
    # window, fragment buffering, batch-audio recovery) generically from the
    # workflow's own awaited field schema.
    awaiting_identifier: dict | None
    # Runtime facts resolved once during call/session initialization.  An ask
    # node must opt into a value with ``prefillFromContext``; the interpreter
    # never performs a database or remote lookup while advancing the flow.
    context_values: dict[str, object]
    # Input-only semantic extraction from the definition's semantic-slots
    # provider (an extension opted into via ``semanticSlots``). Replaced on
    # every invocation; never reuse a preceding utterance's extraction.
    semantic_extraction: dict | None
