"""Analyst-filled summary fields for bot_44a50e8c4146; no live-flow changes.

Three fields on top of the slot-only fields of
jeeves/setup/01_structured_summary.py (which owns the workflow-derived
ones, including "Address changed" / "Updated address" since 2026-10-29):

- ``already_picked_up`` — the device's collection status as the CUSTOMER
  stated it. The flow records it when the customer says so at one of its
  hubs (``visit_status`` = ``already_picked_up`` → Yes); for every other
  outcome the field stays open (``openWhenUnmapped``) and the post-call
  analyst may settle it from an explicit statement: Yes (already collected /
  handed over), No (explicitly not yet), null (never stated). A booked or
  pending visit is NOT evidence either way — a scheduled pickup never shows
  as a completed one.
- ``refund_status`` / ``refund_requested`` — the customer's refund position,
  classified from their own words; the derived Yes/No projects it.

The analyst fields are evidence-required: the post-call processor extracts
them with its own focused request (shared.post_call.analyzer
.extract_evidence_fields) and keeps a value only when the analyst cites a
Customer turn AND quotes words that occur verbatim in that turn. The keyword
``topicWords`` guard that used to sit on top of this was removed on
2026-10-29: it discarded a correct value cited on a Devanagari "location
change" turn (cv_21df0c56facf); meaning is the analyst's job, attribution is
the platform's.

The rules were tuned on simulated Jeeves calls (tests/fixtures/
jeeves_summary_calls.json): statements made in passing while the flow asks
for a visit date must still be read, a booking alone never settles the
device status, and a later clear correction wins.
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
    "ho gaya tha', 'courier wala phone le gaya', 'device already collected', 'pickup ho chuka hai', "
    "'phone pehle hi handover kar diya', 'मैंने डिवाइस पहले ही दे दिया', 'राउटर पहले ही ले गए'. "
    "No = the customer EXPLICITLY states the device has not been collected yet: nobody has come "
    "for the pickup, the pickup did not happen, or the device is still with them: 'abhi tak koi "
    "pickup karne nahi aaya', 'pickup nahi hua', 'router abhi mere paas hai', 'अभी तक कोई लेने नहीं आया'. "
    "null = the customer never states the current status. Booking or confirming a visit day, "
    "giving a date or time, agreeing to a readback, asking whether or when the engineer will come "
    "('kal aayenge na?'), or promising to hand it over ('kal device de dunga') is NOT a status "
    "statement => null. Also null for genuine uncertainty ('shayad le gaye, confirm nahi'), only "
    "disconnection/uninstallation talk, an unrelated parcel, or only a bot statement. Never infer "
    "Yes or No from a booking, a readback or a bot claim. "
    "Negation and corrections: read the whole clause — 'nahi, abhi tak nahi liya' => No; 'de diya "
    "tha... nahi, doosra parcel tha, router mere paas hai' => No; 'abhi nahi liya', later 'sorry, "
    "wife ne kal engineer ko de diya tha' => Yes, citing the later turn; two statements that still "
    "contradict each other at the end => null. "
    "Answers: a short reply to the bot's direct question about the collection status is the "
    "customer's own statement — read it together with the question it answers: bot 'kya aap device "
    "pehle hi engineer ko de chuke hain?' + customer 'haan, de diya' => Yes; customer 'nahi, abhi "
    "mere paas hai' => No. A bare 'haan' to any other bot question is null."
)
REFUND_RULES = (
    "Refund = money back for the broadband connection/deposit/payment. The bot cannot handle refunds "
    "on this call and may deflect or say it has no information; classify from the CUSTOMER's own "
    "words regardless of the bot's reply, even when said in passing at any step (a refund question "
    "asked before the customer answers the bot's question still counts). Decide by the phrase: "
    "(1) demand words => requested: 'paise wapas chahiye', 'wapas kar do', 'wapas karo', 'return kar "
    "do', 'refund chahiye', 'रिफंड चाहिए', 'पैसे वापस', 'I want my money back', 'pehle refund do phir "
    "router le jao'. A demand wins over a complaint in the same breath: 'refund nahi mila, paise wapas "
    "karo' => requested. "
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

#: Fields earlier revisions of this module added and this one retires (the
#: address change is now read from the flow's own slots, see 01's
#: ``address_changed`` / ``updated_address``). Removed from the bot's
#: configuration on apply; values stored on older calls stay in their rows.
RETIRED_FIELDS = (
    "address_verified",
    "address_update_status",
    "address_change_requested",
    "address_update_date",
)


def _field(name, label, kind, instructions, **extra):
    return {
        "name": name, "label": label, "type": kind, "allowLlm": True,
        "evidenceRequired": True, "extractionInstructions": instructions,
        **extra,
    }


INTENT_SUMMARY_FIELDS = [
    _field("already_picked_up", "Already Picked Up", "yes_no", PICKUP_RULES,
           source="visit_status", values={"already_picked_up": "Yes"}, openWhenUnmapped=True,
           description=(
               "Was the device already collected / handed over, as the customer stated it? "
               "Yes from the flow when the customer said so at a hub, otherwise only from an "
               "explicit customer statement; No only when the customer explicitly said it has "
               "not been collected; a booked or pending visit decides nothing."
           )),
    _field("refund_status", "Refund status (customer reported)", "choice", REFUND_RULES,
           options=["requested", "status_inquiry", "reported_refunded", "withdrawn"]),
    _field("refund_requested", "Refund requested / outstanding", "yes_no",
           "Computed by the platform from refund_status: requested or status_inquiry => Yes; "
           "reported_refunded or withdrawn => No; unknown => null.",
           allowLlm=False, derivedFrom="refund_status",
           values={"requested": "Yes", "status_inquiry": "Yes", "reported_refunded": "No", "withdrawn": "No"}),
]


def enhance_summary_fields(existing: list[dict]) -> list[dict]:
    """The bot's full summaryFields from an existing list: every field this
    module does not own is preserved exactly and in place, retired fields are
    dropped, and this module's fields are refreshed in place or appended."""
    additions = {f["name"]: f for f in INTENT_SUMMARY_FIELDS}
    fields = [copy.deepcopy(additions.get(f.get("name"), f)) for f in existing
              if isinstance(f, dict) and f.get("name") not in RETIRED_FIELDS]
    names = {f["name"] for f in fields}
    fields.extend(copy.deepcopy(f) for f in INTENT_SUMMARY_FIELDS if f["name"] not in names)
    return fields
