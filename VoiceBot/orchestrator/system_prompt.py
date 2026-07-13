# orchestrator/system_prompt.py

import logging

from voicebot.config_layer.models import VoicebotConfig
from voicebot.orchestrator.call_state import CallState, ActiveGoal

logger = logging.getLogger(__name__)

RESPONSE_STYLE_INSTRUCTIONS = {
    "concise_direct": (
        "RESPONSE LENGTH: Be brief and direct. "
        "Aim for 2-3 sentences as your natural default. "
        "You may go up to 5 sentences if the question genuinely requires it — "
        "for example if the caller asks for a comparison or needs a step explained. "
        "Never pad. If you have said the essential thing, stop. "
        "Do not volunteer extra information that was not asked for."
    ),
    "friendly_detailed": (
        "RESPONSE LENGTH: Be warm and conversational. "
        "3-5 sentences is your natural default. "
        "You may go longer if the caller has asked a detailed question or said 'tell me more'. "
        "Even when being thorough, lead with the most important point first "
        "and let secondary details follow naturally."
    ),
    "professional": (
        "RESPONSE LENGTH: Be clear and precise. "
        "3-4 sentences is your natural default. "
        "Avoid conversational filler — every sentence should carry information. "
        "You may go up to 6 sentences for complex queries but stay structured and tight."
    ),
    "empathetic": (
        "RESPONSE LENGTH: Acknowledge the caller's situation in one warm sentence first. "
        "Then answer in 2-4 sentences. "
        "You may go deeper if the caller is distressed or the situation is genuinely complex. "
        "Do not over-explain — empathy is shown through listening as much as speaking."
    ),
}

GOAL_CONTEXT_MARKER = "## ACTIVE GOAL CONTEXT"
SENTIMENT_MARKER = "## SENTIMENT INSTRUCTION"


def assemble_system_prompt(
    config: VoicebotConfig,
    caller_graph: dict | None,
    call_state: CallState,
) -> str:
    """
    Build multi-section system prompt once at call start.
    Stored in call_state.system_prompt.
    Not rebuilt every turn — only augmented.

    For returning callers (caller_graph is non-empty), the prompt includes
    a RETURNING CALLER block that instructs the bot to use the caller's
    known data proactively — address by name, skip re-asking known info,
    and acknowledge unresolved items from prior calls.
    """
    sections = []

    # Determine if this is a returning caller before building sections
    is_returning = bool(
        caller_graph
        and caller_graph.get("nodes")
        and config.engine.context_recall_between_calls
    )

    # Section 1 — Role
    sections.append(config.engine.system_role)

    # Primary objectives: appended once after all base sections (see below)
    # so they are never omitted when non-empty and not duplicated mid-prompt.

    # Section 2 — Guardrails
    guardrails_text = (config.engine.guardrails or "").strip()
    if guardrails_text:
        # Append returning-caller guardrail rule when relevant
        if is_returning:
            guardrails_text += (
                "\nNever ask a returning caller for information you already "
                "have about them — name, email, phone, appointment details. "
                "Never say 'according to our records' or 'I can see in our "
                "system' — speak naturally as an advisor who simply remembers."
            )
        sections.append(f"Rules you must always follow:\n{guardrails_text}")

    # Section 3 — Response Style
    style_raw = config.engine.response_style.value
    style_key = style_raw.lower().replace(" & ", "_").replace(" ", "_")
    sections.append(
        RESPONSE_STYLE_INSTRUCTIONS.get(
            style_key,
            RESPONSE_STYLE_INSTRUCTIONS["concise_direct"],
        )
    )

    # Section 3.5 — Conversation memory (in-call history)
    sections.append(
        "CONVERSATION MEMORY INSTRUCTIONS:\n"
        "You have access to the full conversation history "
        "in this call. Every message the caller has said "
        "and every response you have given is available "
        "to you above.\n"
        "- ALWAYS refer back to what the caller told you "
        "earlier in this conversation.\n"
        "- NEVER say you don't remember or don't have "
        "information that was already shared in this call.\n"
        "- If the caller asks what they said before, "
        "summarize it accurately from the conversation history.\n"
        "- Treat this entire conversation as one continuous "
        "session — nothing is forgotten until the call ends."
    )

    # Section 4 — Language
    ci = config.conversation_intelligence
    if (
        ci.auto_language_detection
        and call_state.detected_language != ci.primary_language
    ):
        sections.append(
            f"The caller is speaking in {call_state.detected_language}. "
            f"Respond in {call_state.detected_language}."
        )
    else:
        sections.append(f"Respond in {ci.primary_language}.")

    # Section 5 — Caller Graph (returning callers only)
    if config.engine.context_recall_between_calls and caller_graph:
        graph_text = format_caller_graph(caller_graph, returning=is_returning)
        if graph_text:
            sections.append(graph_text)

    # Section 6 — Returning Caller Behaviour block (returning callers only)
    if is_returning:
        caller_name = caller_graph.get("caller_name", "")
        if caller_name:
            name_instruction = (
                f"This caller's name is {caller_name}. "
                f"Use their name naturally in your very first response. "
                f"Do not ask for it again."
            )
        else:
            name_instruction = (
                "This is a returning caller. "
                "Greet them warmly as someone you already know."
            )

        sections.append(
            "RETURNING CALLER — CRITICAL BEHAVIOUR:\n"
            f"{name_instruction}\n"
            "- You already have their details (see RETURNING CALLER context above). "
            "Use this information actively — do not ask for what you already know.\n"
            "- If they ask about something you have data for (email, phone, "
            "last appointment, prior interest), confirm it from memory naturally.\n"
            "- If there is an UNRESOLVED item from a prior call, acknowledge it "
            "early in the conversation and offer to continue helping with it.\n"
            "- Speak as a human advisor who remembers their clients — "
            "never as a system reading from a database.\n"
            "- Never say phrases like 'according to our records', "
            "'I see in our system', or 'as per our data'."
        )

    result = "\n\n".join(sections)

    # Primary objectives — appended at the very end with returning-caller
    # amendment injected inline when relevant.
    _po = (config.engine.primary_objectives or "").strip()
    if _po:
        if is_returning:
            returning_amendment = (
                "\n\n8. RETURNING CALLER — DO NOT RE-COLLECT KNOWN DATA\n"
                "This caller has spoken with us before. You already have their "
                "name, contact details, and prior conversation context. Do NOT "
                "ask for information you already have. Only collect details that "
                "are genuinely new or need updating. Treat them as a valued "
                "returning client, not a new lead."
            )
            _po = _po.rstrip() + returning_amendment
        result += (
            "\n\n---\n\n"
            "PRIMARY OBJECTIVES:\n"
            f"{_po}\n"
            "----------------------------------\n"
        )

    return result


