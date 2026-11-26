"""Semantic answer options for every question of the Haier rejected-WO workflow.

Review of the 77 DEMO calls of 2026-11-25 (``haier/qa/REPORT_2026-11-25.md``):
each question recognised its answer through its own word lists (edge labels
and route capture patterns). A reply outside those lists was re-asked or sent
down the wrong branch, while the decision model had read it correctly.

This pure transform gives every question the same contract instead. Each
intent hub receives ``answerOptions``: what each possible answer MEANS and
where it leads. The runtime then resolves a reply in one order everywhere:

1. a reaction that answers nothing ("hello", "hmm", "haan boliye", "thank
   you") asks the question again;
2. the option the decision model chose for the whole reply;
3. a plain yes / no ("ji", "haan", "theek hai", "nahi", with or without
   sir / ma'am) through the option's signal;
4. the existing labels and capture patterns, for a turn the model did not
   judge (timeout), without letting a bare yes/no word inside a longer
   sentence decide.

The start node also declares ``closingFollowups``: after the closing line the
bot answers at most two follow-ups, each followed by the authored closing
courtesy and the hang-up (2026-11-25: calls ran up to 170 s past the closing).
Its ``behavior.max_node_steps`` is raised to ``MAX_NODE_STEPS``: four questions
have routing chains longer than the platform's 30 nodes per turn, and a reply
that matched one of their last captures ended on the platform error line.
And its ``doNotCallNode`` names this flow's own repeated-calls closing, so a
"do not call me again" request ends on that line at every step.

No route is invented: an option may only lead where that hub could already go
(an edge or a route of its correction chain), and a plain yes / no keeps the
destination it has today (``_YES_NO_TARGETS`` is the recorded behaviour of the
reviewed definition; ``tests/unit/test_haier_answer_options_repair.py`` checks
it against a replay). No spoken text changes. The helper never publishes
configuration and never restarts a service.
"""
from __future__ import annotations

from copy import deepcopy
import re

VERSION = 1
MARKER = "haierAnswerOptionsVersion"
CLOSING_FOLLOWUPS = 2
# The interpreter walks at most this many nodes per turn (the platform default
# is 30; the knob is the workflow's own ``behavior.max_node_steps``). Four
# questions of this flow have routing chains of 33 to 36 conditions behind
# them, so a reply that matched one of the last captures ("nahi, koi nahi
# aaya" at the unit-ok contact question) ran out of budget and the caller
# heard the platform error line. The longest chain is 36 nodes; 120 leaves
# room for further captures and still stops an authored loop.
MAX_NODE_STEPS = 120
# The engine passes at most this many characters of an option's meaning to the
# decision model (workflow_engine._MAX_OPTION_MEANING_CHARS). A longer meaning
# is cut silently, and it is the END that is lost: the first revision's
# "... is NOT agreement" / "... is NOT this" clauses never reached the model.
MAX_MEANING_CHARS = 200
# "Do not call me again" at ANY point of the call (before the first question,
# at a question, after the closing line) ends on this flow's own closing: the
# apology for the repeated calls and the outcome ``repeat_calls_noted``, which
# the summary reports as "Do-not-call requested: Yes". The platform's generic
# line promises a do-not-call list; the platform keeps no such list (it only
# records a disposition), so that promise is not made on these calls.
DO_NOT_CALL_NODE = "m_n_v2_end_repeat_calls"

_ELSE_LABELS = {"else", "other", "default", "fallback", "no match", "otherwise"}

