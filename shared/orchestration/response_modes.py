"""Workflow response-delivery modes — how a node's authored reply is spoken.

The deterministic layer (workflow engine) always decides WHAT happened this
turn: which node ran, which branch was taken, whether an API call succeeded,
which facts are verified. A node's ``responseMode`` decides only how that
outcome is WORDED to the caller:

- ``fixed`` (default): speak the authored text as authored. Existing
  behavior, including reply-language adaptation for callers who switched
  language. Absent/unknown configuration always resolves here, so every
  pre-existing workflow keeps its behavior.
- ``exact``: speak the authored text VERBATIM — no paraphrasing, no
  language adaptation, no generation. For legal / regulatory / security /
  compliance wording and irreversible-action confirmations.
- ``llm_grounded``: the LLM words the reply naturally from the node's
  response directive (``responseDirective``), the authored script and the
  workflow-verified caller context. The authored text remains the spoken
  fallback whenever generation fails, times out, or fails validation. The
  LLM never decides outcomes — a grounded node still only runs when the
  graph deterministically reached it (e.g. an API node's success edge).

This module is tenant- and domain-neutral: modes and directives live in the
workflow definition JSON, never in shared runtime code.
"""

import re

RESPONSE_MODE_FIXED = "fixed"
RESPONSE_MODE_EXACT = "exact"
RESPONSE_MODE_GROUNDED = "llm_grounded"

_KNOWN_MODES = (RESPONSE_MODE_FIXED, RESPONSE_MODE_EXACT, RESPONSE_MODE_GROUNDED)

# Output that looks like UI formatting rather than speech. Voice replies are
# plain sentences: any line-start bullet/heading/numbered-list marker or
# markdown emphasis fails validation and the authored text is spoken instead.
_MARKDOWNISH = re.compile(r"(?:^|\n)\s*(?:[-*•#>]|\d+\.)\s|\*\*|__|```", re.M)
_DIGIT_RUNS = re.compile(r"\d+")


def node_response_mode(config: dict | None) -> str:
    """The node's declared delivery mode; anything unknown is ``fixed``."""
    if not isinstance(config, dict):
        return RESPONSE_MODE_FIXED
    mode = str(config.get("responseMode") or "").strip().lower()
    return mode if mode in _KNOWN_MODES else RESPONSE_MODE_FIXED


def aggregate_response_mode(segment_modes) -> str:
    """One delivery mode for a turn that traversed several speakable nodes.

    ``exact`` wins over everything (approved wording in any segment forbids
    paraphrasing the concatenated reply); otherwise one grounded segment
    makes the whole turn grounded (the authored concatenation stays the
    fallback script); otherwise fixed.
    """
    modes = set(segment_modes or ())
    if RESPONSE_MODE_EXACT in modes:
        return RESPONSE_MODE_EXACT
    if RESPONSE_MODE_GROUNDED in modes:
        return RESPONSE_MODE_GROUNDED
    return RESPONSE_MODE_FIXED


def grounded_delivery_instruction(
    *,
    directives=(),
    script: str = "",
    pending_question: str | None = None,
) -> str:
    """System-prompt suffix for one grounded workflow reply.

    The instruction carries the flow's response goals and the authored
    script as the factual reference; the verified caller context and tool
    results are already present in the surrounding system prompt. It never
    grants the model authority over outcomes.
    """
    lines = [
        "\n\n# Deliver the call flow's outcome (THIS turn)",
        "The structured call flow has already decided what happened this "
        "turn — your only job is the natural spoken wording of it.",
    ]
    goals = [str(d).strip() for d in (directives or ()) if str(d or "").strip()]
    if goals:
        lines.append("Response goals from the flow:")
        lines.extend(f"- {goal}" for goal in goals)
    script = (script or "").strip()
    if script:
        lines.append(
            "The flow's authored script for this step (your factual "
            f'reference; cover its meaning, not its exact words): "{script}"'
        )
    lines.append(
        "Rules (absolute):\n"
        "- Answer the caller's current request first when their last message "
        "asks something; then cover the response goals.\n"
        "- State facts only from the response goals, the script, the "
        "verified caller context and system-verified tool results in this "
        "conversation. Never invent names, dates, amounts, statuses or "
        "outcomes; a fact not present there is unknown — say so honestly.\n"
        "- Never claim an action succeeded (voucher sent, payment done, "
        "booking confirmed, transfer made, property contacted) or that "
        "identity was verified beyond what the goals and script state — the "
        "flow decides outcomes, you only word them.\n"
        "- Keep every name, number, amount, date and option the script "
        "carries.\n"
        "- Speak naturally and concisely for voice: one to three short "
        "sentences; no headings, bullet points, markdown, lists or menus.\n"
        "- Never ask the caller to repeat information already verified on "
        "this call, and do not add an 'anything else?' offer the script "
        "does not make."
    )
    if pending_question and str(pending_question).strip():
        lines.append(
            "The flow is WAITING for the caller's answer to this question: "
            f'"{str(pending_question).strip()}". Your reply MUST end by '
            "asking exactly this — the same request with the same options, "
            "in the caller's language. Never answer it on the caller's "
            "behalf and never replace it with acknowledgements or progress "
            "filler such as 'please wait'."
        )
    return "\n".join(lines)


def validate_grounded_reply(
    script: str,
    generated: str,
    language: str | None = None,
    *,
    require_question: bool = False,
    must_include=(),
    language_check=None,
) -> bool:
    """Whether a grounded generation may be spoken instead of the script.

    Structural checks only — anything they cannot prove falls back to the
    authored text:
    - non-empty and not disproportionately longer than the script;
    - no markdown/bullet/menu formatting (voice replies are plain speech);
    - still a QUESTION when the flow is waiting on one (progress filler
      that swallows the pending ask fails here);
    - every literal digit run in the script survives verbatim;
    - every ``must_include`` literal survives (case-insensitive) — authors
      should list only language-neutral literals (IDs, names, brand words),
      since a translated reply cannot carry an English phrase;
    - the optional ``language_check(text, language)`` callable confirms the
      output is written in the conversation language.
    """
    generated = (generated or "").strip()
    script = (script or "").strip()
    if not generated:
        return False
    if len(generated) > max(400, 3 * len(script)):
        return False
    if _MARKDOWNISH.search(generated):
        return False
    if require_question and "?" not in generated:
        return False
    if not all(run in generated for run in _DIGIT_RUNS.findall(script)):
        return False
    lowered = generated.casefold()
    if not all(
        str(item).casefold() in lowered
        for item in (must_include or ())
        if str(item or "").strip()
    ):
        return False
    if language and callable(language_check):
        try:
            if not language_check(generated, language):
                return False
        except Exception:  # noqa: BLE001 — a checker bug must fail closed
            return False
    return True
