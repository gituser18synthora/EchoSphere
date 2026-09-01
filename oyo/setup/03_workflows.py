"""Stage: workflows — the three OYO workflow graphs, saved as approved.

Engine contract (shared/orchestration/workflow_engine.py):
- ask: success -> FIRST outgoing edge; edge labeled 'fallback' used after >2 retries.
- api: edges labeled success|ok|done vs failure|failed|error|fallback.
- condition: edges labeled true|false.
- intent: edges picked by semantic signal of their label tokens, then longest
  literal token; tokens ending in '?' carry the 'question' signal so
  question-phrased utterances can advance.
- message/end/handover speak config.text verbatim (no slot interpolation).
- responseMode (message/ask/intent/api/end config): "fixed" (default,
  verbatim), "exact" (never paraphrased/adapted), or "llm_grounded" — the
  flow decided WHAT happened, the LLM words it from responseDirective +
  verified facts; config.text stays the spoken fallback whenever generation
  fails or validation rejects the output. Verification failures, transfers,
  errors and unsafe transactional claims stay fixed on purpose; grounded
  success wording sits ONLY on deterministically-guaranteed branches (an
  api node's success edge, a condition's true edge).
"""

import json

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
BOT1 = "bot_e8cf0b05bb79"
STATE_FILE = __file__.rsplit("/", 1)[0] + "/oyo_config_state.json"
state = json.load(open(STATE_FILE))
BOT2, BOT3 = state["BOT2"], state["BOT3"]


def N(nid, kind, label, config=None):
    return {"id": nid, "kind": kind, "label": label,
            **({"config": config} if config else {})}


def E(src, dst, label=None):
    edge = {"id": f"e_{src}__{dst}", "from": src, "to": dst}
    if label:
        edge["label"] = label
        edge["id"] = f"e_{src}__{dst}__{abs(hash(label)) % 10000}"
    return edge


