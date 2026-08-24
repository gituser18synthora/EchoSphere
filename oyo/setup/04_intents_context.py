"""Stage: intents (all bots) + runtime-context schema (bot 1)."""

import json

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
BOT1 = "bot_e8cf0b05bb79"
STATE_FILE = __file__.rsplit("/", 1)[0] + "/oyo_config_state.json"
state = json.load(open(STATE_FILE))
BOT2, BOT3 = state["BOT2"], state["BOT3"]

WF1 = "workflow:oyo_booking_support_journey"
WF2 = "workflow:oyo_property_verification_journey"
WF3 = "workflow:oyo_stock_validation_journey"

# Confidence thresholds are RISK TIERS against the router's match-strength
# scoring (exact configured phrase 0.95 > contained phrase 0.55–0.9 by how
# much of the utterance it covers > in-order words with gaps > lone word
# ≤0.6). Handoff/destructive intents demand phrase-level evidence (0.65–0.7)
# so a stray "refund"/"cancellation" in a longer sentence can never transfer
# the call; workflow entries sit at 0.55–0.6; informational routing at 0.4.
# Ambiguous phrasings fall through to the LLM classifier on live calls, which
# is gated by the SAME per-intent thresholds against its own confidence.
INTENTS = {
    BOT1: [
        {"name": "booking_confirmation", "category": "booking",
         "description": "Caller wants to know whether their booking is confirmed.",
         "samples": ["is my booking confirmed", "booking confirmation",
                     "confirm my booking", "confirm my upcoming booking",
                     "confirm upcoming booking", "upcoming booking confirmation",
                     "check my upcoming booking", "booking status",
                     "is my reservation confirmed", "check my booking",
                     "meri booking confirm hai kya", "booking confirm hai kya",
                     "मेरी बुकिंग कन्फर्म है क्या", "बुकिंग का स्टेटस बताइए"],
         "confidenceThreshold": 0.55, "route": WF1,
         "entities": [], "optionalEntities": ["booking_id"]},
        {"name": "checkin_confirmation", "category": "booking",
         "description": "Caller wants OYO to confirm the stay directly with the property (spec Flow 6).",
         "samples": ["confirm with the hotel", "confirm with the property",
                     "will the hotel honor my booking", "check-in confirmation",
                     "confirm my check-in", "verify with the property",
                     "please check with the property", "hotel se confirm karo",
                     "होटल से कन्फर्म कर दीजिए"],
         "confidenceThreshold": 0.6, "route": WF1,
         "optionalEntities": ["booking_id"]},
        {"name": "booking_voucher", "category": "booking",
         "description": "Caller wants the booking voucher emailed (spec Flow 4).",
         "samples": ["booking voucher", "send my voucher", "email me the voucher",
                     "need the voucher", "voucher for my booking",
                     "send me the voucher", "share the voucher",
                     "email the voucher", "voucher bhej do", "वाउचर भेज दीजिए"],
         "confidenceThreshold": 0.6, "route": WF1,
         "optionalEntities": ["booking_id", "email_address"]},
        {"name": "booking_details", "category": "booking",
         "description": "Caller asks for their booking details (spec Flow 5); enters the verified flow first.",
         "samples": ["booking details", "my booking details",
                     "share the booking details", "details of my booking",
                     "booking ki details batao", "बुकिंग की डिटेल्स बताइए"],
         "confidenceThreshold": 0.55, "route": WF1,
         "optionalEntities": ["booking_id"]},
        {"name": "call_opening_response", "category": "flow",
         "description": ("Caller responds to the greeting and wants help with "
                         "their booking — start the guided booking flow. Also "
                         "matches a bare affirmative ('haan', 'yes') given "
                         "directly in reply to the opening greeting."),
         # Bare affirmations ("हाँ", "haan ji") are deliberately NOT samples:
         # a "yes" answered to ANY later question would restart the workflow.
         # Live calls route them through the LLM classifier, which sees the
         # greeting context and picks this intent only at the call opening.
         "samples": ["hello", "namaste", "help me with my booking",
                     "i am calling about my booking",
                     "booking ke baare mein baat karni hai",
                     "मुझे अपनी बुकिंग के बारे में बात करनी है"],
         "confidenceThreshold": 0.6, "route": WF1},
        {"name": "cancel_booking", "category": "out_of_scope",
         "description": "Cancellations are out of scope — transfer back to the IVR queue.",
         "samples": ["cancel my booking", "cancellation", "want to cancel",
                     "cancel the reservation", "how do i cancel",
                     "i want to cancel my booking", "cancel this booking",
                     "booking cancel karni hai", "बुकिंग कैंसिल करनी है"],
         "confidenceThreshold": 0.7, "route": "handoff",
         "handoffEnabled": True},
        {"name": "refund_status", "category": "out_of_scope",
         "description": "Refunds are out of scope — transfer back to the IVR queue.",
         "samples": ["refund", "money back", "refund status",
                     "when will i get my refund", "where is my refund",
                     "i want my refund", "i want my money back",
                     "refund kab milega", "रिफंड कब मिलेगा"],
         "confidenceThreshold": 0.7, "route": "handoff",
         "handoffEnabled": True},
        {"name": "new_booking", "category": "out_of_scope",
         "description": "New bookings are out of scope — transfer back to the IVR queue.",
         "samples": ["new booking", "book a room", "make a booking",
                     "book a hotel", "new reservation",
                     "i want to book a room", "naya room book karna hai",
                     "नई बुकिंग करनी है"],
         "confidenceThreshold": 0.65, "route": "handoff",
         "handoffEnabled": True},
        {"name": "complaint_escalation", "category": "out_of_scope",
         "description": "Complaints/escalations go to a human agent.",
         "samples": ["complaint", "file a complaint", "very bad experience",
                     "escalate this", "i want to file a complaint",
                     "i want to escalate this", "shikayat karni hai",
                     "शिकायत दर्ज करनी है"],
         "confidenceThreshold": 0.7, "route": "handoff",
         "handoffEnabled": True},
        {"name": "booking_fact_question", "category": "booking",
         "description": ("Caller asks about a fact of their OWN booking "
                         "(dates, hotel, amounts) — answered by the LLM from "
                         "verified context, never by KB retrieval."),
         "samples": ["when is my check-in", "when is my check-out",
                     "which hotel", "what is my hotel name",
                     "can you confirm my hotel name", "tell me my hotel name",
                     "hotel name please", "what are my booking dates",
                     "how much did i pay", "what is my pending amount",
                     "what is my payment status", "what is my occupancy",
                     "what is my check-in date", "what is my checkout date",
                     "kab hai mera check-in",
                     "मेरा चेक-इन कब है"],
         "confidenceThreshold": 0.4, "route": ""},
    ],
    BOT2: [
        {"name": "pm_call_opening", "category": "flow",
         "description": "PM answers the outbound call — enter the verification flow.",
         "samples": ["hello", "hi", "yes", "speaking", "haan", "who is this",
                     "bolo", "हाँ"],
         "confidenceThreshold": 0.05, "route": WF2},
        {"name": "pm_confirms", "category": "flow",
         "description": "PM confirms the booking straight away.",
         "samples": ["the booking is confirmed", "we will honor",
                     "booking is confirmed", "confirmed",
                     "we can accommodate the guest"],
         "confidenceThreshold": 0.05, "route": WF2},
        {"name": "pm_denies", "category": "flow",
         "description": "PM declines or raises overbooking / maintenance / price.",
         "samples": ["we cannot honor", "cannot honor this booking",
                     "we are overbooked", "overbooked",
                     "property is under maintenance", "price is too low",
                     "booking price is low", "cannot accommodate"],
         "confidenceThreshold": 0.05, "route": WF2},
    ],
    BOT3: [
        {"name": "stock_call_opening", "category": "flow",
         "description": "Stock team answers — enter the validation flow.",
         "samples": ["hello", "hi", "yes", "speaking", "haan", "go ahead", "हाँ"],
         "confidenceThreshold": 0.05, "route": WF3},
        {"name": "stock_response", "category": "flow",
         "description": "Stock team responds about the booking.",
         "samples": ["booking will be honored", "it is confirmed",
                     "cannot honor", "let me check", "checking the booking",
                     "which booking"],
         "confidenceThreshold": 0.05, "route": WF3},
    ],
}

