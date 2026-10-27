"""Opt-in post-call extraction for bot_44a50e8c4146; no live-flow changes.

Seven analyst-filled fields on top of the eight workflow-slot fields of
jeeves/setup/01_structured_summary.py: device collection status, address
change status (+ derived Yes/No, the new address, the update date) and
refund status (+ derived Yes/No). They are evidence-required, so the
post-call processor extracts them with its own focused request
(shared.post_call.analyzer.extract_evidence_fields) and keeps a value only
when the analyst cites a customer turn that carries it. Yes/No/null matches
the platform's existing summary contract; status fields distinguish an
outstanding request from a customer-reported completion.

The rules were tuned on simulated Jeeves calls (tests/fixtures/
jeeves_summary_calls.json): statements made in passing while the flow asks
for a visit date must still be read, a booking alone never settles the
device status, a plain address confirmation is not a change request, and a
date said for the pickup or the handover is never the address-update date.
"""

import copy

BOT_ID = "bot_44a50e8c4146"

PICKUP_RULES = (
    "Context: outbound Airtel Broadband call after the connection was disconnected; the bot "
    "schedules an engineer visit to uninstall and collect (pick up) the customer's device/router. "
    "The customer may mention the device status in passing at ANY step, even while the bot is "
    "asking for a visit date — classify such passing statements too. Decide the FINAL physical "
    "collection status from ALL customer turns (a later clear correction wins). "
    "Yes = the customer states the device has ALREADY been collected / handed over / taken away "
    "(by a courier, engineer or family member): 'maine device already de diya hai', 'kal pickup "
    "ho gaya tha', 'courier wala phone le gaya', 'device already collected', 'phone pehle hi "
    "handover kar diya', 'मैंने डिवाइस पहले ही दे दिया'. "
    "No = the customer EXPLICITLY states the device has not been collected yet: nobody has come "
    "for the pickup, the pickup did not happen, or the device is still with them: 'abhi tak koi "
    "pickup karne nahi aaya', 'pickup nahi hua', 'router abhi mere paas hai'. "
    "null = the customer never states the current status. Booking or confirming a visit day, "
    "giving a date or time, asking whether or when the engineer will come ('kal aayenge na?'), "
    "or promising to hand it over ('kal device de dunga') is NOT a status statement => null. "
    "Also null for genuine uncertainty ('shayad le gaye, confirm nahi'), only disconnection/"
    "uninstallation talk, an unrelated parcel, or only a bot statement. Never infer Yes or No "
    "from a booking, a readback or a bot claim. "
    "Corrections: 'de diya tha... nahi, doosra parcel tha, router mere paas hai' => No. 'abhi nahi "
    "liya', later 'sorry, wife ne kal engineer ko de diya tha' => Yes, citing the later turn."
)
ADDRESS_RULES = (
    "The bot reads back the registered service address and asks the customer to confirm it. "
    "Classify the customer's FINAL position on CHANGING that address by the WORDS they used — "
    "decide by the verb/phrase, not by what the bot asked next: "
    "(1) completion words => reported_updated: 'update kar diya tha', 'update kar diya', 'kar diya "
    "tha', 'kiya tha', 'ho gaya', 'already updated', 'updated hai', 'badal diya', 'बदल दिया', "
    "'अपडेट कर दिया' — the change was ALREADY done (by them or by support), whoever did it, "
    "whatever follows in the same turn ('naya address hi sahi hai' = the new one is correct). "
    "Examples: 'maine kal address update kar diya tha' / 'kal address update kar diya tha' / "
    "'address 25 October ko update kiya tha' / 'address already updated hai' / 'पता पहले ही बदल "
    "दिया है' => reported_updated. A customer claim, not verification. "
    "(2) wanting/wrongness words => requested: 'change kar do', 'badal do', 'update kar do', "
    "'update karna hai', 'galat hai', 'ye mera address nahi hai', 'shift ho gaya hoon', 'I moved', "
    "'the address is wrong', 'naya address X hai, update kar do' — with or without a new address, "
    "even if the bot says it cannot change it on this call. "
    "(3) a retraction or refusal after a change was discussed => not_requested: 'badal do — nahi, "
    "rehne do', 'change nahi chahiye'. ('purana address galat hai' is requested, not this.) "
    "(4) null: a plain confirmation of the registered address with no change discussed ('haan "
    "address sahi hai', 'yes that's correct' — the flow records that separately; never output "
    "not_requested for it), 'agent ne update karne ko bola tha' alone, 'update hua kya?' alone, "
    "no mention. A later clear correction supersedes an earlier answer; a NEW request after an "
    "earlier completed update is requested."
)
UPDATED_ADDRESS_RULES = (
    "Only the replacement/correction address the CUSTOMER spoke, with every component they gave, "
    "in the original script; partial details are fine. The value must name a PLACE (an area, "
    "locality, road, building, flat, city or PIN). Words ABOUT the address that name no place are "
    "not an address => null: 'naya address hi sahi hai', 'address galat hai', 'wahi purana', 'the "
    "address on record is right', 'baad mein bataunga'. Example: Customer \"maine kal address update "
    "kar diya tha, naya address hi sahi hai\" => updated_address null (no place named). null also "
    "when a change is requested or reported complete without a new address. Never copy the "
    "registered address from the bot's readback or the locality, never fill in a missing city/PIN/"
    "house number, never use a rejected/superseded address, a pickup day or a callback date. "
    "Example: Customer \"नया पता 9 Lake Road, Jaipur है, बदल दीजिए\" => \"9 Lake Road, Jaipur\"; cite "
    "that Customer turn as updated_address evidence (its own entry, even when address_update_status "
    "cites the same turn)."
)
ADDRESS_DATE_RULES = (
    "The date the customer gave for the ADDRESS update or address-change request ONLY. The date "
    "words must be in the same sentence/clause as the address change: 'maine kal address update kar "
    "diya tha' => yesterday; 'kal address update karna' => tomorrow; 'address 25 October ko update "
    "kiya tha' => 2026-10-25. Resolve relative words with the call date in Asia/Kolkata and the "
    "tense (past 'kiya tha' = before the call; future 'karna/karunga' = after the call). A date "
    "attached to ANY other event — the pickup/visit day, a callback, when the router was handed over, "
    "a payment — is NOT this date: 'I moved to Powai. I handed over the router last week' => null; "
    "'address badal do, pickup kal karna' => null. Never use the call date, the word 'already' or a "
    "guess; unknown or unclear day/month/year => null. Cite the Customer turn that contains the date "
    "words. A date here always goes with address_update_status requested or reported_updated — a "
    "past-tense update date means reported_updated."
)
REFUND_RULES = (
    "Refund = money back for the broadband connection/deposit/payment. The bot cannot handle refunds "
    "on this call and may deflect or say it has no information; classify from the CUSTOMER's own "
    "words regardless of the bot's reply, even when said in passing at any step (a refund question "
    "asked before the customer answers the bot's question still counts). Decide by the phrase: "
    "(1) demand words => requested: 'paise wapas chahiye', 'wapas kar do', 'wapas karo', 'return kar "
    "do', 'refund chahiye', 'रिफंड चाहिए', 'पैसे वापस', 'I want my money back'. A demand wins over a "
    "complaint in the same breath: 'refund nahi mila, paise wapas karo' => requested. "
    "(2) asking/complaining words without a new demand => status_inquiry: 'refund kab milega', "
    "'kab aayega', 'abhi tak nahi aaya', 'status kya hai', 'request di thi', 'approve hua bola tha, "
    "kab milega', 'रिफंड कब मिलेगा'. Examples: 'mera refund kab milega?' => status_inquiry; 'refund "
    "abhi tak nahi aaya, 15 din ho gaye, kab aayega?' => status_inquiry. "
    "(3) received words => reported_refunded: 'refund mil gaya', 'paise wapas aa gaye', 'mil gaya "
    "hai', 'received'. Processing/approved/initiated is not received. "
    "(4) cancel words => withdrawn: 'refund nahi chahiye', 'request cancel kar do', 'mat karna'. "
    "(5) null: no refund mention; an unrelated payment ('bill ka payment kar diya'); only a bot "
    "offer; 'refund nahi, pickup chahiye'; a CONDITIONAL policy question — any 'agar/if ... to/then "
    "refund milega?' form ('agar main cancel karu to refund milega kya?', 'what if I cancel, will I "
    "get a refund?', 'bas pooch raha hoon') is not a status inquiry even though the connection is "
    "already disconnected. A new demand after an earlier completed refund is requested. Follow the "
    "final clear correction."
)
# Topic words (shared.post_call.structured.mentions_topic /
# date_in_topic_clause): the cited customer turn must mention the field's
# subject — the analyst cannot hang "device not collected" on a refund line
# — and an address-update date must share a clause with an address word.
PICKUP_TOPIC_WORDS = [
    "device", "router", "pickup", "pick up", "pick-up", "phone", "modem", "equipment", "collect",
    "handover", "hand over", "handed", "uninstall", "le gaya", "le gaye", "le liya", "de diya", "de di",
    "mere paas", "डिवाइस", "राउटर", "पिकअप", "दे दिया", "दे दी", "ले गया", "ले गए", "ले लिया", "कलेक्ट", "मेरे पास",
]
ADDRESS_TOPIC_WORDS = ["address", "pata", "पता", "update", "badal", "बदल", "change", "shift", "moved", "correct"]
REFUND_TOPIC_WORDS = ["refund", "paise", "paisa", "payment", "money", "wapas", "return", "रिफंड", "पैसे", "पैसा", "वापस", "पेमेंट"]


