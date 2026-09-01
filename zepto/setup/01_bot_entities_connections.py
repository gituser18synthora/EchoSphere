"""Stage 01 — Zepto Support bot, voice settings, entities, concern tools.

Creates (idempotently, via REST as the tenant-admin service account):
  - bot "Zepto Support" (hi-IN default + en-IN) under tn_04250683f1b3 — an
    INBOUND delivery-partner helpdesk bot built from the four approved call
    scripts in tenant/zepto/ (Image.jpg = RTO, Image-1.jpg = MDND,
    Image-2.jpg = Raincoat/T-shirt/Bag deduction + Onboarding-fee deduction).
  - voice settings: Sarvam saaras:v3 STT (auto language detection — partners
    speak English, Hindi and Hinglish), Sarvam bulbul:v3 TTS voice Kavya for
    both languages (hi-IN default — partners are Hindi-first; the scripted
    node questions stay in English as authored in the approved scripts),
    gpt-4o-mini orchestration with time context enabled.
  - tenant entity definitions used by the workflow's ask nodes and intents.
  - FOUR API connections, one per concern, each pinning its concern code in
    the bodyTemplate (per-branch connections, the honasa resolution-connection
    recipe) so a ticket can never be registered under the wrong concern even
    when an earlier concern's slots are still in the session.

Tool design notes (per project constraint: NO separate mock/test API):
  - The connections point at Zepto's partner-support ticketing endpoint. The
    real endpoint is not available yet, so the URL uses the RESERVED
    ``.example`` TLD — DNS can never resolve, the call deterministically
    takes each workflow api node's ``failure`` edge (which speaks the
    approved script's own closing assurance), and no partner data can ever
    leak to an unintended host. Swap ``url`` (and add auth) when Zepto
    provides the real ticketing endpoint; nothing else changes.
  - The expected response contract AND a full sample response live as JSON in
    each connection's own ``responseSchema`` (``example`` key) — sample data
    stays inside the existing tool configuration, exactly as the Testing
    Studio's mockToolResults expects to replay it. The regression suite
    (zepto/tests/run_chat_scenarios.py) reads those examples back and
    replays them via /testing/simulate, so the success path is exercised
    with zero external services.
  - is_state_changing=True (idempotency + the state-changing guardrail gate
    apply — registering a ticket mutates the support queue).
    require_confirmation stays False: that flag gates on the
    customer_verified slot, which only verification lookups can set — this
    flow has no lookup step; the ticket is the mechanism that STARTS
    verification by the human team.
  - No slot placeholders in bodyTemplate: workflow api nodes send the full
    scalar slot state (issue_type plus the per-branch answers); the template
    pins the concern constants.

Run: env/bin/python zepto/setup/01_bot_entities_connections.py
"""

import json
import sys

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
TENANT = "tn_04250683f1b3"
BOT_NAME = "Zepto Support"
VOICE = "vp-sv-kavya"

STATE_FILE = __file__.rsplit("/", 1)[0] + "/zepto_config_state.json"