# Demo booking facts for post-verification LLM questions in Testing Studio.
# This flag deliberately starts false: only the verification workflow may
# establish identity for the current session; Manual Test JSON never can.
RUNTIME_CONTEXT = {
    "name": "Verified booking facts",
    "sourceMode": "manual",
    "fields": [],
    "allowAdditional": True,
    "testPayload": {
        "identity_verified": False,
        "booking_id": "601001",
        "guest_name": "Rahul Sharma",
        "hotel_name": "OYO Townhouse 121 Sector 29 Gurugram",
        "hotel_city": "Gurugram",
        "checkin_date": "2026-08-20",
        "checkout_date": "2026-08-22",
        "occupancy": "2 guests, 1 room",
        "room_type": "Deluxe",
        "booking_status": "confirmed",
        "payment_status": "partially_paid",
        "booking_amount_inr": 3400,
        "amount_paid_inr": 1000,
        "amount_pending_inr": 2400,
    },
    "missingValuePolicy": ("Never guess a booking fact. If a value is not in the "
                           "context, ask for the booking ID and verify, or offer "
                           "to transfer to support."),
    "domainPolicy": "generic",
}


def check(r, what):
    if r.status_code >= 300:
        raise SystemExit(f"FAIL {what}: {r.status_code} {r.text[:500]}")
    print(f"ok   {what}")
    return r.json().get("data")


c = httpx.Client(base_url=BASE, timeout=30)
token = check(c.post("/auth/login", json={"email": "oyo.config@oyo.com",
                                          "password": "Demo@2026!"}), "login")["token"]
c.headers["Authorization"] = f"Bearer {token}"

for bot_id, intents in INTENTS.items():
    existing = {i["name"]: i["id"]
                for i in check(c.get(f"/bots/{bot_id}/intents"), f"list intents {bot_id}")}
    for intent in intents:
        if intent["name"] in existing:
            check(c.patch(f"/intents/{existing[intent['name']]}", json=intent),
                  f"update intent {intent['name']}")
        else:
            check(c.post(f"/bots/{bot_id}/intents", json=intent),
                  f"intent {intent['name']} ({bot_id})")

# A manually edited Testing-Studio payload is deliberate demo data — reruns
# of this stage must never clobber it. Only the very first run seeds the
# default payload.
existing_context = check(c.get(f"/bots/{BOT1}/runtime-context"),
                         "read runtime context bot 1") or {}
existing_payload = (existing_context.get("testPayload")
                    if isinstance(existing_context, dict) else None)
if isinstance(existing_payload, dict) and existing_payload:
    RUNTIME_CONTEXT["testPayload"] = existing_payload
check(c.put(f"/bots/{BOT1}/runtime-context", json=RUNTIME_CONTEXT),
      "runtime context bot 1")
print("intents + context done")