def layout(nodes):
    for i, n in enumerate(nodes):
        n.setdefault("x", 40 + (i % 6) * 260)
        n.setdefault("y", 40 + (i // 6) * 130)
    return nodes


BID_ASK = {
    "entityType": "text",
    # OYO sample booking IDs are exactly six digits.  The digit boundaries
    # prevent a partial match when telephony STT inserts or drops a digit.
    "pattern": r"(?<![0-9])(?:BK[-\s]?)?([0-9]{6})(?![0-9])",
}

# ═════════════════════════════ BOT 1 — customer bot ══════════════════════════

B1_NODES = layout([
    N("n_start", "start", "Call starts"),
    N("n_ask_booking", "ask", "Ask booking ID", {
        "question": "I can help you with that. Please say your six-digit booking ID slowly, one digit at a time.",
        "variable": "booking_id", **BID_ASK}),
    N("n_ask_name", "ask", "Ask guest name", {
        "question": "Thank you. For verification, may I know the guest name on this booking?",
        "variable": "guest_name", "entityType": "text"}),
    N("n_api_verify", "api", "Customer Verification API", {
        "connection": "OYO Customer Verification"}),
    N("n_msg_verify_failed", "message", "Verification failed", {
        "text": "I'm sorry, I could not verify these details against the booking, so I can't share any booking information on this call. Let me connect you to a support executive for further help."}),
    N("n_api_ivr_sup", "api", "IVR Transfer API", {
        "connection": "OYO IVR Transfer"}),
    N("n_handover_support", "handover", "Transfer to support", {
        "queue": "customer_support",
        "text": "Please stay on the line while I transfer you to our support team."}),
    N("n_api_booking", "api", "Booking Details API", {
        "connection": "OYO Booking Details"}),
    N("n_msg_lookup_failed", "message", "Lookup failed", {
        "text": "I'm unable to retrieve this booking in our system right now."}),
    N("n_cond_confirmed", "condition", "Booking confirmed?", {
        "variable": "booking_status", "operator": "equals", "value": "confirmed"}),

    # cancelled branch
    N("n_msg_cancelled", "message", "Booking cancelled", {
        "text": "I've checked, and I'm sorry to share that this booking is showing as cancelled in our system, along with the cancellation date on record."}),
    N("n_intent_cancel", "intent", "Did you cancel?", {
        "prompt": "Did you cancel this booking yourself?"}),
    N("n_msg_cancel_ack", "message", "Cancel acknowledged", {
        "text": "Understood. Since this booking stands cancelled, there is nothing pending on it. For a new booking, our support team or the OYO app can help you anytime.",
        "responseMode": "llm_grounded",
        "responseDirective": (
            "Acknowledge that the caller confirmed they cancelled this "
            "booking themselves, so nothing is pending on it; mention the "
            "OYO app or support team can help with any new booking, and "
            "close politely.")}),
    N("n_msg_cancel_dispute", "message", "Cancel dispute", {
        "text": "I understand — you did not cancel this booking yourself. This needs immediate attention, so let me transfer you to a support executive who can investigate the cancellation right away."}),
    N("n_api_ivr_esc", "api", "IVR Transfer API (escalation)", {
        "connection": "OYO IVR Transfer"}),
    N("n_handover_esc", "handover", "Transfer to escalations", {
        "queue": "escalations",
        "text": "Transferring you now — please stay on the line."}),

    # requirement hub — grounded delivery: the flow decided the booking is
    # confirmed (condition true edge); the LLM only words the announcement
    # and MUST keep asking the three-option question (validated; the
    # authored prompt is the fallback).
    N("n_hub", "intent", "What would you like?", {
        "prompt": "Great news — your booking is confirmed in our system. Would you like me to also confirm it directly with the property, hear your booking details, or get the booking voucher emailed to you?",
        "responseMode": "llm_grounded",
        "responseDirective": (
            "Tell the caller their booking is confirmed in our system, then "
            "ask what they would like next, offering exactly these three "
            "options: confirming the booking directly with the property, "
            "hearing their booking details, or emailing the booking "
            "voucher. Never choose an option for them.")}),
    N("n_msg_sysconfirm", "message", "System confirmation", {
        "text": "Perfect. Your booking is confirmed in our system, and you can proceed with your check-in without any issues.",
        "responseMode": "llm_grounded",
        "responseDirective": (
            "Confirm that the booking is confirmed in our system and the "
            "caller can proceed with their check-in without any issues, "
            "then close warmly.")}),

    # details branch — grounded: answer the caller's actual detail request
    # from the workflow-verified facts (single fact first; broad request →
    # concise summary). The authored text is only the LLM-down fallback.
    N("n_msg_details_exit", "message", "Details answered from facts", {
        "responseMode": "llm_grounded",
        "responseDirective": (
            "Answer the caller's current booking question directly from the "
            "workflow-verified booking facts. For a single fact (hotel "
            "name, check-in or check-out date, occupancy, payment status, "
            "pending amount) state that value in the first sentence. For a "
            "broad details request give one concise spoken summary of the "
            "hotel, stay dates, occupancy and payment facts. Never recite a "
            "menu of what they may ask."),
        "text": "I have your verified booking open — please tell me which detail you need."}),
    N("n_end_details", "end", "End (details Q&A)"),

    # voucher branch
    N("n_cond_email", "condition", "Email on file?", {
        "variable": "guest_email", "operator": "exists"}),
    N("n_intent_email_confirm", "intent", "Send to email on file?", {
        "prompt": "I have the email address from your booking on file. Shall I send the booking voucher there?"}),
    N("n_ask_email", "ask", "Ask email", {
        "question": "Sure — could you please tell me the email address where I should send the voucher?",
        "variable": "email_address", "entityType": "email"}),
    N("n_api_voucher", "api", "Booking Voucher API", {
        "connection": "OYO Booking Voucher"}),
    # Grounded success wording is safe here ONLY because this node sits on
    # the Booking Voucher API's success edge — a failed send can never
    # reach it (it routes to the fixed n_msg_voucher_fail instead).
    N("n_msg_voucher_ok", "message", "Voucher sent", {
        "text": "Done! I've emailed your booking voucher — it should reach your inbox within a few minutes.",
        "responseMode": "llm_grounded",
        "responseDirective": (
            "Tell the caller their booking voucher was just emailed "
            "successfully (the send is system-verified) and should reach "
            "their inbox within a few minutes.")}),
    N("n_msg_voucher_fail", "message", "Voucher failed", {
        "text": "I'm sorry, I couldn't send the voucher right now. Our support team can email it to you manually."}),
    N("n_hub_more", "intent", "Anything else?", {
        "prompt": "Is there anything else I can help you with today?"}),

    # property verification branch
    N("n_msg_hold", "message", "Hold for property check", {
        "text": "Certainly. Let me quickly connect with the property and verify your booking status. Please stay on the line while I check."}),
    N("n_api_pm", "api", "PM Verification Call", {
        "connection": "OYO PM Verification Call"}),
    N("n_cond_pm_answered", "condition", "PM unreachable?", {
        "variable": "pm_call_status", "operator": "equals", "value": "no_answer"}),
    N("n_cond_pm_honored", "condition", "Booking honored?", {
        "variable": "pm_booking_honored", "operator": "equals", "value": "true"}),
    N("n_cond_res_alt", "condition", "Alternate room?", {
        "variable": "pm_resolution", "operator": "equals", "value": "alternate_room"}),
    N("n_cond_res_comp", "condition", "Compensation added?", {
        "variable": "pm_resolution", "operator": "equals", "value": "compensation_added"}),
    N("n_msg_pm_confirmed", "message", "PM confirmed", {
        "text": "Thank you for waiting. I have successfully confirmed your booking with the property. Your reservation is secured, and you can check in without any hassle."}),
    N("n_msg_alt_room", "message", "Alternate room arranged", {
        "text": "Good news! The property has arranged an alternate room for your stay, and your booking has been confirmed. You may proceed with your check-in."}),
    N("n_msg_comp_ok", "message", "Confirmed after compensation", {
        "text": "Good news! Your booking has been successfully confirmed with the property, and you can proceed with your check-in as planned."}),

    # denial reasons
    N("n_cond_deny_ob", "condition", "Denied: overbooked?", {
        "variable": "pm_deny_reason", "operator": "equals", "value": "overbooked"}),
    N("n_cond_deny_mnt", "condition", "Denied: maintenance?", {
        "variable": "pm_deny_reason", "operator": "equals", "value": "maintenance"}),
    N("n_msg_overbooked", "message", "Overbooked", {
        "text": "Thank you for waiting. Unfortunately, the property is currently overbooked due to high demand and is unable to accommodate your reservation."}),
    N("n_msg_maintenance", "message", "Maintenance", {
        "text": "Thank you for waiting. The property is currently undergoing maintenance and is unable to accommodate your booking."}),
    N("n_msg_price_denied", "message", "Price denial", {
        "text": "I sincerely apologize — despite our best efforts, the property is unable to accommodate this reservation."}),

    # stock team branch
    N("n_msg_stock", "message", "Trying stock team", {
        "text": "I was unable to reach the property manager at this time. Let me quickly validate your booking with our internal team instead. Please stay on the line."}),
    N("n_api_stock", "api", "Stock Team Call", {
        "connection": "OYO Stock Team Call"}),
    N("n_cond_stock", "condition", "Stock confirmed?", {
        "variable": "stock_status", "operator": "equals", "value": "confirmed"}),
    N("n_msg_stock_ok", "message", "Stock confirmed", {
        "text": "Thank you for waiting. Our internal team has validated your reservation — your booking will be honored at check-in, and you may proceed with your stay as planned."}),
    N("n_msg_stock_fail", "message", "Stock could not confirm", {
        "text": "I'm sorry — I could not get a confirmation from the property or our internal team at this moment."}),

    # shift flow
    N("n_intent_shift", "intent", "Offer shift", {
        "prompt": "Would you like me to help arrange an alternate OYO property nearby with similar amenities?"}),
    N("n_api_alts", "api", "Alternate Properties API", {
        "connection": "OYO Alternate Properties"}),
    N("n_msg_no_alts", "message", "No alternates", {
        "text": "I'm sorry — I couldn't find an alternate OYO property nearby right now. Let me connect you to our support team to help you further."}),
    N("n_intent_shift_confirm", "intent", "Confirm shift", {
        "prompt": "I have found alternative OYO properties nearby with similar amenities. Shall I proceed with shifting your booking?"}),
    N("n_api_shift", "api", "Shift Booking API", {
        "connection": "OYO Shift Booking"}),
    N("n_msg_shift_ok", "message", "Shift done", {
        "text": "Done! I've initiated the shift of your booking to a nearby OYO property with similar amenities. You'll receive the new property details on SMS and email shortly."}),
    N("n_msg_shift_err", "message", "Shift failed", {
        "text": "I'm sorry, the shift could not be completed right now. Let me connect you to our support team to arrange it manually."}),
    N("n_msg_shift_decline", "message", "Shift declined", {
        "text": "I understand. You may visit the property as planned, and if you face any issue during check-in, please contact OYO support right away. You can also choose to cancel this booking through OYO support."}),

    # closure
    N("n_api_dispo", "api", "CRM Disposition API", {
        "connection": "OYO Call Disposition"}),
    N("n_end_main", "end", "End call", {
        "text": "Thank you for calling OYO. Have a great day!"}),
])

YES = "yes/sure/okay/ok/please do/go ahead/haan/ji haan/हाँ/जी हाँ/ठीक है/proceed/definitely"
NO = "no/not now/nahi/नहीं/don't/do not/later/leave it"

B1_EDGES = [
    E("n_start", "n_ask_booking"),
    E("n_ask_booking", "n_ask_name"),                       # success = first edge
    E("n_ask_booking", "n_msg_verify_failed", "fallback"),
    E("n_ask_name", "n_api_verify"),
    E("n_ask_name", "n_msg_verify_failed", "fallback"),
    E("n_api_verify", "n_api_booking", "success"),
    E("n_api_verify", "n_msg_verify_failed", "failure"),
    E("n_msg_verify_failed", "n_api_ivr_sup"),
    E("n_api_ivr_sup", "n_handover_support"),
    E("n_api_booking", "n_cond_confirmed", "success"),
    E("n_api_booking", "n_msg_lookup_failed", "failure"),
    E("n_msg_lookup_failed", "n_api_ivr_sup"),
    E("n_cond_confirmed", "n_hub", "true"),
    E("n_cond_confirmed", "n_msg_cancelled", "false"),

    # cancelled branch
    E("n_msg_cancelled", "n_intent_cancel"),
    E("n_intent_cancel", "n_msg_cancel_ack",
      "yes/i did/i cancelled/cancelled it myself/haan/maine cancel/हाँ"),
    E("n_intent_cancel", "n_msg_cancel_dispute",
      "no/didn't cancel/did not cancel/not me/never cancelled/nahi/नहीं/didn't?/who cancelled?"),
    E("n_intent_cancel", "n_msg_cancel_dispute", "else"),
    E("n_msg_cancel_ack", "n_api_dispo"),
    E("n_msg_cancel_dispute", "n_api_ivr_esc"),
    E("n_api_ivr_esc", "n_handover_esc"),

    # hub — the details edge comes FIRST: a question the hub has no literal
    # token for ("what is my checking date?", "cancellation policy?") must
    # exit to LLM Q&A over the verified facts, never start the property call
    # (equal-score question-signal edges resolve in authored order).
    E("n_hub", "n_msg_details_exit",
      "details/booking details/check-in date/check in date/checking date/"
      "checkout date/check-out/payment/amount pending/hotel name/dates/"
      "occupancy/चेक-इन डेट/चेक इन डेट/डेट क्या/होटल का नाम/पेमेंट/कितना/"
      "details?/date?/dates?"),
    E("n_hub", "n_msg_hold",
      "property/with the property/hotel/confirm with property/property confirmation/check-in confirmation/checkin confirmation/honor/honour/प्रॉपर्टी/होटल से/property?/hotel?"),
    E("n_hub", "n_cond_email",
      "voucher/booking voucher/send voucher/email the voucher/confirmation email/voucher?/email?"),
    E("n_hub", "n_msg_sysconfirm",
      "no/nothing/nothing else/that's all/thats all/thank you/thanks/that's it/im good/nahi/नहीं/bas/बस"),
    E("n_msg_sysconfirm", "n_api_dispo"),

    # details
    E("n_msg_details_exit", "n_end_details"),

    # voucher
    E("n_cond_email", "n_intent_email_confirm", "true"),
    E("n_cond_email", "n_ask_email", "false"),
    E("n_intent_email_confirm", "n_api_voucher", YES),
    E("n_intent_email_confirm", "n_ask_email",
      "different/another/change/new email/other email/not that/wrong email"),
    E("n_intent_email_confirm", "n_ask_email", "else"),
    E("n_ask_email", "n_api_voucher"),
    E("n_ask_email", "n_msg_voucher_fail", "fallback"),
    E("n_api_voucher", "n_msg_voucher_ok", "success"),
    E("n_api_voucher", "n_msg_voucher_fail", "failure"),
    E("n_msg_voucher_ok", "n_hub_more"),
    E("n_msg_voucher_fail", "n_hub_more"),

    # anything-else hub — details first, same rationale as n_hub above.
    E("n_hub_more", "n_msg_details_exit",
      "details/booking details/dates/payment/hotel name/check-in date/"
      "check in date/checking date/checkout date/चेक-इन डेट/डेट क्या/"
      "होटल का नाम/पेमेंट/details?/date?"),
    E("n_hub_more", "n_msg_hold",
      "property/hotel/confirm with property/check-in confirmation/प्रॉपर्टी/होटल से/property?/hotel?"),
    E("n_hub_more", "n_cond_email", "voucher/email/voucher?"),
    E("n_hub_more", "n_api_dispo",
      "no/nothing/nothing else/that's all/thanks/thank you/bye/nahi/नहीं/bas/बस"),

    # property verification
    E("n_msg_hold", "n_api_pm"),
    E("n_api_pm", "n_cond_pm_answered", "success"),
    E("n_api_pm", "n_msg_stock", "failure"),
    E("n_cond_pm_answered", "n_msg_stock", "true"),
    E("n_cond_pm_answered", "n_cond_pm_honored", "false"),
    E("n_cond_pm_honored", "n_cond_res_alt", "true"),
    E("n_cond_pm_honored", "n_cond_deny_ob", "false"),
    E("n_cond_res_alt", "n_msg_alt_room", "true"),
    E("n_cond_res_alt", "n_cond_res_comp", "false"),
    E("n_cond_res_comp", "n_msg_comp_ok", "true"),
    E("n_cond_res_comp", "n_msg_pm_confirmed", "false"),
    E("n_msg_pm_confirmed", "n_api_dispo"),
    E("n_msg_alt_room", "n_api_dispo"),
    E("n_msg_comp_ok", "n_api_dispo"),
    E("n_cond_deny_ob", "n_msg_overbooked", "true"),
    E("n_cond_deny_ob", "n_cond_deny_mnt", "false"),
    E("n_cond_deny_mnt", "n_msg_maintenance", "true"),
    E("n_cond_deny_mnt", "n_msg_price_denied", "false"),
    E("n_msg_overbooked", "n_intent_shift"),
    E("n_msg_maintenance", "n_intent_shift"),
    E("n_msg_price_denied", "n_intent_shift"),

    # stock team
    E("n_msg_stock", "n_api_stock"),
    E("n_api_stock", "n_cond_stock", "success"),
    E("n_api_stock", "n_msg_stock_fail", "failure"),
    E("n_cond_stock", "n_msg_stock_ok", "true"),
    E("n_cond_stock", "n_msg_stock_fail", "false"),
    E("n_msg_stock_ok", "n_api_dispo"),
    E("n_msg_stock_fail", "n_intent_shift"),

    # shift
    E("n_intent_shift", "n_api_alts", YES + "/arrange/alternate/please?"),
    E("n_intent_shift", "n_msg_shift_decline",
      NO + "/don't shift/dont shift/i'll manage/i will manage/cancel instead"),
    E("n_api_alts", "n_intent_shift_confirm", "success"),
    E("n_api_alts", "n_msg_no_alts", "failure"),
    E("n_msg_no_alts", "n_api_ivr_sup"),
    E("n_intent_shift_confirm", "n_api_shift", YES + "/confirm"),
    E("n_intent_shift_confirm", "n_msg_shift_decline",
      NO + "/don't shift/dont shift/wait/hold on/not yet"),
    E("n_api_shift", "n_msg_shift_ok", "success"),
    E("n_api_shift", "n_msg_shift_err", "failure"),
    E("n_msg_shift_ok", "n_api_dispo"),
    E("n_msg_shift_err", "n_api_ivr_sup"),
    E("n_msg_shift_decline", "n_api_dispo"),

    # closure
    E("n_api_dispo", "n_end_main"),
]

# ═══════════════════════ BOT 2 — property verification ═══════════════════════

PM_YES = "yes/confirmed/we will honor/honour it/honor it/no problem/sure/of course/accept/haan/हाँ/जी हाँ/ठीक है/okay"
PM_NO = "no/we cannot/cannot accommodate/cannot honor/cannot/can't/cant/not possible/won't/wont/unable/deny/refuse/nahi/नहीं/decline"

B2_NODES = layout([
    N("n_start", "start", "Call starts"),
    N("n_ask_bid", "ask", "Confirm booking ID", {
        "question": "Could you please confirm the booking ID for the reservation, as shown in your OYO manager app?",
        "variable": "booking_id", **BID_ASK}),
    N("n_api_booking", "api", "Booking Details API", {
        "connection": "OYO Booking Details"}),
    N("n_intent_pm", "intent", "Will you honor?", {
        "prompt": "Thank you. Could you please confirm whether this booking will be honored for check-in?"}),
    N("n_ask_reason", "ask", "Ask deny reason", {
        "question": "I understand. Could you please help me understand the reason for declining the booking?",
        "variable": "deny_reason", "entityType": "text"}),

    # deny-reason classification chain
    N("n_cond_r_overbook", "condition", "reason: overbook?", {
        "variable": "deny_reason", "operator": "contains", "value": "overbook"}),
    N("n_cond_r_full", "condition", "reason: full?", {
        "variable": "deny_reason", "operator": "contains", "value": "full"}),
    N("n_cond_r_noroom", "condition", "reason: no room?", {
        "variable": "deny_reason", "operator": "contains", "value": "no room"}),
    N("n_cond_r_mnt", "condition", "reason: maintenance?", {
        "variable": "deny_reason", "operator": "contains", "value": "maintenance"}),
    N("n_cond_r_renov", "condition", "reason: renovation?", {
        "variable": "deny_reason", "operator": "contains", "value": "renovat"}),
    N("n_cond_r_repair", "condition", "reason: repair?", {
        "variable": "deny_reason", "operator": "contains", "value": "repair"}),
    N("n_cond_r_price", "condition", "reason: price?", {
        "variable": "deny_reason", "operator": "contains", "value": "price"}),
    N("n_cond_r_rate", "condition", "reason: rate?", {
        "variable": "deny_reason", "operator": "contains", "value": "rate"}),
    N("n_cond_r_low", "condition", "reason: low?", {
        "variable": "deny_reason", "operator": "contains", "value": "low"}),
    N("n_cond_r_tariff", "condition", "reason: tariff?", {
        "variable": "deny_reason", "operator": "contains", "value": "tariff"}),

    # overbooked branch
    N("n_api_occ", "api", "Property Occupancy API", {
        "connection": "OYO Property Occupancy"}),
    N("n_cond_avail", "condition", "Inventory available?", {
        "variable": "has_availability", "operator": "equals", "value": "true"}),
    N("n_msg_penalty", "message", "Penalty advisory", {
        "text": "I'd like to flag one thing: our records show available inventory at your property for these dates. Please note that denying a valid booking despite availability can lead to penalties under your agreement with OYO."}),
    N("n_intent_penalty", "intent", "Honor after advisory?", {
        "prompt": "Considering this, would you be able to honor the booking?"}),

    # maintenance branch
    N("n_intent_altroom", "intent", "Alternate room?", {
        "prompt": "I'm sorry to hear about the maintenance. Do you have any alternate rooms available that could accommodate this booking?"}),

    # price branch
    N("n_api_pricing", "api", "Property Pricing API", {
        "connection": "OYO Property Pricing"}),
    N("n_cond_arr", "condition", "Rate meets ARR?", {
        "variable": "rate_vs_arr", "operator": "equals", "value": "meets"}),
    N("n_msg_arr", "message", "ARR pitch", {
        "text": "I checked our records — this booking actually meets or exceeds your average realized rate over the last seven days. We request you to honor the booking to avoid potential penalties and to ensure a positive guest experience."}),
    N("n_intent_arr", "intent", "Honor at ARR?", {
        "prompt": "Can we count on you to honor this booking?"}),
    N("n_msg_comp", "message", "Compensation offer", {
        "text": "I understand the concern on the rate. To bridge the gap, OYO can add a complimentary compensation amount to this reservation from our side."}),
    N("n_intent_comp", "intent", "Honor with compensation?", {
        "prompt": "With this additional compensation added, would you be willing to honor the booking?"}),
    N("n_api_comp", "api", "Add Complimentary API", {
        "connection": "OYO Add Complimentary Amount",
        "text": "Excellent — I've added the complimentary amount to this booking on OYO's side."}),

    # reporting + ends
    N("n_api_report_h", "api", "Report: honored", {
        "connection": "OYO PM Report Honored"}),
    N("n_api_report_nh", "api", "Report: not honored", {
        "connection": "OYO PM Report Not Honored"}),
    N("n_end_ok", "end", "End (honored)", {
        "text": "Thank you for confirming — the guest will proceed with check-in as planned. We appreciate your support. Have a good day!"}),
    N("n_end_deny", "end", "End (not honored)", {
        "text": "I understand. We will arrange an alternate stay for the guest, and our team may follow up regarding this booking. Thank you for your time."}),
    N("n_end_nobid", "end", "End (no booking ref)", {
        "text": "No problem — our team will call back with the booking details shortly. Thank you for your time."}),
])

B2_EDGES = [
    E("n_start", "n_ask_bid"),
    E("n_ask_bid", "n_api_booking"),
    E("n_ask_bid", "n_end_nobid", "fallback"),
    E("n_api_booking", "n_intent_pm", "success"),
    E("n_api_booking", "n_end_nobid", "failure"),
    E("n_intent_pm", "n_api_report_h", PM_YES),
    # Direct edges when the PM states the reason inside the denial sentence —
    # tokens deliberately LONGER than the generic deny tokens so they win the
    # longest-literal-token tie-break.
    E("n_intent_pm", "n_api_occ",
      "we are overbooked/fully overbooked/overbooked/house full/sold out/no rooms left"),
    E("n_intent_pm", "n_intent_altroom",
      "under maintenance/maintenance work/maintenance/renovation/repair work"),
    E("n_intent_pm", "n_api_pricing",
      "price is too low/price is very low/price too low/rate is too low/rate too low/price is low/low tariff"),
    E("n_intent_pm", "n_ask_reason", PM_NO),
    E("n_ask_reason", "n_cond_r_overbook"),
    E("n_cond_r_overbook", "n_api_occ", "true"),
    E("n_cond_r_overbook", "n_cond_r_full", "false"),
    E("n_cond_r_full", "n_api_occ", "true"),
    E("n_cond_r_full", "n_cond_r_noroom", "false"),
    E("n_cond_r_noroom", "n_api_occ", "true"),
    E("n_cond_r_noroom", "n_cond_r_mnt", "false"),
    E("n_cond_r_mnt", "n_intent_altroom", "true"),
    E("n_cond_r_mnt", "n_cond_r_renov", "false"),
    E("n_cond_r_renov", "n_intent_altroom", "true"),
    E("n_cond_r_renov", "n_cond_r_repair", "false"),
    E("n_cond_r_repair", "n_intent_altroom", "true"),
    E("n_cond_r_repair", "n_cond_r_price", "false"),
    E("n_cond_r_price", "n_api_pricing", "true"),
    E("n_cond_r_price", "n_cond_r_rate", "false"),
    E("n_cond_r_rate", "n_api_pricing", "true"),
    E("n_cond_r_rate", "n_cond_r_low", "false"),
    E("n_cond_r_low", "n_api_pricing", "true"),
    E("n_cond_r_low", "n_cond_r_tariff", "false"),
    E("n_cond_r_tariff", "n_api_pricing", "true"),
    E("n_cond_r_tariff", "n_api_report_nh", "false"),

    # overbooked
    E("n_api_occ", "n_cond_avail", "success"),
    E("n_api_occ", "n_api_report_nh", "failure"),
    E("n_cond_avail", "n_msg_penalty", "true"),
    E("n_cond_avail", "n_api_report_nh", "false"),
    E("n_msg_penalty", "n_intent_penalty"),
    E("n_intent_penalty", "n_api_report_h", PM_YES),
    E("n_intent_penalty", "n_api_report_nh", PM_NO),

    # maintenance
    E("n_intent_altroom", "n_api_report_h",
      "yes/we have/arrange/manage/haan/हाँ/okay/sure"),
    E("n_intent_altroom", "n_api_report_nh",
      "no/we cannot/cannot/none/nothing available/not available/nahi/नहीं/all blocked"),

    # price
    E("n_api_pricing", "n_cond_arr", "success"),
    E("n_api_pricing", "n_api_report_nh", "failure"),
    E("n_cond_arr", "n_msg_arr", "true"),
    E("n_cond_arr", "n_msg_comp", "false"),
    E("n_msg_arr", "n_intent_arr"),
    E("n_intent_arr", "n_api_report_h", PM_YES),
    E("n_intent_arr", "n_api_report_nh", PM_NO),
    E("n_msg_comp", "n_intent_comp"),
    E("n_intent_comp", "n_api_comp", PM_YES),
    E("n_intent_comp", "n_api_report_nh", PM_NO),
    E("n_api_comp", "n_api_report_h", "success"),
    E("n_api_comp", "n_api_report_nh", "failure"),

    # reporting
    E("n_api_report_h", "n_end_ok"),
    E("n_api_report_nh", "n_end_deny"),
]

# ═══════════════════════ BOT 3 — stock team validation ═══════════════════════

B3_NODES = layout([
    N("n_start", "start", "Call starts"),
    N("n_ask_bid", "ask", "Confirm booking ID", {
        "question": "Could you please confirm the booking ID that needs validation?",
        "variable": "booking_id", **BID_ASK}),
    N("n_api_booking", "api", "Booking Details API", {
        "connection": "OYO Booking Details"}),
    N("n_intent_stock", "intent", "Can it be honored?", {
        "prompt": "The property has not confirmed this booking so far. Could you check whether this booking can be honored at check-in?"}),
    N("n_api_report_h", "api", "Report: honoured", {
        "connection": "OYO Stock Report Honored"}),
    N("n_api_report_nh", "api", "Report: cannot confirm", {
        "connection": "OYO Stock Report Not Honored"}),
    N("n_end_ok", "end", "End (validated)", {
        "text": "Perfect, thank you for validating. We'll inform the guest that the booking stands confirmed. Have a good day!"}),
    N("n_end_no", "end", "End (not confirmed)", {
        "text": "Understood. We'll proceed with offering the guest an alternate property. Thank you for checking."}),
    N("n_end_nores", "end", "End (no booking ref)", {
        "text": "No problem — I'll route this validation through the dashboard instead. Thank you!"}),
])

B3_EDGES = [
    E("n_start", "n_ask_bid"),
    E("n_ask_bid", "n_api_booking"),
    E("n_ask_bid", "n_end_nores", "fallback"),
    E("n_api_booking", "n_intent_stock", "success"),
    E("n_api_booking", "n_end_nores", "failure"),
    E("n_intent_stock", "n_api_report_h",
      "yes/confirmed/will be honored/honoured/valid/it can be/haan/हाँ/okay/sure/stands"),
    E("n_intent_stock", "n_api_report_nh",
      "no/we cannot/cannot/can't/not possible/unavailable/won't/nahi/नहीं/out of stock/no inventory"),
    E("n_api_report_h", "n_end_ok"),
    E("n_api_report_nh", "n_end_no"),
]

WORKFLOWS = [
    (BOT1, "OYO booking support journey", B1_NODES, B1_EDGES),
    (BOT2, "OYO property verification journey", B2_NODES, B2_EDGES),
    (BOT3, "OYO stock validation journey", B3_NODES, B3_EDGES),
]


def check(r, what):
    if r.status_code >= 300:
        raise SystemExit(f"FAIL {what}: {r.status_code} {r.text[:800]}")
    print(f"ok   {what}")
    return r.json().get("data")


c = httpx.Client(base_url=BASE, timeout=30)
token = check(c.post("/auth/login", json={"email": "oyo.config@oyo.com",
                                          "password": "Demo@2026!"}), "login")["token"]
c.headers["Authorization"] = f"Bearer {token}"

for bot_id, name, nodes, edges in WORKFLOWS:
    data = check(c.put(f"/bots/{bot_id}/workflow", json={
        "name": name, "nodes": nodes, "edges": edges, "status": "approved",
    }), f"workflow '{name}'")
    issues = data.get("issues") or []
    if issues:
        print(f"     issues: {json.dumps(issues)[:600]}")

print("workflows done")
