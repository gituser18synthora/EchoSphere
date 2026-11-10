"""Bot-specific prompt and summary amendments for the DEMO Haier repair.

Pure transforms only: publishing remains a separate, explicitly scoped step.
The spelling of brand/product names and every existing disposition mapping
are preserved.
"""
from __future__ import annotations

from copy import deepcopy


PROMPT_AMENDMENT = """# Current-status clarification for rejected requests

Treat technician contact, customer satisfaction, problem resolution and service requirement as separate facts. Negation applies to the fact the customer negated: dissatisfaction does not deny that a technician called or visited. A completed visit does not establish that the problem was fixed. A caller correcting a negative opening with an explicit statement that a call happened has confirmed the call. If the facts remain unclear, ask one brief clarification; do not assert that no contact happened.

For the Unit OK / No Defect Found branch, a clear report that the product still has a problem requires the inconvenience apology and the existing re-dispatch proposal. Do not ask the general service-requirement question again after that clear report. Keep the customer's ability to decline the proposal. Dissatisfaction alone does not establish an unresolved defect: first clarify the current problem. When the customer says the installation or service is completed, preserve the existing checks for anything pending and who completed it. A bare yes or no to a question offering both unresolved and resolved states needs clarification before choosing either outcome.

Only the customer's explicit statements establish contact and problem status. Keep the original rejection reason separate from their current report. Forwarding a request is a proposed follow-up, never proof that a backend dispatch, cancellation or completion has been executed.
"""


def repair_system_prompt(prompt: str) -> str:
    if PROMPT_AMENDMENT in prompt:
        return prompt
    marker = "# Specific Situations"
    if marker not in prompt:
        raise ValueError("Expected Haier system-prompt section is missing")
    return prompt.replace(marker, PROMPT_AMENDMENT + "\n" + marker, 1)


def repair_summary_policy(policy: dict) -> dict:
    result = deepcopy(policy)
    fields = result.get("summaryFields") or []
    by_name = {field["name"]: field for field in fields}
    completion = by_name.get("call_completion")
    outcome = by_name.get("workflow_outcome")
    contact = by_name.get("technician_contact")
    resolution = by_name.get("issue_resolved")
    if not completion or not outcome or not contact or not resolution or outcome.get("source") != "outcome":
        raise ValueError("Expected Haier summary fields are missing")
    # Completion with an unknown installer cannot establish either Haier or
    # non-Haier resolution. An explicit empty mapping is authoritative under
    # the existing summary engine and prevents an analyst from guessing.
    resolution.setdefault("values", {})["close_completed"] = ""
    # A compound call-or-visit question can establish contact without
    # establishing its method. Keep that unknown in both the prose and the
    # structured field instead of forcing inconsistent call/visit guesses.
    contact.update({
        "description": (
            "Customer-reported technician contact. called means an explicitly "
            "confirmed phone call; visited means an explicitly confirmed visit; "
            "no_contact means the customer denied both. A confirmed call does "
            "not prove the absence of a visit. If contact was confirmed without "
            "identifying the method, use null and say only that contact occurred "
            "in the narrative summary. Do not infer the method from an elliptical "
            "answer to a combined call-or-visit question."
        ),
        "extractionInstructions": (
            "Use only the customer's explicit contact facts. A phone call "
            "explicitly received is called; an explicit technician visit is "
            "visited; an explicit denial of both is no_contact. A denial of a "
            "visit alone does not deny a call, or conversely. A bare affirmation "
            "or elliptical past-tense affirmation to a combined call-or-visit "
            "question establishes contact but not its method: return null, "
            "including when the customer then expresses dissatisfaction. Do "
            "not guess a method from grammatical gender or from the bot's "
            "claims. Keep the narrative equally non-specific in this case."
        ),
    })
    # These values are written by the existing terminal outcome markers.
    # Transport caller_hangup can follow an agreed outcome during the final
    # thank-you. It must not turn that completed guided flow into a mid-flow
    # disconnect. With no terminal outcome the analyst still has to inspect
    # the actual transcript and transport facts.
    completion.update({
        "source": "outcome",
        "values": {value: "flow_completed" for value in outcome["options"]},
        "openWhenUnmapped": True,
        "description": (
            "Completion of the guided conversation, separate from which side "
            "disconnected the transport. A recorded terminal workflow outcome "
            "means flow_completed, including caller hangup during the final "
            "thank-you after the decision. This does not assert that the full "
            "closing audio was heard or that any CRM action was executed. "
            "Without a terminal outcome, customer_disconnected_midway means "
            "the caller disconnected while a required decision was pending; "
            "otherwise use ended_without_outcome."
        ),
        "extractionInstructions": (
            "Use the terminal workflow outcome when present. Do not infer a "
            "mid-flow disconnect from end_reason=caller_hangup alone. When "
            "there is no terminal outcome, inspect the pending question, last "
            "customer response and end reason. Never treat a proposed dispatch "
            "as an executed dispatch. In the narrative summary, keep uncertain "
            "answers uncertain as well: an elliptical affirmation to a combined "
            "technician-call-or-visit question establishes contact, but does not "
            "identify whether it was a phone call or a visit. Describe only "
            "confirmed contact unless the customer explicitly identifies the "
            "method. A bare yes or no to a question offering both unresolved "
            "and resolved states does not establish either state. Report the "
            "problem status as unconfirmed in that case, rather than guessing "
            "a defect or a resolution. These rules apply to the narrative and "
            "important facts as well as the structured fields."
        ),
    })
    return result
