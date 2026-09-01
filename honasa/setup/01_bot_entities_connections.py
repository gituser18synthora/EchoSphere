"""Stage 01 — Honasa bot, voice settings, entities and API connections.

Creates (idempotently, via REST as the tenant-admin service account):
  - bot "Honasa Customer Care" (en-IN + hi-IN) under tn_620d5400d462
  - voice settings: Sarvam saaras:v3 STT (auto language detection),
    Sarvam bulbul:v3 TTS voice Shreya for both languages (en-IN default),
    gpt-4o-mini orchestration with time context enabled
  - tenant entity definitions used by intents/asks
  - the API connections against the Honasa mock commerce service (port 9022)

Connection design notes (engine contract):
  - The lookup connection has NO bodyTemplate: workflow api nodes then send
    the full scalar slot state, and the mock resolves order_ref2 (retry ask)
    over order_ref (first ask).
  - Resolution (return/replacement) connections pin ``issue_type`` and
    ``resolution`` as bodyTemplate constants — workflow api nodes can only
    send slots, so each deterministic branch gets its own connection (the
    proven per-path-constant pattern).
  - State-changing actions (returns, tracking link) set requireConfirmation:
    the executor refuses them until the lookup has mapped
    ``verified -> customer_verified``. The escalation connection deliberately
    does NOT require confirmation — a caller whose order lookup failed must
    still reach a human.

Run: env/bin/python honasa/setup/01_bot_entities_connections.py
"""

import json
import sys

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
MOCK = "http://127.0.0.1:9022/api/v1"
TENANT = "tn_620d5400d462"
BOT_NAME = "Honasa Customer Care"
VOICE = "vp-sv-shreya"

STATE_FILE = __file__.rsplit("/", 1)[0] + "/honasa_config_state.json"


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
    r = c.post("/auth/login", json={"email": "honasa.config@honasa.com",
                                    "password": "Demo@2026!"})
    r.raise_for_status()
    c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"
    return c


def check(r: httpx.Response, what: str):
    if r.status_code >= 300:
        print(f"FAIL {what}: {r.status_code} {r.text[:500]}")
        sys.exit(1)
    print(f"ok   {what}")
    return r.json().get("data")


# ── bot ──────────────────────────────────────────────────────────────────────


def stage_bot(c: httpx.Client, state: dict):
    existing = check(c.get("/bots", params={"tenantId": TENANT}), "list bots")
    by_name = {b["name"]: b["id"] for b in existing}
    if BOT_NAME in by_name:
        state["BOT"] = by_name[BOT_NAME]
        print(f"reuse bot {state['BOT']}")
    else:
        bot = check(c.post("/bots", json={
            "name": BOT_NAME,
            "useCase": "Order information & returns support",
            "description": (
                "Inbound customer-care voice bot for Honasa's D2C brands "
                "(Mamaearth, The Derma Co, Aqualogica, Dr. Sheth's, BBlunt). "
                "POC scope: Order/Information (status, ETA, tracking, amount, "
                "discount/cashback, refund status) and Return/Replacement "
                "(eligibility, damaged/wrong/missing/defective-or-expired "
                "items) per the Honasa FAQ response bank. Out-of-scope "
                "requests are transferred to a support executive."),
            "languages": ["en-IN", "hi-IN"],
            "tenantId": TENANT,
        }), "create bot")
        state["BOT"] = bot["id"]
    save_state(state)

    check(c.patch(f"/bots/{state['BOT']}", json={"voiceId": VOICE}),
          f"bot voiceId -> {VOICE}")

    check(c.put(f"/bots/{state['BOT']}/voice-settings", json={
        "voiceId": VOICE,
        "speed": 1.0, "pauseMs": 250, "empathy": 60, "energy": 35,
        "languageVoiceMap": {
            "en-IN": {"provider": "sarvam", "model": "bulbul:v3", "voice": VOICE,
                      "params": {"temperature": 0.01, "min_buffer_size": 50,
                                 "max_chunk_length": 150,
                                 "send_completion_event": True}},
            "hi-IN": {"provider": "sarvam", "model": "bulbul:v3", "voice": VOICE,
                      "params": {"temperature": 0.01, "min_buffer_size": 50,
                                 "max_chunk_length": 150,
                                 "send_completion_event": True}},
            "default": "en-IN",
        },
        "sttProvider": "sarvam", "sttModel": "saaras:v3",
        "sttSettings": {
            "mode": "transcribe", "vad_signals": True,
            "input_encoding": "pcm_s16le", "timeout_seconds": 30,
            "min_speech_frames": 2, "auto_detect_language": True,
            "high_vad_sensitivity": False,
            "negative_speech_threshold": 0.45,
            "positive_speech_threshold": 0.7,
            "interrupt_min_speech_frames": 2,
        },
        "ttsProvider": "sarvam", "ttsModel": "bulbul:v3",
        "ttsSettings": {"temperature": 0.01, "min_buffer_size": 50,
                        "max_chunk_length": 150, "send_completion_event": True},
        "llmProvider": "openai", "llmModel": "gpt-4o-mini",
        "llmSettings": {"max_tokens": 150, "max_retries": 1, "temperature": 0.3,
                        "timeout_seconds": 30, "orchestration_timeout_seconds": 2.0,
                        "time_context_enabled": True,
                        "max_output_characters": 360},
        "audioSettings": {"browser": {"codec": "linear16", "sampleRate": 16000},
                          "telephony": {"codec": "mulaw", "sampleRate": 8000}},
    }), "voice settings")