# Where a plain "हाँ" / "नहीं" leads today at each hub (replay of the reviewed
# local definition v96 = DEMO v85, 2026-11-25). "(stay)" = the hub kept waiting.
_YES_NO_TARGETS: dict[str, tuple[str, str]] = {
    'n_wp_available_q': ('n_wp_identity_ask', 'm_n_end_wp_unavailable'),
    'n_complaint_intro': ('n_service_need', 'n_apologize_no_visit'),
    'n_apologize_no_visit': ('n_redispatch', 'm_n_end_no_service'),
    'n_redispatch': ('m_n_end_redispatch_thanks', 'n_service_need_clarify'),
    'n_reason_bill_q': ('n_haier_bill_state_past_yes', 'n_haier_bill_state_past_no'),
    'n_bill_charge_q': ('n_redispatch', 'm_n_v2_end_payment_declined'),
    'n_reason_refused_q': ('n_service_need', 'n_service_need'),
    'n_reason_delivered_q': ('n_v2_delivery_check', 'm_n_end_not_delivered'),
    'n_reason_ecom_q': ('n_ecom_cancel_info', 'n_service_need'),
    'n_ecom_cancel_info': ('m_n_end_thanks', 'm_n_end_thanks'),
    'n_reason_refused_pay_q': ('n_rtp_warn_q', 'n_service_need'),
    'n_rtp_warn_q': ('n_redispatch', 'm_n_v2_end_payment_declined'),
    'n_charges_info_q': ('n_redispatch', 'm_n_v2_end_payment_declined'),
    'n_reason_unit_ok_q': ('n_unit_ok_pending_apology', 'n_resolved_how_q'),
    'n_resolved_how_q': ('(stay)', 'n_haier_followup_n_resolved_how_q_clarify'),
    'n_reason_not_haier_q': ('n_service_need', 'm_n_end_not_haier'),
    'n_service_need': ('n_redispatch', 'm_n_end_no_service'),
    'n_intro_bill': ('n_haier_bill_state_contact_visit', 'n_haier_bill_state_contact_no_visit'),
    'n_intro_refused_visit': ('n_reason_refused_q', 'n_apology_call'),
    'n_intro_not_delivered': ('n_reason_delivered_q', 'n_apology_call'),
    'n_intro_ecom_damaged': ('n_reason_ecom_q', 'n_apology_call'),
    'n_intro_refused_pay': ('n_reason_refused_pay_q', 'n_apology_visit'),
    'n_intro_unit_ok': ('n_reason_unit_ok_q', 'n_apologize_no_visit'),
    'n_intro_not_haier': ('n_reason_not_haier_q', 'n_apologize_no_visit'),
    'n_apology_visit': ('n_redispatch', 'm_n_end_no_service'),
    'n_apology_call': ('n_redispatch', 'm_n_end_no_service'),
    'n_intro_bill_clarify': ('n_haier_bill_state_contact_visit', 'n_haier_bill_state_contact_no_visit'),
    'n_intro_refused_pay_clarify': ('n_reason_refused_pay_q', 'n_apology_visit'),
    'n_intro_refused_visit_clarify': ('n_reason_refused_q', 'n_apology_call'),
    'n_intro_not_delivered_clarify': ('n_reason_delivered_q', 'n_apology_call'),
    'n_intro_ecom_damaged_clarify': ('n_reason_ecom_q', 'n_apology_call'),
    'n_intro_unit_ok_clarify': ('n_reason_unit_ok_q', 'n_apologize_no_visit'),
    'n_intro_not_haier_clarify': ('n_reason_not_haier_q', 'n_apologize_no_visit'),
    'n_complaint_intro_clarify': ('n_service_need', 'n_apologize_no_visit'),
    'n_reason_bill_q_clarify': ('n_haier_bill_state_past_yes', 'n_haier_bill_state_past_no'),
    'n_bill_charge_q_clarify': ('n_redispatch', 'm_n_v2_end_payment_declined'),
    'n_reason_refused_q_clarify': ('n_service_need', 'n_service_need'),
    'n_reason_delivered_q_clarify': ('n_v2_delivery_check', 'm_n_end_not_delivered'),
    'n_reason_ecom_q_clarify': ('n_ecom_cancel_info', 'n_service_need'),
    'n_reason_refused_pay_q_clarify': ('n_rtp_warn_q', 'n_service_need'),
    'n_reason_unit_ok_q_clarify': ('n_resolved_how_q', 'n_unit_ok_pending_apology'),
    'n_reason_not_haier_q_clarify': ('n_service_need', 'm_n_end_not_haier'),
    'n_service_need_clarify': ('n_redispatch', 'm_n_end_no_service'),
    'n_apologize_no_visit_clarify': ('n_redispatch', 'm_n_end_no_service'),
    'n_apology_visit_clarify': ('n_redispatch', 'm_n_end_no_service'),
    'n_apology_call_clarify': ('n_redispatch', 'm_n_end_no_service'),
    'n_bill_dealer_q': ('n_end_bill_dealer', 'n_bill_charge_q'),
    'n_bill_dealer_q_clarify': ('n_end_bill_dealer', 'n_bill_charge_q'),
    'n_end_bill_dealer': ('m_n_end_redispatch_thanks', 'n_service_need_clarify'),
    'n_wp_haier_q': ('n_wp_complaint_q', 'm_n_end_wp_no_haier'),
    'n_wp_haier_q_clarify': ('n_wp_complaint_q', 'm_n_end_wp_no_haier'),
    'n_wp_complaint_q': ('n_service_need', 'm_n_end_wp_no_complaint'),
    'n_wp_complaint_q_clarify': ('n_service_need', 'm_n_end_wp_no_complaint'),
    'n_v2_completed_check': ('n_v2_pending_case_check', 'n_v2_completed_by_q'),
    'n_v2_pending_case_check': ('m_n_v2_end_pending_case', 'm_n_v2_other_issue'),
    'n_v2_existing_request_check': ('m_n_v2_end_existing_request', 'n_service_need'),
    'n_v2_paid_status_check': ('n_v2_completed_by_q', 'n_service_need'),
    'n_v2_future_when': ('n_v2_future_when_clarify', 'n_v2_future_when_clarify'),
    'n_future_confirm': ('m_n_end_service_later', 'n_service_need'),
    'n_v2_delivery_check': ('n_redispatch', 'n_v2_future_when'),
    'n_v2_no_response_status': ('n_v2_no_response_status_clarify', 'n_v2_no_response_status_clarify'),
    'n_v2_intro_no_response': ('n_v2_no_response_status', 'n_apologize_no_visit'),
    'n_v2_completed_check_clarify': ('n_v2_pending_case_check', 'n_v2_completed_by_q'),
    'n_v2_pending_case_check_clarify': ('m_n_v2_end_pending_case', 'm_n_v2_other_issue'),
    'n_v2_existing_request_check_clarify': ('m_n_v2_end_existing_request', 'n_service_need'),
    'n_v2_future_when_clarify': ('m_n_v2_end_unverified', 'm_n_v2_end_unverified'),
    'n_v2_delivery_check_clarify': ('n_redispatch', 'n_v2_future_when'),
    'n_v2_no_response_status_clarify': ('n_v2_completed_check', 'n_service_need'),
    'n_v2_intro_no_response_clarify': ('n_v2_no_response_status', 'n_apologize_no_visit'),
    'n_v2_paid_status_check_clarify': ('n_v2_completed_by_q', 'n_service_need'),
    'n_v2_completed_by_q': ('m_n_v2_end_completed_haier', 'm_n_v2_end_completed_other'),
    'n_warranty_claim_q': ('n_redispatch', 'm_n_end_no_service'),
    'n_warranty_claim_q_clarify': ('n_redispatch', 'm_n_end_no_service'),
    'n_haier_audit_redispatch_confirm': ('m_n_end_redispatch_thanks', 'n_service_need_clarify'),
    'n_haier_followup_n_resolved_how_q_clarify': ('(stay)', 'm_n_v2_end_unverified'),
    'n_haier_followup_n_v2_completed_by_q_clarify': ('m_n_v2_end_completed_haier', 'm_n_v2_end_completed_other'),
    'n_haier_bill_state_service_need': ('n_redispatch', '(stay)'),
    'n_haier_bill_state_service_need_clarify': ('n_redispatch', '(stay)'),
    'n_haier_bill_state_now_q': ('n_haier_bill_state_current_yes', 'n_haier_bill_state_current_no'),
    'n_haier_bill_state_now_clarify': ('n_haier_bill_state_current_yes', 'n_haier_bill_state_current_no'),
    'n_haier_bill_state_visit_q': ('n_haier_bill_state_contact_visit', 'n_haier_bill_state_contact_no_visit'),
    'n_haier_bill_state_visit_clarify': ('n_haier_bill_state_contact_visit', 'n_haier_bill_state_contact_no_visit'),
}

