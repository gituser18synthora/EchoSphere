"""Opt-in post-call extraction for bot_44a50e8c4146; no live-flow changes.

Keep Yes/No/null to match the platform's existing summary contract. Status
fields distinguish outstanding requests from customer-reported completion.
"""

import copy

BOT_ID = "bot_44a50e8c4146"

PICKUP_RULES = (
    "Determine the final physical collection status of the customer's device. "
    "Read ALL customer turns; a clear later correction replaces earlier answers. "
    "Apply these mutually exclusive rules to the final position: "
    "1. Yes = customer confirms actual completed handover/collection, including a "
    "courier or family member handing it over. 'Maine device already de diya hai', "
    "'Pickup ho chuka hai', 'Kal courier wala le gaya', 'Device already collected', "
    "'Phone pehle hi handover kar diya', 'डिवाइस कल दे दिया था' => Yes. "
    "2. No = customer explicitly denies collection OR says they still possess it. "
    "This rule takes precedence over a simultaneous future pickup request. "
    "'Pickup karwa do, abhi router mere paas hai', 'अभी device मेरे पास है, कल ले जाना', "
    "'Pickup nahi hua', 'clothes parcel le gaye, router yahin hai' => No. "
    "3. null = only a future request/promise without a present/past status statement "
    "('कल engineer को device दे दूँगा', 'kal pickup karna'); OR genuine uncertainty "
    "('shayad le gaye, confirm nahi'); OR only a status question, booking, "
    "disconnection/uninstallation, unrelated handover, or a bot claim. Never infer Yes. "
    "Corrections: 'de diya tha—nahi, doosra parcel tha, router mere paas hai' => No. "
    "Customer 'Not collected yet'; later Customer 'Sorry, my wife confirmed she "
    "gave the router to the engineer yesterday' => Yes, cite the LATER customer "
    "turn. For every non-null value, cite the supporting Customer turn number."
)
ADDRESS_RULES = (
    "Classify the final customer position: requested = registered address is wrong "
    "or they want to change/correct it, including a partial new address or no new "
    "address; reported_updated = customer explicitly says the update is already "
    "done (not backend verification); not_requested = customer explicitly confirms "
    "the current address, declines a change, or retracts an earlier change request. "
    "No mention/uncertain/hypothetical => null. 'Purana address galat hai' and "
    "'address badal do' => requested; 'मैंने कल पता अपडेट कर दिया था' and "
    "'address already updated hai' => reported_updated. 'Agent ne update karne "
    "ko bola tha' alone is not completion. 'update hua kya?' alone is ambiguous. "
    "A bot promise 'I will update it' never establishes completion. Follow the "
    "last clear correction and distinguish a new request after an earlier update. "
    'Example: Customer "पता पहले ही बदल दिया है" => '
    'structured_fields {"address_update_status":"reported_updated"}; '
    'cite that Customer turn number as address_update_status evidence. '
    "A completed update with a spoken date is still reported_updated and the "
    "spoken update date belongs in address_update_date."
    " A request without a new address is still requested; inability to give "
    "the details does not negate the request. Retraction means not_requested. "
    'Example: Customer "पता बदल दो, नया अभी नहीं बता सकता"; Bot "I will update it" '
    '=> requested, updated_address null; quote the customer request. '
    'Example: Customer "बदल दो—नहीं, रहने दो" => not_requested.'
)
REFUND_RULES = (
    "requested = new/renewed clear demand for money back: 'Mujhe mere paise wapas "
    "chahiye', 'Mera payment return kar do', 'I want my money back', 'मुझे रिफंड "
    "चाहिए'. status_inquiry = customer asks when/where an outstanding refund is: "
    "'Refund kab milega?', 'refund request pehle di thi, status kya hai?'. "
    "reported_refunded = customer confirms money was actually received ('paise "
    "wapas mil gaye'); processing/approved/initiated is not received. withdrawn = "
    "customer retracts a request or declines refund ('refund nahi chahiye'). "
    "Unrelated payments, a hypothetical policy question ('agar cancel karu to "
    "refund milega?'), 'refund nahi, pickup chahiye', bot-only offers and uncertainty "
    "do not establish refund intent. 'Refund nahi mila, paise wapas karo' is "
    "requested, not denial of intent. A request for another refund after a "
    "previous completed refund is requested. Resolve corrections in context."
)