# Sample ticket responses kept as JSON inside each tool configuration
# (responseSchema "example") — the single source the tests replay as
# mockToolResults.
SAMPLES = {
    "mdnd": {
        "ticket_id": "ZPT-MDND-73412",
        "concern": "Mark Delivered but Not Delivered",
        "status": "registered",
        "callback_eta": "within 24 to 48 hours",
        "sms_sent": True,
    },
    "uniform_deduction": {
        "ticket_id": "ZPT-UNIF-51208",
        "concern": "Raincoat, T-shirt and Bag related deduction",
        "status": "registered",
        "callback_eta": "within 24 to 48 hours",
        "sms_sent": True,
    },
    "onboarding_fee": {
        "ticket_id": "ZPT-ONBF-66931",
        "concern": "Onboarding Fee related deduction",
        "status": "registered",
        "callback_eta": "within 24 to 48 hours",
        "sms_sent": True,
    },
    "rto": {
        "ticket_id": "ZPT-RTO-48057",
        "concern": "RTO issue",
        "status": "registered",
        "callback_eta": "within 24 to 48 hours",
        "sms_sent": True,
    },
}


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
    r = c.post("/auth/login", json={"email": "zepto.config@zepto.com",
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
            "useCase": "Delivery-partner payout deduction support (inbound)",
            "description": (
                "Inbound support bot for Zepto delivery partners. Handles "
                "four payout concerns from the approved support scripts: "
                "MDND (Mark Delivered but Not Delivered), Raincoat/T-shirt/"
                "Bag related deduction, Onboarding Fee related deduction, "
                "and RTO issues. Identifies the partner's concern (or "
                "accepts it directly from the opening utterance), collects "
                "exactly that concern's scripted enquiry answers, registers "
                "a support ticket, and assures a callback from the support "
                "team. Built from the four approved call-flow scripts in "
                "tenant/zepto/."),
            "languages": ["hi-IN", "en-IN"],
            "tenantId": TENANT,
        }), "create bot")
        state["BOT"] = bot["id"]
    save_state(state)

    check(c.patch(f"/bots/{state['BOT']}", json={"voiceId": VOICE}),
          f"bot voiceId -> {VOICE}")

    check(c.put(f"/bots/{state['BOT']}/voice-settings", json={
        "voiceId": VOICE,
        "speed": 1.0, "pauseMs": 250, "empathy": 60, "energy": 50,
        "languageVoiceMap": {
            "en-IN": {"provider": "sarvam", "model": "bulbul:v3", "voice": VOICE,
                      "params": {"temperature": 0.01, "min_buffer_size": 50,
                                 "max_chunk_length": 150,
                                 "send_completion_event": True}},
            "hi-IN": {"provider": "sarvam", "model": "bulbul:v3", "voice": VOICE,
                      "params": {"temperature": 0.01, "min_buffer_size": 50,
                                 "max_chunk_length": 150,
                                 "send_completion_event": True}},
            "default": "hi-IN",
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

# Tenant-level entity definitions document the data the bot collects and feed
# intent optionalEntities. The workflow's ask nodes carry their own inline
# entity configs (per-branch variable names prevent cross-concern slot reuse),
# so these definitions are the catalogue, not the extraction source.
ENTITIES = [
    {"name": "issue_type", "kind": "custom", "dataType": "text",
     "description": ("Which of the four supported payout concerns the "
                     "partner is calling about: mdnd, uniform_deduction, "
                     "onboarding_fee or rto."),
     "example": "mdnd"},
    {"name": "deduction_amount", "kind": "custom", "dataType": "text",
     "description": "Deduction amount the partner reports, as stated.",
     "example": "450 rupees"},
    {"name": "order_id_last4", "kind": "custom", "dataType": "number",
     "regexPattern": "[0-9]{4}",
     "description": ("Last 4 digits of the Order ID for order-linked "
                     "concerns (MDND, RTO)."),
     "example": "7842"},
    {"name": "deduction_date", "kind": "custom", "dataType": "text",
     "description": "Date or week the deduction was made, as stated.",
     "example": "last Tuesday"},
    {"name": "deduction_count", "kind": "custom", "dataType": "text",
     "description": "How many times the deduction has been made.",
     "example": "two times"},
    {"name": "date_of_joining", "kind": "custom", "dataType": "text",
     "description": "The partner's date of joining Zepto.",
     "example": "15 June"},
    {"name": "handover_recipient", "kind": "custom", "dataType": "text",
     "description": ("Who received the delivered product — customer, "
                     "security guard, or someone else (MDND)."),
     "example": "security guard"},
    {"name": "store_handover_date", "kind": "custom", "dataType": "text",
     "description": "When the partner handed the RTO product to the store team.",
     "example": "same evening"},
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


# ── API connections (one per concern — see module docstring) ─────────────────

RESPONSE_SCHEMA_PROPS = {
    "ticket_id": {"type": "string"},
    "concern": {"type": "string"},
    "status": {"type": "string"},
    "callback_eta": {"type": "string"},
    "sms_sent": {"type": "boolean"},
}
RESPONSE_MAPPING = [
    {"source": "ticket_id", "target": "ticket_id"},
    {"source": "concern", "target": "ticket_concern"},
    {"source": "status", "target": "ticket_status"},
    {"source": "callback_eta", "target": "callback_eta"},
    {"source": "sms_sent", "target": "sms_sent"},
]


def _connection(concern_code: str, name: str, label: str,
                request_props: dict) -> dict:
    return {
        "name": name,
        "description": (f"Registers a '{label}' payout concern ticket in "
                        "Zepto's partner-support system and triggers the "
                        "confirmation SMS to the partner's number. Real "
                        "ticketing endpoint pending — the reserved .example "
                        "host guarantees the workflow's failure edge (the "
                        "approved script's own closing assurance) until the "
                        "production URL and credentials are configured. The "
                        "response contract and the sample payload used by "
                        "Testing Studio live in responseSchema.example."),
        "method": "POST",
        "url": ("https://partner-support.zepto.example/api/v1/"
                f"deduction-concerns/{concern_code}"),
        "isStateChanging": True, "requireConfirmation": False,
        "timeoutMs": 6000, "retries": 1,
        "bodyTemplate": {
            "concern_code": concern_code,
            "concern_label": label,
            "channel": "voice_support_bot",
        },
        "requestSchema": {
            "type": "object",
            "properties": request_props,
            "required": [],
        },
        "responseSchema": {
            "type": "object",
            "properties": RESPONSE_SCHEMA_PROPS,
            "example": SAMPLES[concern_code],
        },
        "responseMapping": RESPONSE_MAPPING,
    }


CONNECTIONS = [
    _connection("mdnd", "Zepto Register MDND Concern",
                "Mark Delivered but Not Delivered", {
                    "m_issue_description": {"type": "string"},
                    "m_deduction_amount": {"type": "string"},
                    "m_order_last4": {"type": "string"},
                    "m_deduction_date": {"type": "string"},
                    "m_called_customer": {"type": "string"},
                    "m_reached_location": {"type": "string"},
                    "m_handover_recipient": {"type": "string"},
                    "m_cx_support_call": {"type": "string"},
                    "m_other_deduction_note": {"type": "string"},
                    "m_correction": {"type": "string"},
                }),
    _connection("uniform_deduction", "Zepto Register Uniform Deduction Concern",
                "Raincoat, T-shirt and Bag related deduction", {
                    "u_deduction_amount": {"type": "string"},
                    "u_deduction_count": {"type": "string"},
                    "u_items_received": {"type": "string"},
                    "u_deduction_date": {"type": "string"},
                }),
    _connection("onboarding_fee", "Zepto Register Onboarding Fee Concern",
                "Onboarding Fee related deduction", {
                    "o_date_of_joining": {"type": "string"},
                    "o_deduction_amount": {"type": "string"},
                    "o_deduction_date": {"type": "string"},
                    "o_paid_on_joining": {"type": "string"},
                }),
    _connection("rto", "Zepto Register RTO Concern",
                "RTO issue", {
                    "r_deduction_amount": {"type": "string"},
                    "r_order_last4": {"type": "string"},
                    "r_deduction_date": {"type": "string"},
                    "r_store_handover": {"type": "string"},
                    "r_store_handover_date": {"type": "string"},
                }),
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