# A plain no at the bill-state service question used to keep the hub waiting;
# it is the same question as every other service-need step and its no edge
# already leads to the no-service closing.
_YES_NO_OVERRIDES = {
    "n_haier_bill_state_service_need": (None, "m_n_end_no_service"),
    "n_haier_bill_state_service_need_clarify": (None, "m_n_end_no_service"),
}

_CONTACT_ANY = (
    "A Haier technician did call them or visit them: some contact happened, whatever they add about how it went",
    "No call and no visit from any Haier technician happened at all",
)
_CONTACT_CALL = (
    "A Haier technician did call them (a phone call or a visit happened)",
    "They received no call from any Haier technician",
)
_CONTACT_VISIT = (
    "A Haier technician did come to their place",
    "No technician came to their place. This includes: a call came but nobody visited",
)
_SERVICE_NEED = (
    "They need or want the service, installation or repair for this request, including: the problem is still "
    "there, the work was not done or not done properly, or someone should come and check",
    "They do not need the service for this request (no requirement now, not needed any more)",
)
_PERMISSION = (
    "They agree that the visit request be forwarded (yes, okay, go ahead), with or without a preferred day or "
    "request. NOT this: the product works or has no problem, or they will handle it themselves",
    "They do not want the request forwarded or a technician visit, including: the product works now, there "
    "is no problem any more, or they will handle it themselves",
)
_PAY = (
    "They are ready or agree to pay the service charges",
    "They are not ready to pay or refuse to pay the charges",
)

