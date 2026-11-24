"""Authored dialogue metadata for the reviewed Haier follow-up workflow.

Initial speech retains the complete complaint introduction. A resumed pending
question uses a separately authored short prompt, and the primary named-caller
question explicitly opts in to the runtime's identity confirmation contract.
This pure transform neither publishes configuration nor changes any service.
"""
from __future__ import annotations

from copy import deepcopy

from haier.setup.haier_call_audit_repair import BOT_ID, WORKFLOW_ID


VERSION = 1
MARKER = "haierDialogueRegressionRepairVersion"
INTRO_PROFILES = {
    "n_complaint_intro": "call_or_visit",
    "n_intro_bill": "visit",
    "n_intro_refused_visit": "call",
    "n_intro_not_delivered": "call",
    "n_intro_ecom_damaged": "call",
    "n_intro_refused_pay": "visit",
    "n_intro_unit_ok": "call_or_visit",
    "n_intro_not_haier": "call_or_visit",
    "n_v2_intro_no_response": "call_or_visit",
}
RESUME_PROMPTS = {
    "call_or_visit": {
        "en": "Did you receive any call or visit from a Haier technician?",
        "hi": "क्या आपको Haier के technician की तरफ़ से कोई call आया था या कोई visit हुई थी?",
    },
    "visit": {
        "en": "Did a Haier technician visit your place?",
        "hi": "क्या Haier के technician की तरफ़ से आपके यहाँ कोई visit हुई थी?",
    },
    "call": {
        "en": "Did you receive a call from a Haier technician?",
        "hi": "क्या आपको Haier के technician की तरफ़ से कोई call आया था?",
    },
}
_INITIAL_QUESTIONS = {
    "call_or_visit": "May I know whether you received any call or visit from a Haier technician?",
    "visit": "May I know whether a Haier technician visited your place?",
    "call": "May I know whether you received a call from a Haier technician?",
}


def apply_dialogue_regression_repair(definition: dict) -> dict:
    """Add reviewed semantic metadata without changing speech or graph routes."""
    if definition.get("id") != WORKFLOW_ID or definition.get("bot_id", definition.get("botId")) != BOT_ID:
        raise ValueError("This repair is restricted to the reviewed Haier workflow")
    nodes = {node["id"]: node for node in definition.get("nodes", [])}
    gate = nodes.get("n_identity_gate", {})
    config = gate.get("config", {})
    if config.get("haierFollowupRepairVersion") != 1 or config.get("haierFollowupConsentRepairVersion") not in (1, 2, 3, 4):
        raise ValueError("Apply reviewed Haier follow-up and consent repairs first")
    if config.get(MARKER) == VERSION:
        return deepcopy(definition)
    if config.get(MARKER) is not None:
        raise ValueError("Review the existing dialogue repair version before editing")
    outcomes = config.get("entity", {}).get("synonymPatterns", {})
    if gate.get("kind") != "ask" or config.get("variable") != "identity_answer" or not {"yes", "no"} <= set(outcomes):
        raise ValueError("Reviewed named-caller identity confirmation is missing")
    if config.get("confirmationRole") not in (None, "identity") or config.get("confirmationValues") not in (None, {"confirmed": "yes", "denied": "no"}):
        raise ValueError("Review the existing identity confirmation contract before editing")

    # Validate authored question semantics before attaching resume metadata. Do
    # not infer the pending question by splitting speech at punctuation.
    for node_id, profile in INTRO_PROFILES.items():
        node = nodes.get(node_id, {})
        authored = node.get("config", {})
        localized = authored.get("textByLanguage", {})
        if (node.get("kind") != "intent"
                or not str(authored.get("prompt", "")).startswith("This call is regarding ")
                or not str(authored.get("prompt", "")).endswith(_INITIAL_QUESTIONS[profile])
                or localized.get("en") != authored.get("prompt")
                or not str(localized.get("hi", "")).endswith(RESUME_PROMPTS[profile]["hi"])):
            raise ValueError(f"Reviewed combined introduction is missing: {node_id}")
        if any(key in authored for key in ("resumePrompt", "resumePromptByLanguage")):
            raise ValueError(f"Review the existing resume prompt before editing: {node_id}")

    result = deepcopy(definition)
    nodes = {node["id"]: node for node in result["nodes"]}
    for node_id, profile in INTRO_PROFILES.items():
        prompts = RESUME_PROMPTS[profile]
        nodes[node_id]["config"].update(
            resumePrompt=prompts["en"], resumePromptByLanguage=deepcopy(prompts),
        )
    nodes["n_identity_gate"]["config"].update(
        confirmationRole="identity",
        confirmationValues={"confirmed": "yes", "denied": "no"},
        **{MARKER: VERSION},
    )
    return result
