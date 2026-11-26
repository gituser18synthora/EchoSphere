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
    # Explicit valueReadback opt-in only: permitted display values, separate
    # from masked slots and never included in exported slot/LLM context.
    readback_values: dict[str, dict]
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
    # Call-context values the handover node forwards with the transfer
    # (its opt-in ``contextArgs``); recomputed every step.
    handoff_data: dict[str, str] | None
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
    # Input-only provenance for signal_override. True means a validated
    # decision supplied the signal, including an intentional None (no generic
    # signal). False/absent retains the legacy regex fallback for None. Cleared
    # with the override after every turn so authority cannot be checkpointed
    # into the next caller utterance.
    signal_authoritative: bool
    # Input-only: the id of the paused hub's ``answerOptions`` entry the
    # validated decision chose for THIS turn (None = it chose none). Read only
    # by an intent hub that declares that option; cleared with the signal.
    option_override: str | None
    # Input-only: a model decision judged this reply against the hub's options
    # (it may have chosen none). Cleared with the override.
    option_judged: bool
    # Output of the turn: the answer option the paused hub's reply resolved to
    # (chosen by the decision, a plain yes/no, or the option whose target the
    # authored labels reached), None when the hub declares no options or the
    # turn left it waiting. ``turn_side_effects`` is True when the turn ran an
    # api or handover node. Both are per-turn and overwritten every turn.
    answered_option: str | None
    turn_side_effects: bool
    # Input-only: a call-control request ("do_not_call") the delivery layer
    # hands to a flow that declares its own closing for it; cleared every
    # turn. ``call_control_handled`` is the turn's answer: the request when
    # the flow took it, None otherwise.
    call_control: str | None
    call_control_handled: str | None
    # Validated CURRENT identity reply, consumed only by an ask explicitly
    # authored with confirmationRole=identity and confirmationValues. Never
    # retained as authority for the next utterance.
    identity_resolution: str | None
    identity_confirmation: dict | None
    # Per-turn constant writes from message nodes explicitly marked
    # terminalOutcome. Reported only when this turn completes the workflow;
    # the delivery layer can retract these outcome annotations if a caller
    # corrects a fact after the closing reply.
    terminal_set_slots: list[str]
    # Testing Studio: {tool_name: payload} replaces live HTTP in api nodes.
    mock_tool_results: dict | None
    # Digits heard so far at an ask node whose entity expects a numeric
    # identifier, keyed by node id — a caller dictating "six zero … <pause>
    # one zero double one" accumulates across turns instead of failing.
    pending_digits: dict[str, str]
    # Private caller candidates; absent from exported slots and audit values.
    pending_identifier_confirmations: dict[str, dict]
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