# hub (and its "_clarify" twin unless listed itself) -> (yes meaning, no meaning)
_YES_NO_MEANINGS: dict[str, tuple[str, str]] = {
    "n_complaint_intro": _CONTACT_ANY,
    "n_intro_unit_ok": _CONTACT_ANY,
    "n_intro_not_haier": _CONTACT_ANY,
    "n_v2_intro_no_response": _CONTACT_ANY,
    "n_intro_refused_visit": _CONTACT_CALL,
    "n_intro_not_delivered": _CONTACT_CALL,
    "n_intro_ecom_damaged": _CONTACT_CALL,
    "n_intro_bill": _CONTACT_VISIT,
    "n_intro_refused_pay": _CONTACT_VISIT,
    "n_service_need": _SERVICE_NEED,
    "n_apologize_no_visit": _SERVICE_NEED,
    "n_apology_visit": _SERVICE_NEED,
    "n_apology_call": _SERVICE_NEED,
    "n_haier_bill_state_service_need": _SERVICE_NEED,
    "n_redispatch": _PERMISSION,
    "n_haier_audit_redispatch_confirm": _PERMISSION,
    "n_end_bill_dealer": (
        "They agree or acknowledge that the request is being re-dispatched (okay, fine, yes)",
        "They do not agree to the re-dispatch",
    ),
    "n_warranty_claim_q": (
        "They agree to the request being re-dispatched so that a technician visits and checks the warranty",
        "They do not want that visit or do not need the service",
    ),
    "n_v2_delivery_check": (
        "They want the installation done now",
        "They do not want the installation right now",
    ),
    "n_reason_bill_q": (
        "They had the bill available when the technician visited",
        "They did not have the bill available at that visit",
    ),
    "n_bill_charge_q": _PAY,
    "n_rtp_warn_q": _PAY,
    "n_charges_info_q": _PAY,
    "n_reason_refused_q": (
        "They had declined the service, or asked the technician to come later, when the technician called",
        "They had not declined the service",
    ),
    "n_reason_delivered_q": (
        "The product has been delivered now",
        "The product is still not delivered",
    ),
    "n_reason_ecom_q": (
        "The product was delivered in a damaged condition",
        "The product was not damaged when it was delivered",
    ),
    "n_reason_refused_pay_q": (
        "They did not pay or declined to pay at the technician's visit (too expensive, could not afford it). "
        "If their reason is that it is under warranty or should be free, the answer is warranty",
        "They had not refused to pay",
    ),
    "n_reason_unit_ok_q": (
        "The product still has a problem or is not working properly",
        "The product has no problem at present, it is working (even if they want to watch it for a few days)",
    ),
    # The clarification asks the opposite question ("has the issue been resolved now?").
    "n_reason_unit_ok_q_clarify": (
        "The issue is resolved for now, the product is working at present (even if they want to watch it "
        "for a few days)",
        "The issue is not resolved, the product still has a problem",
    ),
    "n_reason_not_haier_q": (
        "They do have the Haier product this request was raised for",
        "They do not have a Haier product, or it is another brand",
    ),
    "n_wp_available_q": (
        "The named customer is there and can come to the phone",
        "The named customer is not available right now",
    ),
    "n_wp_haier_q": ("They have a Haier product", "They have no Haier product"),
    "n_wp_complaint_q": (
        "They had raised a service or installation request",
        "They had not raised any request",
    ),
    "n_v2_completed_check": (
        "Some installation or service work is still pending",
        "Nothing is pending, the work is fully done",
    ),
    "n_v2_pending_case_check": (
        "A request for that repair, replacement or part is already open",
        "No request is open for it",
    ),
    "n_v2_existing_request_check": (
        "Their new request or booked visit is still open",
        "That request is not open any more",
    ),
    "n_v2_paid_status_check": (
        "The product's problem is resolved now",
        "The problem is still not resolved",
    ),
    "n_future_confirm": (
        "Correct: they need the installation or service later, not now",
        "Not correct: they need it now",
    ),
    "n_v2_completed_by_q": (
        "The installation or service was done by a Haier technician",
        "It was done by someone else: a local or private technician, the dealer, or themselves",
    ),
    "n_haier_followup_n_v2_completed_by_q_clarify": (
        "The installation or service was done by a Haier technician",
        "It was done by someone else: a local or private technician, the dealer, or themselves",
    ),
    "n_bill_dealer_q": (
        "They can get the bill from the dealer",
        "They cannot get the bill from the dealer",
    ),
    "n_haier_bill_state_now_q": (
        "They have the bill available now",
        "They do not have the bill now",
    ),
    "n_haier_bill_state_now_clarify": (
        "They have the bill available now",
        "They do not have the bill now",
    ),
    "n_haier_bill_state_visit_q": (
        "The technician did also come to their place",
        "The technician did not come, it was only a phone call",
    ),
    "n_haier_bill_state_visit_clarify": (
        "The technician did come to their place",
        "The technician did not come, it was only a phone call",
    ),
}