# ── entities ─────────────────────────────────────────────────────────────────


ENTITIES = [
    {"name": "order_id", "kind": "regex", "dataType": "text",
     "regexPattern": r"(?<![0-9])([0-9]{7})(?![0-9])",
     "description": "Honasa order reference (7-digit) shared by the caller.",
     "example": "7001001"},
    {"name": "registered_phone", "kind": "regex", "dataType": "text",
     "regexPattern": r"(?<![0-9])([0-9]{10})(?![0-9])",
     "description": "Mobile number registered with the order (alternate lookup key).",
     "example": "9876501001", "pii": True},
    {"name": "product_name", "kind": "custom", "dataType": "text",
     "description": "Product the caller refers to in a return/replacement request.",
     "example": "Mamaearth Vitamin C Face Wash"},
    {"name": "issue_description", "kind": "custom", "dataType": "text",
     "description": "Caller's description of what is wrong with the product/order.",
     "example": "the pump is broken and the bottle leaked"},
]


def stage_entities(c: httpx.Client, state: dict):
    existing = {
        e["name"]: e["id"]
        for e in check(c.get("/entities", params={"tenantId": TENANT}),
                       "list entities")
    }
    for entity in ENTITIES:
        if entity["name"] in existing:
            check(c.patch(f"/entities/{existing[entity['name']]}", json={
                key: value for key, value in entity.items() if key != "name"
            }), f"update entity {entity['name']}")
            continue
        check(c.post("/entities", json={**entity, "tenantId": TENANT}),
              f"entity {entity['name']}")


# ── API connections ──────────────────────────────────────────────────────────


LOOKUP_MAPPING = [
    {"source": "verified", "target": "customer_verified"},
    {"source": "order_id", "target": "order_id"},
    {"source": "customer_name", "target": "customer_name"},
    {"source": "order_status", "target": "order_status"},
    # Mapped as order_items on purpose: a key containing the word "product"
    # would make the verified-context question matcher intercept quality
    # statements like "the product is past its expiry date" away from the
    # workflow (mentions_context_fact treats non-generic key words as
    # distinctive).
    {"source": "product_summary", "target": "order_items"},
    {"source": "item_count", "target": "item_count"},
    {"source": "order_placed_on", "target": "order_placed_on"},
    {"source": "shipped_on", "target": "shipped_on"},
    {"source": "expected_delivery_date", "target": "expected_delivery_date"},
    {"source": "delivered_on", "target": "delivered_on"},
    {"source": "days_since_delivery", "target": "days_since_delivery"},
    {"source": "courier_name", "target": "courier_name"},
    {"source": "tracking_available", "target": "tracking_available"},
    {"source": "order_amount_inr", "target": "order_amount_inr"},
    {"source": "payment_mode", "target": "payment_mode"},
    {"source": "discount_inr", "target": "discount_inr"},
    {"source": "cashback_inr", "target": "cashback_inr"},
    {"source": "refund_status", "target": "refund_status"},
    {"source": "refund_amount_inr", "target": "refund_amount_inr"},
    {"source": "refund_initiated_on", "target": "refund_initiated_on"},
    {"source": "refund_expected_by", "target": "refund_expected_by"},
    {"source": "refund_mode", "target": "refund_mode"},
    {"source": "return_eligible", "target": "return_eligible"},
    {"source": "return_window_days", "target": "return_window_days"},
    {"source": "return_window_days_left", "target": "return_window_days_left"},
    {"source": "return_ineligible_reason", "target": "return_ineligible_reason"},
    {"source": "registered_phone_masked", "target": "registered_phone_masked"},
    {"source": "multiple_orders_on_phone", "target": "multiple_orders_on_phone"},
]

RESOLUTION_MAPPING = [
    {"source": "request_id", "target": "resolution_request_id"},
    {"source": "resolution", "target": "resolution_type"},
    {"source": "issue_type", "target": "resolution_issue_type"},
    {"source": "whatsapp_link_sent", "target": "whatsapp_link_sent"},
    {"source": "whatsapp_number_masked", "target": "whatsapp_number_masked"},
]


