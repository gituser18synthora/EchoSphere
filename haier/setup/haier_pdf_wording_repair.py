"""Use the PDF closing with wording appropriate to each cancellation reason.

Source: Haier_Rejection_Call_Flow.pdf, page 2 (Not Haier Product, No ->
Action 2), and page 3 (Action 2 and common closing). A subsequent user-approved
neutral non-Haier closing avoids treating product ownership as service refusal.
The text is localized for the reviewed bot's voice; no routing or outcome changes.
This pure transform does not publish configuration or operate services.
"""
from __future__ import annotations

from copy import deepcopy

from haier.setup.haier_call_audit_repair import BOT_ID, WORKFLOW_ID


PDF_ACTION_2 = "Jaise ki aapko service ki requirement nhi he, me aapki service request ko cancel kr deta hu."
PDF_CLOSING = "Haier me apna kimti samay dene ke liye dhanyevad, aapka din subh rhe."
TEXT = {
    "en": "As you do not require service, I am cancelling your service request. Thank you for giving your valuable time to Haier. Have a good day.",
    "hi": "जैसे कि आपको service की requirement नहीं है, मैं आपकी service request को cancel कर देती हूँ। Haier में अपना कीमती समय देने के लिए धन्यवाद, आपका दिन शुभ रहे।",
}
NON_HAIER_TEXT = {
    "en": "Alright, I am cancelling this request. Thank you for giving your valuable time to Haier. Have a good day.",
    "hi": "ठीक है, मैं आपकी इस request को cancel कर देती हूँ। Haier में अपना कीमती समय देने के लिए धन्यवाद, आपका दिन शुभ रहे।",
}
TEXT_BY_NODE = {
    "n_end_not_haier": NON_HAIER_TEXT,
    "n_end_no_service": TEXT,
}
OUTCOMES = {
    "n_end_not_haier": "cancel_not_haier_product",
    "n_end_no_service": "cancel_no_service",
}
DIRECTIVE = (
    "Speak this fixed PDF Action 2 and common closing in the chosen language. "
    "Do not add a confirmation preface, a company helpline referral, customer-care "
    "instructions, a forwarding statement, a callback promise or any extra sentence."
)
NON_HAIER_DIRECTIVE = (
    "Speak this fixed neutral cancellation and common closing in the chosen language. "
    "A non-Haier-product answer does not mean that the caller refused service. "
    "Do not state that service is not required, add a confirmation preface, a "
    "helpline/customer-care referral, a forwarding statement, a callback promise "
    "or any extra sentence."
)


def apply_pdf_wording_repair(definition: dict) -> dict:
    """Use the approved reason-specific wording while retaining graph and outcomes."""
    if (definition.get("id") != WORKFLOW_ID
            or definition.get("bot_id", definition.get("botId")) != BOT_ID):
        raise ValueError("This wording update is restricted to the reviewed Haier workflow")
    nodes = {node["id"]: node for node in definition.get("nodes", [])}
    for node_id, value in OUTCOMES.items():
        closing = nodes.get(node_id, {})
        marker = nodes.get("m_" + node_id, {})
        outcome = marker.get("config", {})
        if (closing.get("kind") != "end" or marker.get("kind") != "message"
                or outcome.get("setSlots", {}).get("outcome") != value
                or outcome.get("silent") is not True
                or outcome.get("terminalOutcome") is not True
                or not any(edge.get("from") == "m_" + node_id
                           and edge.get("to") == node_id
                           for edge in definition.get("edges", []))):
            raise ValueError(f"The reviewed cancellation branch is missing: {node_id}")
        config = closing.get("config", {})
        if set(config) - {"text", "textByLanguage", "responseMode", "responseDirective"}:
            raise ValueError(f"Review additional closing fields before editing: {node_id}")
    result = deepcopy(definition)
    for target in result["nodes"]:
        if target["id"] in OUTCOMES:
            localized = TEXT_BY_NODE[target["id"]]
            target["config"].update(
                text=localized["en"], textByLanguage=deepcopy(localized),
                responseMode="fixed", responseDirective=(
                    NON_HAIER_DIRECTIVE if target["id"] == "n_end_not_haier" else DIRECTIVE
                ),
            )
    return result
