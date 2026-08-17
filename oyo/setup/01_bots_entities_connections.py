"""Configure the OYO tenant (tn_de5cc992b1e9) meta-bot solution via REST.

Stages (run: python configure_oyo.py <stage>):
  bots        - rename bot 1, create bots 2+3, voice settings
  entities    - tenant entity definitions
  connections - API connections against the oyo mock service (port 9021)
  prompts     - system + greeting prompts for all three bots (publish)
  workflows   - the three workflow graphs (approved)
  intents     - router intents per bot
  context     - runtime-context schema for bot 1
  all         - everything in order
"""

import json
import sys

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
MOCK = "http://127.0.0.1:9021/api/v1"
TENANT = "tn_de5cc992b1e9"
BOT1 = "bot_e8cf0b05bb79"

STATE_FILE = __file__.rsplit("/", 1)[0] + "/oyo_config_state.json"


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def client() -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.post("/auth/login", json={"email": "oyo.config@oyo.com",
                                    "password": "Demo@2026!"})
    r.raise_for_status()
    token = r.json()["data"]["token"]
    c.headers["Authorization"] = f"Bearer {token}"
    return c


def check(r: httpx.Response, what: str):
    if r.status_code >= 300:
        print(f"FAIL {what}: {r.status_code} {r.text[:500]}")
        sys.exit(1)
    print(f"ok   {what}")
    return r.json().get("data")


# ── stage: bots ──────────────────────────────────────────────────────────────


def stage_bots(c: httpx.Client, state: dict):
    check(c.patch(f"/bots/{BOT1}", json={
        "name": "OYO Booking Support",
        "useCase": "Booking confirmation & upcoming-stay support",
        "description": ("Inbound customer bot for the OYO IVR 'upcoming booking' queue. "
                        "Handles booking confirmation, booking details, booking voucher and "
                        "check-in confirmation; orchestrates property-manager and stock-team "
                        "verification and the hotel-shift flow; transfers out-of-scope calls "
                        "back to the IVR."),
    }), "rename bot 1")

    existing = check(c.get("/bots", params={"tenantId": TENANT}), "list bots")
    by_name = {b["name"]: b["id"] for b in existing}

    if "OYO Property Verification" in by_name:
        state["BOT2"] = by_name["OYO Property Verification"]
        print(f"reuse bot 2 {state['BOT2']}")
    else:
        b2 = check(c.post("/bots", json={
            "name": "OYO Property Verification",
            "useCase": "Property manager check-in confirmation",
            "description": ("Outbound bot ('Amit') that calls the property manager to confirm an "
                            "upcoming booking will be honored. Handles denial scenarios: overbooked "
                            "(backend occupancy check + penalty advisory), maintenance (alternate "
                            "room), price-related (7-day ARR comparison / complimentary amount). "
                            "Reports the outcome to the verification backend for the customer bot."),
            "languages": ["en-IN", "hi-IN"],
            "tenantId": TENANT,
        }), "create bot 2")
        state["BOT2"] = b2["id"]

    if "OYO Stock Team Validation" in by_name:
        state["BOT3"] = by_name["OYO Stock Team Validation"]
        print(f"reuse bot 3 {state['BOT3']}")
    else:
        b3 = check(c.post("/bots", json={
            "name": "OYO Stock Team Validation",
            "useCase": "Stock team booking validation",
            "description": ("Outbound bot that calls the internal Stock Team when the property "
                            "manager is unreachable or does not confirm a booking (spec Flow 7). "
                            "Reports whether the booking will be honoured."),
            "languages": ["en-IN", "hi-IN"],
            "tenantId": TENANT,
        }), "create bot 3")
        state["BOT3"] = b3["id"]

    vs1 = check(c.get(f"/bots/{BOT1}/voice-settings"), "read bot1 voice settings")
    for bot_id, voice in ((state["BOT2"], "vp-el-niraj"), (state["BOT3"], "vp-el-viraj")):
        check(c.put(f"/bots/{bot_id}/voice-settings", json={
            "voiceId": voice,
            "speed": vs1["speed"], "pauseMs": vs1["pauseMs"],
            "empathy": vs1["empathy"], "energy": vs1["energy"],
            "languageVoiceMap": {
                "en-IN": {"provider": "elevenlabs", "model": "eleven_flash_v2_5", "voice": voice},
                "hi-IN": {"provider": "elevenlabs", "model": "eleven_flash_v2_5", "voice": voice},
                "default": "en-IN",
            },
            "sttProvider": vs1["sttProvider"], "sttModel": vs1["sttModel"],
            "sttSettings": vs1["sttSettings"],
            "ttsProvider": vs1["ttsProvider"], "ttsModel": vs1["ttsModel"],
            "ttsSettings": vs1["ttsSettings"],
            "llmProvider": vs1["llmProvider"], "llmModel": vs1["llmModel"],
            "audioSettings": vs1["audioSettings"],
        }), f"voice settings {bot_id}")
    save_state(state)