# Questions a yes / no does not answer: (option id, target, meaning).
_CHOICE_OPTIONS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "n_resolved_how_q": (
        ("haier_technician", "m_n_end_resolved_tech",
         "Haier's people fixed it: a Haier technician, the company's mechanic or service person "
         "('Haier wale', 'company wale', 'Haier machine wale' came and repaired it)"),
        ("other_or_self", "m_n_end_resolved_auto",
         "It got fixed on its own, or the customer or a local / outside technician fixed it: not Haier"),
        ("not_resolved", "n_service_need",
         "The problem is actually not fixed yet, it is still there"),
    ),
    "n_v2_future_when": (
        ("soon", "n_redispatch",
         "They are available now or within the next few days (today, tomorrow, a named day or date this week)"),
        ("later", "m_n_end_service_later",
         "Only later: after a week or more, next month, or they will call when they need it"),
        ("not_needed", "m_n_end_no_service",
         "They do not need the installation or service at all"),
    ),
    "n_v2_no_response_status": (
        ("completed", "n_v2_completed_check",
         "The installation or service has been done, by anyone, including an outside technician"),
        ("pending", "n_service_need",
         "The installation or service is still pending or was not done"),
    ),
}
# A third answer some yes / no questions get in practice.
_EXTRA_OPTIONS: dict[str, tuple[tuple[str, str, str], ...]] = {
    hub: (("call_only", "n_apology_visit",
           "Only a phone call happened: the technician did not actually come to their place"),)
    for hub in ("n_complaint_intro", "n_intro_unit_ok", "n_intro_not_haier", "n_v2_intro_no_response")
}
_EXTRA_OPTIONS["n_intro_bill"] = (
    ("call_mentioned", "n_haier_bill_state_contact_phone",
     "They mention a phone call only and do not say whether anybody came to their place"),
)
_CHOICE_TWINS = {
    "n_haier_followup_n_resolved_how_q_clarify": "n_resolved_how_q",
    "n_v2_future_when_clarify": "n_v2_future_when",
    "n_v2_no_response_status_clarify": "n_v2_no_response_status",
}