def _field(name, label, kind, instructions, **extra):
    return {
        "name": name, "label": label, "type": kind, "allowLlm": True,
        "evidenceRequired": True, "extractionInstructions": instructions,
        **extra,
    }


INTENT_SUMMARY_FIELDS = [
    _field("already_picked_up", "Device already collected", "yes_no", PICKUP_RULES,
           topicWords=PICKUP_TOPIC_WORDS),
    _field("address_update_status", "Address update status", "choice", ADDRESS_RULES,
           options=["requested", "reported_updated", "not_requested"], topicWords=ADDRESS_TOPIC_WORDS),
    _field("address_change_requested", "Address change requested", "yes_no",
           "Computed by the platform from address_update_status: requested => Yes; "
           "reported_updated or not_requested => No; unknown => null.",
           allowLlm=False, derivedFrom="address_update_status",
           values={"requested": "Yes", "reported_updated": "No", "not_requested": "No"}),
    _field("updated_address", "Updated address (customer provided)", "text", UPDATED_ADDRESS_RULES,
           maxLength=1000, requires={"address_update_status": ["requested", "reported_updated"]}),
    _field("address_update_date", "Address update date", "date", ADDRESS_DATE_RULES,
           timezone="Asia/Kolkata", topicWords=ADDRESS_TOPIC_WORDS,
           requires={"address_update_status": ["requested", "reported_updated"]}),
    _field("refund_status", "Refund status (customer reported)", "choice", REFUND_RULES,
           options=["requested", "status_inquiry", "reported_refunded", "withdrawn"],
           topicWords=REFUND_TOPIC_WORDS),
    _field("refund_requested", "Refund requested / outstanding", "yes_no",
           "Computed by the platform from refund_status: requested or status_inquiry => Yes; "
           "reported_refunded or withdrawn => No; unknown => null.",
           allowLlm=False, derivedFrom="refund_status",
           values={"requested": "Yes", "status_inquiry": "Yes", "reported_refunded": "No", "withdrawn": "No"}),
]


def enhance_summary_fields(existing: list[dict]) -> list[dict]:
    """Preserve existing fields exactly; append/update only this feature's keys."""
    additions = {f["name"]: f for f in INTENT_SUMMARY_FIELDS}
    fields = [copy.deepcopy(additions.get(f.get("name"), f)) for f in existing]
    names = {f["name"] for f in fields}
    fields.extend(copy.deepcopy(f) for f in INTENT_SUMMARY_FIELDS if f["name"] not in names)
    return fields