# ── stage: entities ──────────────────────────────────────────────────────────


ENTITIES = [
    {"name": "booking_id", "kind": "regex", "dataType": "text",
     "regexPattern": r"(?:BK[-\s]?)?([0-9]{4,10})",
     "description": "OYO booking reference shared by the caller.",
     "example": "601001"},
    {"name": "guest_name", "kind": "custom", "dataType": "text",
     "description": "Guest name on the booking, used for caller verification (Flow 2).",
     "example": "Rahul Sharma"},
    {"name": "email_address", "kind": "custom", "dataType": "email",
     "description": "Email address for the booking voucher (Flow 4).",
     "example": "guest@example.com", "pii": True},
    {"name": "checkin_date", "kind": "custom", "dataType": "date",
     "description": "Check-in date, alternate verification detail.",
     "example": "2026-08-20"},
    {"name": "hotel_name", "kind": "custom", "dataType": "text",
     "description": "Hotel/property name, alternate verification detail.",
     "example": "OYO Townhouse 121 Sector 29 Gurugram"},
]


def stage_entities(c: httpx.Client, state: dict):
    existing = {e["name"] for e in check(c.get("/entities", params={"tenantId": TENANT}),
                                         "list entities")}
    for entity in ENTITIES:
        if entity["name"] in existing:
            print(f"reuse entity {entity['name']}")
            continue
        check(c.post("/entities", json={**entity, "tenantId": TENANT}),
              f"entity {entity['name']}")


# ── stage: connections ───────────────────────────────────────────────────────