# Things a caller says at any step that change the flow. Offered at a hub only
# when that hub can already reach the target: (option id, target, meaning).
_TOPIC_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("stop_calling", "m_n_v2_end_repeat_calls",
     "They ask not to be called again or to stop the calls, or are fed up with so many calls. This wins over "
     "every other option. NOT this: a question about why the calls keep coming"),
    ("already_done", "n_v2_completed_check",
     "They say the installation or service for this request has already been completed. When they also say "
     "they have paid, the answer is paid"),
    ("not_now", "n_v2_future_when",
     "They want the service but cannot take the visit right now (busy, out of station, not at home)"),
    ("after_some_time", "n_future_confirm",
     "They want it only after a longer wait: next week or later, after some days, weeks or months. "
     "A day within the next few days is NOT this"),
    ("warranty", "m_warranty_claimed",
     "They say the product is under warranty or AMC, or that the service should be free. This includes asking "
     "why they should pay when it is under warranty"),
    ("charges_question", "n_charges_info_q",
     "They ask what the charges are or how much must be paid (the amount), without claiming warranty"),
    ("paid", "n_v2_paid_status_check",
     "They say they have already paid, or that the technician already took the money, whether or not the "
     "work was also done"),
    ("part_pending", "n_v2_pending_case_check",
     "They say a repair, replacement or spare part is still pending, or the product is with the service centre"),
    ("new_request", "n_v2_existing_request_check",
     "They say a new request or a technician visit is already booked for this"),
    ("installation_case", "n_v2_no_response_status",
     "They say there was no fault at all: the request was for an installation"),
    ("no_visit_happened", "n_apology_visit",
     "They say no technician actually came, the visit never happened"),
    ("has_bill_now", "n_haier_bill_state_current_yes", "They say they have the bill with them now"),
    ("no_bill_now", "n_haier_bill_state_current_no", "They say they still do not have the bill"),
    ("delivered", "n_v2_delivery_check", "They say the product has been delivered now"),
    ("not_delivered", "m_n_end_not_delivered", "They say the product has still not been delivered"),
    ("gift", "m_n_end_gift",
     "The product is a gift for someone else or has been given away, so it will not be installed here"),
    ("resale", "m_n_end_resale",
     "They are a dealer or bought the product to sell it on, so no installation is needed"),
    ("other_issue", "m_n_v2_other_issue",
     "They are talking about a different issue or a different product than this request"),
    ("wrong_person", "n_wp_available_q",
     "They say they are not the named customer, or that this is a wrong number"),
    ("no_such_request", "n_wp_haier_q",
     "They say they have no Haier product or never raised any such request"),
)
# A topic is not offered where the hub's own answer already says the same
# thing (the caller would be sent round a longer path), or where it would
# repeat the apology the caller has just heard.
_TOPIC_EXCLUDED: dict[str, tuple[str, ...]] = {
    "already_done": (
        "n_reason_unit_ok_q", "n_resolved_how_q", "n_haier_followup_n_resolved_how_q_clarify",
        "n_v2_paid_status_check", "n_v2_no_response_status", "n_v2_completed_by_q",
        "n_haier_followup_n_v2_completed_by_q_clarify",
    ),
    "delivered": ("n_reason_delivered_q",),
    "not_delivered": ("n_reason_delivered_q",),
    "has_bill_now": ("n_haier_bill_state_now_q", "n_haier_bill_state_now_clarify"),
    "no_bill_now": ("n_haier_bill_state_now_q", "n_haier_bill_state_now_clarify"),
    "not_now": ("n_v2_future_when",),
    "after_some_time": ("n_v2_future_when", "n_future_confirm"),
    "installation_case": ("n_v2_no_response_status",),
}
# Never taken on a reply that is itself a question: "kya baar-baar call kar rahe
# ho?" is answered with an apology and the question is asked again; only a
# statement or a request ("call mat karo") ends the call on this closing.
_NOT_ON_QUESTION = ("stop_calling",)