def _resolution_connection(label: str, issue_type: str, resolution: str,
                           details_slot: str, description: str) -> dict:
    return {
        "name": f"Honasa {label}",
        "description": description,
        "method": "POST", "url": f"{MOCK}/orders/{{order_id}}/returns",
        "isStateChanging": True, "requireConfirmation": True,
        "bodyTemplate": {"issue_type": issue_type, "resolution": resolution,
                         "details": "{" + details_slot + "}"},
        "responseMapping": RESOLUTION_MAPPING,
    }


CONNECTIONS = [
    {
        "name": "Honasa Order Lookup",
        "description": ("Resolves an order by order ID or registered mobile "
                        "number and returns the full order view: status, ETA, "
                        "tracking, amounts, discount/cashback, refund state and "
                        "return eligibility (seven-day policy computed "
                        "server-side). Marks the caller verified for "
                        "state-changing follow-ups."),
        "method": "POST", "url": f"{MOCK}/orders/lookup",
        "responseMapping": LOOKUP_MAPPING,
    },
    {
        "name": "Honasa Send Tracking Link",
        "description": ("Sends the live tracking link for the order to the "
                        "registered number over WhatsApp (FAQ: 'Can you share "
                        "the tracking link?'). Fails when tracking is not live "
                        "yet."),
        "method": "POST", "url": f"{MOCK}/orders/{{order_id}}/tracking-link",
        "isStateChanging": True, "requireConfirmation": True,
        "responseMapping": [
            {"source": "sent", "target": "tracking_link_sent"},
            {"source": "whatsapp_number_masked", "target": "whatsapp_number_masked"},
        ],
    },
    _resolution_connection(
        "Return Request", "no_longer_needed", "return", "return_note",
        "Change-of-mind return (FAQ: 'I want to return my product'). Server "
        "re-validates the seven-day eligibility window; the return link is "
        "shared over WhatsApp."),
    _resolution_connection(
        "Damaged Replacement", "damaged", "replacement", "damage_details",
        "Replacement for a damaged product (FAQ row: damaged product)."),
    _resolution_connection(
        "Damaged Return", "damaged", "return", "damage_details",
        "Return + refund for a damaged product (FAQ row: damaged product)."),
    _resolution_connection(
        "Wrong Item Replacement", "wrong_item", "replacement", "wrong_details",
        "Replacement with the correct item (FAQ row: wrong product received)."),
    _resolution_connection(
        "Wrong Item Return", "wrong_item", "return", "wrong_details",
        "Return + refund for a wrong item (FAQ row: wrong product received)."),
    _resolution_connection(
        "Missing Item Replacement", "missing_item", "replacement", "missing_details",
        "Ships the missing/incomplete item (FAQ row: product missing/incomplete)."),
    _resolution_connection(
        "Missing Item Return", "missing_item", "return", "missing_details",
        "Refund for the missing/incomplete item (FAQ row: product missing/incomplete)."),
    _resolution_connection(
        "Defective Replacement", "defective_expired", "replacement", "defect_details",
        "Replacement for a defective or expired product (FAQ row: defective/expired)."),
    _resolution_connection(
        "Defective Return", "defective_expired", "return", "defect_details",
        "Return + refund for a defective or expired product (FAQ row: defective/expired)."),
    {
        "name": "Honasa Support Escalation",
        "description": ("Creates a support ticket with the call's slot state "
                        "before transferring to a human executive. Deliberately "
                        "does not require a verified caller — lookup failures "
                        "must still reach support."),
        "method": "POST", "url": f"{MOCK}/support/escalations",
        "isStateChanging": True,
        "responseMapping": [
            {"source": "ticket_id", "target": "escalation_ticket_id"},
        ],
    },
]


def stage_connections(c: httpx.Client, state: dict):
    existing = {a["name"]: a["id"]
                for a in check(c.get("/api-connections", params={"tenantId": TENANT}),
                               "list connections")}
    for conn in CONNECTIONS:
        if conn["name"] in existing:
            check(c.patch(f"/api-connections/{existing[conn['name']]}",
                          json={k: v for k, v in conn.items() if k != "name"}),
                  f"update connection {conn['name']}")
            continue
        check(c.post("/api-connections", json={**conn, "tenantId": TENANT}),
              f"connection {conn['name']}")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    c = client()
    state = load_state()
    stages = {"bot": stage_bot, "entities": stage_entities,
              "connections": stage_connections}
    if stage == "all":
        for fn in stages.values():
            fn(c, state)
    else:
        stages[stage](c, state)
    save_state(state)
    print("state:", json.dumps(state))