CONNECTIONS = [
    {
        "name": "OYO Customer Verification",
        "description": "Verifies the caller against the booking record (booking ID + guest name / phone / hotel / check-in date). 200 only when verified.",
        "method": "POST", "url": f"{MOCK}/customers/verify",
        "bodyTemplate": {"booking_id": "{booking_id}", "guest_name": "{guest_name}",
                         "caller_phone": "{caller_phone}"},
        "responseMapping": [
            {"source": "verified", "target": "customer_verified"},
            {"source": "matched_on", "target": "verified_via"},
        ],
    },
    {
        "name": "OYO Booking Details",
        "description": "Fetches the booking record: status, hotel, dates, occupancy, payment, property id.",
        "method": "GET", "url": f"{MOCK}/bookings/{{booking_id}}",
        "responseMapping": [
            {"source": "booking_status", "target": "booking_status"},
            {"source": "hotel_name", "target": "hotel_name"},
            {"source": "property_id", "target": "property_id"},
            {"source": "checkin_date", "target": "checkin_date"},
            {"source": "checkout_date", "target": "checkout_date"},
            {"source": "occupancy", "target": "occupancy_details"},
            {"source": "payment_status", "target": "payment_status"},
            {"source": "amount_pending", "target": "amount_pending"},
            {"source": "guest_email", "target": "guest_email"},
            {"source": "cancelled_on", "target": "cancelled_on"},
            {"source": "cancelled_by", "target": "cancelled_by"},
            {"source": "city", "target": "hotel_city"},
        ],
    },
    {
        "name": "OYO Booking Voucher",
        "description": "Emails the booking voucher (Flow 4). State-changing; requires a verified caller.",
        "method": "POST", "url": f"{MOCK}/bookings/{{booking_id}}/voucher",
        "isStateChanging": True, "requireConfirmation": True,
        "responseMapping": [
            {"source": "sent", "target": "voucher_sent"},
            {"source": "email", "target": "voucher_email"},
        ],
    },
    {
        "name": "OYO PM Verification Call",
        "description": "Orchestrates the outbound Property-Manager verification call (handled by the OYO Property Verification bot) and returns its outcome. Live verification reports win over scripted outcomes.",
        "method": "POST", "url": f"{MOCK}/calls/property-manager",
        "isStateChanging": True, "requireConfirmation": True,
        "timeoutMs": 8000,
        "responseMapping": [
            {"source": "call_status", "target": "pm_call_status"},
            {"source": "booking_honored", "target": "pm_booking_honored"},
            {"source": "deny_reason", "target": "pm_deny_reason"},
            {"source": "resolution", "target": "pm_resolution"},
            {"source": "source", "target": "pm_outcome_source"},
        ],
    },
    {
        "name": "OYO Stock Team Call",
        "description": "Orchestrates the outbound Stock-Team validation call (handled by the OYO Stock Team Validation bot) and returns its outcome (spec Flow 7).",
        "method": "POST", "url": f"{MOCK}/calls/stock-team",
        "isStateChanging": True, "requireConfirmation": True,
        "timeoutMs": 8000,
        "responseMapping": [
            {"source": "call_status", "target": "stock_call_status"},
            {"source": "stock_status", "target": "stock_status"},
        ],
    },
    {
        "name": "OYO Alternate Properties",
        "description": "Nearby alternate OYO properties with similar amenities, for the relocation offer.",
        "method": "GET", "url": f"{MOCK}/properties/{{property_id}}/alternates",
        "responseMapping": [
            {"source": "count", "target": "alternates_count"},
            {"source": "top_alternate_name", "target": "alternate_property_name"},
        ],
    },
    {
        "name": "OYO Shift Booking",
        "description": "Shift API (spec Flow 8): moves the booking to the best alternate property. State-changing; requires a verified caller.",
        "method": "POST", "url": f"{MOCK}/bookings/{{booking_id}}/shift",
        "isStateChanging": True, "requireConfirmation": True,
        "responseMapping": [
            {"source": "status", "target": "shift_status"},
            {"source": "new_property_name", "target": "shift_property_name"},
        ],
    },
    {
        "name": "OYO Property Occupancy",
        "description": "Backend occupancy for the overbooking check during PM verification.",
        "method": "GET", "url": f"{MOCK}/properties/{{property_id}}/occupancy",
        "responseMapping": [
            {"source": "has_availability", "target": "has_availability"},
            {"source": "occupancy_pct", "target": "occupancy_pct"},
        ],
    },
    {
        "name": "OYO Property Status",
        "description": "Operational status + hold/blocked reasons (maintenance check).",
        "method": "GET", "url": f"{MOCK}/properties/{{property_id}}/status",
        "responseMapping": [
            {"source": "under_maintenance", "target": "under_maintenance"},
            {"source": "operational_status", "target": "operational_status"},
        ],
    },
    {
        "name": "OYO Property Pricing",
        "description": "7-day ARR vs booking rate and the available complimentary amount, for price-related denials.",
        "method": "GET", "url": f"{MOCK}/properties/{{property_id}}/pricing",
        "queryParams": {"booking_id": "{booking_id}"},
        "responseMapping": [
            {"source": "rate_vs_arr", "target": "rate_vs_arr"},
            {"source": "arr_7day", "target": "arr_7day"},
            {"source": "booking_rate", "target": "booking_rate"},
            {"source": "complimentary_amount", "target": "complimentary_amount"},
        ],
    },
    {
        "name": "OYO Add Complimentary Amount",
        "description": "Adds the complimentary compensation amount to the booking after the PM accepts the price resolution.",
        "method": "POST", "url": f"{MOCK}/bookings/{{booking_id}}/complimentary",
        "isStateChanging": True,
        "responseMapping": [
            {"source": "added", "target": "comp_added"},
            {"source": "amount", "target": "comp_amount"},
        ],
    },
    {
        "name": "OYO PM Report Honored",
        "description": "Records the PM call outcome: booking honored. Read back by the customer bot's PM Verification Call.",
        "method": "POST", "url": f"{MOCK}/verification-reports",
        "queryParams": {"channel": "pm", "outcome": "honored"},
        "isStateChanging": True,
    },
    {
        "name": "OYO PM Report Not Honored",
        "description": "Records the PM call outcome: booking not honored (with deny_reason from the conversation).",
        "method": "POST", "url": f"{MOCK}/verification-reports",
        "queryParams": {"channel": "pm", "outcome": "not_honored"},
        "isStateChanging": True,
    },
    {
        "name": "OYO Stock Report Honored",
        "description": "Records the Stock-Team call outcome: booking will be honoured.",
        "method": "POST", "url": f"{MOCK}/verification-reports",
        "queryParams": {"channel": "stock", "outcome": "honored"},
        "isStateChanging": True,
    },
    {
        "name": "OYO Stock Report Not Honored",
        "description": "Records the Stock-Team call outcome: booking cannot be confirmed.",
        "method": "POST", "url": f"{MOCK}/verification-reports",
        "queryParams": {"channel": "stock", "outcome": "not_honored"},
        "isStateChanging": True,
    },
    {
        "name": "OYO Call Disposition",
        "description": "CRM disposition update at call closure (spec Flow 9). The full slot state of the call rides along in the body.",
        "method": "POST", "url": f"{MOCK}/crm/dispositions",
    },
    {
        "name": "OYO IVR Transfer",
        "description": "Signals the IVR that the call is being routed back to a human queue.",
        "method": "POST", "url": f"{MOCK}/ivr/transfer",
    },
]


def stage_connections(c: httpx.Client, state: dict):
    existing = {a["name"]: a["id"]
                for a in check(c.get("/api-connections", params={"tenantId": TENANT}),
                               "list connections")}
    for conn in CONNECTIONS:
        if conn["name"] in existing:
            print(f"reuse connection {conn['name']}")
            continue
        check(c.post("/api-connections", json={**conn, "tenantId": TENANT}),
              f"connection {conn['name']}")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    c = client()
    state = load_state()
    stages = {
        "bots": stage_bots,
        "entities": stage_entities,
        "connections": stage_connections,
    }
    if stage == "all":
        for fn in stages.values():
            fn(c, state)
    else:
        stages[stage](c, state)
    save_state(state)
    print("state:", json.dumps(state))