def _field(name, label, kind, instructions, **extra):
    return {
        "name": name, "label": label, "type": kind, "allowLlm": True,
        "evidenceRequired": True, "extractionInstructions": instructions,
        **extra,
    }


INTENT_SUMMARY_FIELDS = [
    _field("already_picked_up", "Device already collected", "yes_no", PICKUP_RULES),
    _field("address_update_status", "Address update status", "choice", ADDRESS_RULES,
           options=["requested", "reported_updated", "not_requested"]),
    _field("address_change_requested", "Address change requested", "yes_no",
           "Match address_update_status: Yes for requested (including incorrect existing "
           "address), No for reported_updated/not_requested, null if status "
           "unknown. A completed-only report is not a new outstanding change request. "
           + ADDRESS_RULES, allowLlm=False, derivedFrom="address_update_status",
           values={"requested": "Yes", "reported_updated": "No", "not_requested": "No"}),
    _field("updated_address", "Updated address (customer provided)", "text",
           "Copy only the final customer-provided replacement/correction address, "
           "including partial details. Keep all supplied components and original script. "
           "Never fill missing city/PIN/house number from the registered address, "
           "bot readback, locality slot, or assumptions. A confirmed bot readback "
           "may resolve a correction only when originally supplied by the customer. "
           "Do not use a rejected/superseded address or a callback date. No new "
           "address spoken => null even if an update is requested or reported complete. "
           'Example: Customer "नया पता 9 Lake Road, Jaipur है, बदल दीजिए" => '
           'structured_fields {"updated_address":"9 Lake Road, Jaipur"}; '
           'cite that Customer turn number as updated_address evidence. '
           "The address needs its own evidence entry even if address_update_status cites the same turn.",
           maxLength=1000,
           requires={"address_update_status": ["requested", "reported_updated"]}),
    _field("address_update_date", "Address update date", "date",
           "Date explicitly linked to the address change request or reported completed "
           "update, not pickup/callback/refund date. Resolve explicit relative dates "
           "using the call date in Asia/Kolkata and tense: 'kal update kiya tha' = "
           "yesterday; 'kal update karna' = tomorrow. For 'parso' use tense too. "
           "Never substitute call date just because a change is discussed or 'already' "
           "is said. No date or uncertain day/month/year => null. Do not guess a "
           "missing year. This date records the stated update/request date, not "
           "independent proof of completion. A date given for a requested future "
           "update is valid too. Output an ISO date AND cite the Customer turn "
           "containing the original address-update date expression.", timezone="Asia/Kolkata",
           requires={"address_update_status": ["requested", "reported_updated"]}),
    _field("refund_status", "Refund status (customer reported)", "choice", REFUND_RULES,
           options=["requested", "status_inquiry", "reported_refunded", "withdrawn"]),
    _field("refund_requested", "Refund requested / outstanding", "yes_no",
           "Match refund_status: Yes for requested or status_inquiry (clear outstanding "
           "refund intent); No for reported_refunded or withdrawn; null when intent "
           "is not settled, including unrelated payment or hypothetical policy talk. "
           + REFUND_RULES, allowLlm=False, derivedFrom="refund_status",
           values={"requested": "Yes", "status_inquiry": "Yes", "reported_refunded": "No", "withdrawn": "No"}),
]


def enhance_summary_fields(existing: list[dict]) -> list[dict]:
    """Preserve existing fields exactly; append/update only this feature's keys."""
    additions = {f["name"]: f for f in INTENT_SUMMARY_FIELDS}
    fields = [copy.deepcopy(additions.get(f.get("name"), f)) for f in existing]
    names = {f["name"] for f in fields}
    fields.extend(copy.deepcopy(f) for f in INTENT_SUMMARY_FIELDS if f["name"] not in names)
    return fields
