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

INTENTS = {
    BOT1: [
        {"name": "booking_confirmation", "category": "booking",
         "description": "Caller wants to know whether their booking is confirmed.",
         "samples": ["is my booking confirmed", "booking confirmation",
                     "confirm my booking", "booking status",
                     "is my reservation confirmed", "check my booking"],
         "confidenceThreshold": 0.05, "route": WF1,
         "entities": [], "optionalEntities": ["booking_id"]},
        {"name": "checkin_confirmation", "category": "booking",
         "description": "Caller wants OYO to confirm the stay directly with the property (spec Flow 6).",
         "samples": ["confirm with the hotel", "confirm with the property",
                     "will the hotel honor my booking", "check-in confirmation",
                     "confirm my check-in", "verify with the property"],
         "confidenceThreshold": 0.05, "route": WF1,
         "optionalEntities": ["booking_id"]},
        {"name": "booking_voucher", "category": "booking",
         "description": "Caller wants the booking voucher emailed (spec Flow 4).",
         "samples": ["booking voucher", "send my voucher", "email me the voucher",
                     "need the voucher", "voucher for my booking"],
         "confidenceThreshold": 0.05, "route": WF1,
         "optionalEntities": ["booking_id", "email_address"]},
        {"name": "booking_details", "category": "booking",
         "description": "Caller asks for their booking details (spec Flow 5); enters the verified flow first.",
         "samples": ["booking details", "my booking details",
                     "share the booking details", "details of my booking"],
         "confidenceThreshold": 0.05, "route": WF1,
         "optionalEntities": ["booking_id"]},
        {"name": "call_opening_response", "category": "flow",
         "description": "Short acknowledgements after the greeting — start the guided booking flow.",
         # NOTE: never use short substring-prone samples here ("hi" matches
         # inside "which", "yes" inside "yesterday") — the router matches
         # samples as substrings of the utterance.
         "samples": ["hello", "namaste", "haan ji", "hanji", "हाँ", "help me with my booking"],
         "confidenceThreshold": 0.05, "route": WF1},
        {"name": "cancel_booking", "category": "out_of_scope",
         "description": "Cancellations are out of scope — transfer back to the IVR queue.",
         "samples": ["cancel my booking", "cancellation", "want to cancel",
                     "cancel the reservation", "how do i cancel"],
         "confidenceThreshold": 0.05, "route": "handoff",
         "handoffEnabled": True},
        {"name": "refund_status", "category": "out_of_scope",
         "description": "Refunds are out of scope — transfer back to the IVR queue.",
         "samples": ["refund", "money back", "refund status",
                     "when will i get my refund"],
         "confidenceThreshold": 0.05, "route": "handoff",
         "handoffEnabled": True},
        {"name": "new_booking", "category": "out_of_scope",
         "description": "New bookings are out of scope — transfer back to the IVR queue.",
         "samples": ["new booking", "book a room", "make a booking",
                     "book a hotel", "new reservation"],
         "confidenceThreshold": 0.05, "route": "handoff",
         "handoffEnabled": True},
        {"name": "complaint_escalation", "category": "out_of_scope",
         "description": "Complaints/escalations go to a human agent.",
         "samples": ["complaint", "file a complaint", "very bad experience",
                     "escalate this"],
         "confidenceThreshold": 0.05, "route": "handoff",
         "handoffEnabled": True},
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

# Verified demo caller for LLM-answered booking-detail questions in the
# Testing Studio (booking 601001 — matches oyo/data/bookings.json).
RUNTIME_CONTEXT = {
    "name": "Verified booking facts",
    "sourceMode": "manual",
    "fields": [],
    "allowAdditional": True,
    "testPayload": {
        "identity_verified": True,
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

check(c.put(f"/bots/{BOT1}/runtime-context", json=RUNTIME_CONTEXT),
      "runtime context bot 1")
print("intents + context done")