# Only a question about what happened at the visit may hear "nobody came":
# after the service question it would loop back into the same apology.
_TOPIC_ONLY_AT_PREFIX = {"no_visit_happened": ("n_reason_", "n_haier_bill_state_")}

# Questions about a STATE where "theek hai" / "sahi hai" / "ok" / "fine" may
# describe that state and then means the opposite of yes: "does it still have a
# problem?" - "theek hai" (it works); "is any work still pending?" - "sab theek
# hai"; "was it delivered damaged?" - "theek tha". There only an explicit yes
# (haan / ji / yes) is a plain yes and everything else is read by the decision.
# At every other question - a proposal ("may I forward it?", the user's rule of
# 2026-11-25) or a fact ("did you receive a call?" - "correct") - those words
# are the plain yes they have always been.
_STATE_HUBS = (
    "n_reason_unit_ok_q",
    "n_v2_completed_check", "n_v2_completed_check_clarify",
    "n_reason_ecom_q", "n_reason_ecom_q_clarify",
)

# Hubs deliberately left on their authored labels: an information step whose
# every answer leads to the same closing.
_UNCHANGED_HUBS = ("n_ecom_cancel_info",)


def _tokens(label: str) -> list[str]:
    return [t.strip().lower() for t in re.split(r"[/,|]", label or "") if t.strip()]


def _base(hub: str) -> str:
    return hub[: -len("_clarify")] if hub.endswith("_clarify") else hub


def _reachable(nodes: dict, edges_from: dict, hub: str) -> set[str]:
    """Every non-condition node the hub can already hand over to: its edge
    targets and every exit of its correction chain."""
    found: set[str] = set()
    stack = [str(edge.get("to")) for edge in edges_from.get(hub, [])]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen or current not in nodes:
            continue
        seen.add(current)
        if nodes[current].get("kind") == "condition":
            stack.extend(str(edge.get("to")) for edge in edges_from.get(current, []))
        else:
            found.add(current)
    return found


def _yes_no_targets(hub: str) -> tuple[str | None, str | None]:
    yes, no = _YES_NO_TARGETS.get(hub, (None, None))
    over_yes, over_no = _YES_NO_OVERRIDES.get(hub, (None, None))
    return over_yes or yes, over_no or no


