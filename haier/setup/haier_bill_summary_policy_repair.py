"""Pure Haier summary configuration for current and historical bill facts.

Apply the returned policy through the intended environment's voice-settings
API. This helper does not change the conversation prompt, publish a release,
connect to a server, or modify historical calls.
"""
from __future__ import annotations

from copy import deepcopy


WARRANTY_DESCRIPTION = (
    "Did the customer explicitly claim that the product is under warranty or "
    "guarantee, or explicitly assert an entitlement to free service? Yes only "
    "for that claim in the customer's own words; No only for an explicit "
    "denial or withdrawal of the claim; otherwise null. Possessing a bill "
    "and questioning service charges do not establish this claim."
)
WARRANTY_RULE = (
    "Require an explicit affirmative warranty/guarantee claim, such as 'warranty "
    "mein hai', 'guarantee mein hai' or 'under warranty', or an explicit claim "
    "that the service should be free, such as 'service free honi chahiye'. "
    "Do not classify a generic charge objection or question as Yes: 'extra "
    "charge kyun', 'jab bill hai to service charges kyun doon', 'mere paas "
    "bill hai' and 'ab bill mil gaya' do not assert warranty or free-service "
    "entitlement. Bill availability, purchase proof and warranty validity are "
    "separate facts. A bot's statement, a negated claim, uncertainty and a "
    "question asking whether warranty applies are not an affirmative claim. "
    "Use the latest clear customer statement; an explicit denial or withdrawal "
    "of warranty is No. Return null when the customer made no explicit claim "
    "or denial. Never infer warranty from a workflow bill slot."
)
BILL_RULE = (
    "Keep historical bill availability separate from present availability. "
    "'Us samay nahi tha, abhi hai' means unavailable then and available now; "
    "a later 'mil gaya' or 'mere paas bill hai' corrects current availability "
    "without rewriting the earlier time. Bare yes/no must answer the actual "
    "preceding question about the same time. Missing mention, uncertainty "
    "or an unresolved contradiction is null. Bill possession never proves a "
    "technician visit, warranty validity, free service or charge acceptance."
)
NARRATIVE_RULE = (
    "For bill-related calls, keep the original rejection reason, historical "
    "bill availability and the customer's latest current bill availability "
    "separate in the narrative and important facts. Where present, workflow "
    "slots bill_available_at_visit and bill_available_now record these "
    "different times; neither slot proves that a visit occurred. A current "
    "bill correction must not be summarized as a continuing missing-bill "
    "problem. The customer's possession of a bill or question about charges "
    "does not establish warranty, an entitlement to free service, willingness "
    "to pay or a refusal of service. Report a charge question as an objection "
    "or unresolved clarification unless the customer explicitly settles it. "
    "Do not claim warranty unless the customer explicitly claims warranty, "
    "guarantee or that service should be free."
)
BILL_FIELDS = (
    {
        "name": "bill_available_at_visit",
        "label": "Bill available at the earlier contact/visit time",
        "type": "yes_no",
        "source": "bill_available_at_visit",
        "values": {"yes": "Yes", "no": "No"},
        "allowLlm": True,
        "evidenceRequired": True,
        "description": (
            "Customer-reported bill availability at the earlier time discussed "
            "for the rejection, separate from whether the bill is available "
            "now. This field does not establish that a technician visited."
        ),
        "extractionInstructions": BILL_RULE + " Classify the earlier time only.",
    },
    {
        "name": "bill_available_now",
        "label": "Bill available now",
        "type": "yes_no",
        "source": "bill_available_now",
        "values": {"yes": "Yes", "no": "No"},
        "allowLlm": True,
        "evidenceRequired": True,
        "description": (
            "Whether the customer currently has the bill, using the latest "
            "clear statement. This is independent of availability at the "
            "earlier time discussed for the original rejection."
        ),
        "extractionInstructions": BILL_RULE + " Classify the current time only.",
    },
)


def repair_summary_policy(policy: dict) -> dict:
    """Amend only bill/warranty evidence and summary instructions, idempotently."""
    result = deepcopy(policy)
    fields = result.get("summaryFields")
    if not isinstance(fields, list) or any(not isinstance(f, dict) for f in fields):
        raise ValueError("The reviewed Haier summary fields are missing")
    names = [field.get("name") for field in fields]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate Haier summary fields are unsafe to amend")
    by_name = {field.get("name"): field for field in fields}
    required = {"warranty_claimed", "call_completion", "customer_statement", "workflow_outcome"}
    if not required <= by_name.keys():
        raise ValueError("The reviewed Haier summary fields are missing")
    warranty = by_name["warranty_claimed"]
    if (warranty.get("source") != "warranty_claimed"
            or warranty.get("type") != "yes_no"
            or warranty.get("values") != {"yes": "Yes"}
            or not warranty.get("allowLlm") or not warranty.get("evidenceRequired")
            or by_name["workflow_outcome"].get("source") != "outcome"):
        raise ValueError("The reviewed Haier warranty evidence contract has changed")
    warranty.update(description=WARRANTY_DESCRIPTION, extractionInstructions=WARRANTY_RULE)
    for spec in BILL_FIELDS:
        existing = by_name.get(spec["name"])
        if existing is not None:
            if existing != spec:
                raise ValueError("An existing bill summary field needs review")
        else:
            # Put both time-specific facts before warranty in conversation order.
            index = next(i for i, field in enumerate(fields) if field["name"] == "warranty_claimed")
            fields.insert(index, deepcopy(spec))
    # Evidence-required fields are omitted from the narrative analyst prompt.
    # call_completion already carries shared narrative evidence instructions.
    completion = by_name["call_completion"]
    instructions = str(completion.get("extractionInstructions") or "")
    if NARRATIVE_RULE not in instructions:
        completion["extractionInstructions"] = (instructions + " " + NARRATIVE_RULE).strip()
    return result