def update_goal_context(
    system_prompt: str,
    active_goal: ActiveGoal | None,
) -> str:
    """
    Add or remove active goal section from system prompt.
    Strips old goal section first, then appends new one if goal active.
    """
    if GOAL_CONTEXT_MARKER in system_prompt:
        system_prompt = system_prompt[
            : system_prompt.index(GOAL_CONTEXT_MARKER)
        ].rstrip()

    if active_goal is None:
        return system_prompt

    filled = active_goal.filled_slots()
    unfilled = active_goal.unfilled_slots()

    filled_str = (
        "\n".join(f"  - {k}: {v}" for k, v in filled.items())
        if filled
        else "  None yet"
    )
    unfilled_str = ", ".join(unfilled) if unfilled else "None"

    return (
        system_prompt
        + (
            f"\n\n{GOAL_CONTEXT_MARKER}\n"
            f"You are currently helping the caller with: "
            f"{active_goal.goal_name}.\n"
            f"Collected so far:\n{filled_str}\n"
            f"Still needed: {unfilled_str}\n"
            f"Ask for the next missing piece naturally. One question at a time."
        )
    )


def augment_with_sentiment(
    system_prompt: str,
    sentiment: str,
) -> str:
    """
    Append or update sentiment instruction.
    Does not rebuild entire prompt.
    """
    if SENTIMENT_MARKER in system_prompt:
        system_prompt = system_prompt[
            : system_prompt.index(SENTIMENT_MARKER)
        ].rstrip()

    if sentiment in ("negative", "frustrated"):
        return (
            system_prompt
            + (
                f"\n\n{SENTIMENT_MARKER}\n"
                f"IMPORTANT: The caller appears {sentiment}. "
                f"Be extra empathetic and patient. "
                f"Acknowledge their frustration before responding."
            )
        )
    return system_prompt


def format_caller_graph(caller_graph: dict, returning: bool = False) -> str:
    """
    Format MongoDB caller graph into a structured context block.

    For returning callers (returning=True): the bot is explicitly told to use
    this data proactively — address by name, skip re-asking known facts,
    acknowledge prior actions and unresolved items.

    For first-time callers: caller_graph is None so this is never called.
    The returning=False path exists only as a safe fallback.
    """
    if not caller_graph:
        return ""

    nodes = {n["node_id"]: n for n in caller_graph.get("nodes", [])}
    edges = caller_graph.get("edges", [])
    caller_name = caller_graph.get("caller_name")

    facts = []
    preferences = []
    previous_actions = []
    unresolved = []

    if caller_name:
        facts.append(f"- Full name: {caller_name}")

    # De-duplicate node_ids already covered by caller_name
    seen_values = {caller_name} if caller_name else set()

    for edge in edges:
        to_node = nodes.get(edge.get("to_node"))
        if not to_node:
            continue
        relation = edge.get("relation", "")
        value = to_node.get("value", "")
        key = to_node.get("key", "").replace("_", " ").title()

        # Skip duplicates (e.g. person_caller node echoing the name)
        if value in seen_values:
            continue
        seen_values.add(value)

        if relation == "has_preference":
            preferences.append(f"- {key}: {value}")
        elif relation == "has_fact":
            facts.append(f"- {key}: {value}")
        elif relation in ("requested", "scheduled"):
            previous_actions.append(f"- {value}")
        elif relation == "unresolved":
            unresolved.append(f"- {value}")

    if not any([facts, preferences, previous_actions, unresolved]):
        return ""

    if returning:
        lines = [
            "RETURNING CALLER CONTEXT — YOU ALREADY KNOW THIS PERSON:",
            "Use all of the following information naturally throughout the call.",
            "Do NOT ask the caller to repeat any of this — you already have it.",
            "Speak as a human advisor who simply remembers, not as a system reading data.",
        ]
    else:
        lines = ["CALLER CONTEXT:"]

    if facts:
        lines.append("\nKnown facts:")
        lines.extend(facts)
    if preferences:
        lines.append("\nKnown preferences:")
        lines.extend(preferences)
    if previous_actions:
        lines.append("\nWhat they did in previous calls:")
        lines.extend(previous_actions)
    if unresolved:
        lines.append("\nSTILL UNRESOLVED from their last call — address this proactively:")
        lines.extend(unresolved)

    return "\n".join(lines)