def options_for(hub: str, nodes: dict, edges_from: dict) -> list[dict]:
    """The answer options of one hub in offer order: its own answers first,
    then the topics it can already reach."""
    reachable = _reachable(nodes, edges_from, hub)
    base = _base(hub)
    options: list[dict] = []
    choice = _CHOICE_OPTIONS.get(_CHOICE_TWINS.get(hub, hub))
    if choice is not None:
        for option_id, target, meaning in choice:
            options.append({"id": option_id, "meaning": meaning, "to": target})
    else:
        meanings = _YES_NO_MEANINGS.get(hub) or _YES_NO_MEANINGS.get(base)
        if meanings is None:
            raise ValueError(f"No answer meanings authored for hub: {hub}")
        yes_target, no_target = _yes_no_targets(hub)
        for option_id, signal, target, meaning in (
            ("yes", "affirm", yes_target, meanings[0]),
            ("no", "refusal", no_target, meanings[1]),
        ):
            if not target or target == "(stay)" or target == hub:
                raise ValueError(f"Plain {option_id} has no destination at hub: {hub}")
            options.append({"id": option_id, "meaning": meaning, "to": target, "signal": signal})
        for option_id, target, meaning in _EXTRA_OPTIONS.get(base, ()):
            if target in reachable and target not in {option["to"] for option in options}:
                options.append({"id": option_id, "meaning": meaning, "to": target})
    taken = {option["to"] for option in options}
    for option_id, target, meaning in _TOPIC_OPTIONS:
        if target not in reachable or target in taken or target == hub:
            continue
        if hub in _TOPIC_EXCLUDED.get(option_id, ()) or base in _TOPIC_EXCLUDED.get(option_id, ()):
            continue
        prefixes = _TOPIC_ONLY_AT_PREFIX.get(option_id)
        if prefixes and not hub.startswith(prefixes):
            continue
        option = {"id": option_id, "meaning": meaning, "to": target}
        if option_id in _NOT_ON_QUESTION:
            option["notOnQuestion"] = True
        options.append(option)
        taken.add(target)
    for option in options:
        if option["to"] not in nodes:
            raise ValueError(f"Answer option leads to a missing node: {hub} -> {option['to']}")
        if option["to"] not in reachable:
            raise ValueError(f"Answer option would add a new route: {hub} -> {option['to']}")
    return options


def apply_answer_options_repair(definition: dict) -> dict:
    """Return a copy of ``definition`` whose intent hubs declare answerOptions."""
    result = deepcopy(definition)
    nodes = {str(node["id"]): node for node in result["nodes"]}
    edges_from: dict[str, list[dict]] = {}
    for edge in result["edges"]:
        edges_from.setdefault(str(edge.get("from")), []).append(edge)
    for node in result["nodes"]:
        if node.get("kind") == "start":
            # After the closing line: answer at most this many follow-ups, each
            # followed by the closing courtesy and the hang-up.
            config = node.setdefault("config", {})
            config["closingFollowups"] = CLOSING_FOLLOWUPS
            behavior = dict(config.get("behavior") or {})
            declared = behavior.get("max_node_steps")
            behavior["max_node_steps"] = max(
                declared if isinstance(declared, int) and not isinstance(declared, bool) else 0,
                MAX_NODE_STEPS,
            )
            config["behavior"] = behavior
            if DO_NOT_CALL_NODE in nodes:
                config["doNotCallNode"] = DO_NOT_CALL_NODE
    for hub, node in nodes.items():
        if node.get("kind") != "intent" or hub in _UNCHANGED_HUBS:
            continue
        config = node.setdefault("config", {})
        config["answerOptions"] = options_for(hub, nodes, edges_from)
        for option in config["answerOptions"]:
            if len(option["meaning"]) > MAX_MEANING_CHARS:
                raise ValueError(
                    f"Meaning of option {option['id']} at {hub} has {len(option['meaning'])} characters; "
                    f"the engine passes {MAX_MEANING_CHARS}")
        if hub in _STATE_HUBS:
            config.pop("answerAgreementWords", None)
        else:
            config["answerAgreementWords"] = True
        config[MARKER] = VERSION
    return result


def hubs_with_options(definition: dict) -> dict[str, list[dict]]:
    return {
        str(node["id"]): list((node.get("config") or {}).get("answerOptions") or [])
        for node in definition["nodes"]
        if node.get("kind") == "intent" and (node.get("config") or {}).get("answerOptions")
    }
