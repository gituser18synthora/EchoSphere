"""Stage 01 — Frankfinn bot, voice settings, entities and the booking tool.

Creates (idempotently, via REST as the tenant-admin service account):
  - bot "Frankfinn Seminar Booking" (hi-IN + en-IN, Hindi-first) under
    tn_6553beac240d — an OUTBOUND admissions bot that calls students who
    showed interest in aviation/hospitality/travel careers and books them
    into Frankfinn's free career-counselling seminar (source: Frankfinn/
    "Quality Call Flow_.docx" + call recording C44989190.wav, service
    FIVT_AHMEDABAD).
  - voice settings: Sarvam saaras:v3 STT (auto language detection),
    Sarvam bulbul:v3 TTS voice Priya for both languages (hi-IN default —
    the approved script is Hinglish), gpt-4o-mini orchestration with time
    context enabled.
  - tenant entity definitions used by the workflow's ask nodes.
  - ONE API connection, "Frankfinn Book Seminar Seat".

Tool design notes (per project constraint: NO separate mock service):
  - The connection points at Frankfinn's CRM appointment endpoint. The real
    endpoint is not available yet, so the URL uses the RESERVED ``.example``
    TLD — DNS can never resolve, the call deterministically takes the
    workflow's ``failure`` edge, and no student data can ever leak to an
    unintended host. Swap ``url`` (and add auth) when Frankfinn provides
    the real CRM endpoint; nothing else changes.
  - The expected response contract AND a full sample response live as JSON
    in the connection's own ``responseSchema`` (``example`` key) — sample
    data stays inside the existing tool configuration, exactly as the
    Testing Studio's mockToolResults expects to replay it. The regression
    suite (frankfinn/tests/run_chat_scenarios.py) reads that example back
    from the connection and feeds it to /testing/chat as mockToolResults,
    so the success path is exercised with zero external services.
  - is_state_changing=True (idempotency + the state-changing guardrail gate
    apply). require_confirmation stays False: that flag gates on the
    customer_verified slot, which only API lookups can set — this outbound
    flow confirms the callee conversationally at the opening and books a
    free, non-financial seminar seat, so there is no lookup step to gate on.
  - No bodyTemplate slot placeholders: workflow api nodes send the full
    scalar slot state (student_age, qualification, student_area, visit_day,
    parents_joining …); the template pins the campaign constants.

Run: env/bin/python frankfinn/setup/01_bot_entities_connections.py
"""

import json
import sys

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
TENANT = "tn_6553beac240d"
BOT_NAME = "Frankfinn Seminar Booking"
VOICE = "vp-sv-priya"

STATE_FILE = __file__.rsplit("/", 1)[0] + "/frankfinn_config_state.json"

# Sample response kept as JSON inside the tool configuration (responseSchema
# "example") — the single source the tests replay as mockToolResults.
BOOKING_SAMPLE_RESPONSE = {
    "appointment_number": "FRK-AHD-104217",
    "center_code": "FIVT_AHMEDABAD",
    "center_name": "Frankfinn Institute - Ahmedabad (C G Road) Centre",
    "center_address": ("3rd Floor, near Mocha Cafe, C G Road, "
                       "Ahmedabad"),
    "seminar_date": "tomorrow",
    "entry_window": "10:15 AM to 11:30 AM",
    "seminar_start_time": "11:40 AM",
    "duration_minutes": 45,
    "sms_sent": True,
    "sms_number_masked": "XXXXXX6337",
    "scholarship_note": ("Exclusive scholarship up to Rs 40,000 applicable "
                         "when parents attend, first come first serve"),
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
    r = c.post("/auth/login", json={"email": "frankfinn.config@frankfinn.com",
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
            "useCase": "Outbound seminar-seat booking (admissions)",
            "description": (
                "Outbound Hinglish admissions bot for Frankfinn Institute of "
                "Air Hostess Training. Calls students who showed interest in "
                "aviation, hospitality, travel and customer-service careers, "
                "checks eligibility (12th pass mandate, final-year probe for "
                "third-year graduation students), pitches the free 45-minute "
                "career counselling seminar at the Ahmedabad C G Road centre, "
                "books a seat, and confirms the appointment SMS, Aadhaar-card "
                "entry requirement and the 1800 258 7332 helpline. Built from "
                "Frankfinn's approved Quality Call Flow document and a "
                "reference call recording."),
            "languages": ["hi-IN", "en-IN"],
            "tenantId": TENANT,
        }), "create bot")
        state["BOT"] = bot["id"]
    save_state(state)

    check(c.patch(f"/bots/{state['BOT']}", json={"voiceId": VOICE}),
          f"bot voiceId -> {VOICE}")

    check(c.put(f"/bots/{state['BOT']}/voice-settings", json={
        "voiceId": VOICE,
        "speed": 1.0, "pauseMs": 250, "empathy": 55, "energy": 55,
        "languageVoiceMap": {
            "hi-IN": {"provider": "sarvam", "model": "bulbul:v3", "voice": VOICE,
                      "params": {"temperature": 0.01, "min_buffer_size": 50,
                                 "max_chunk_length": 150,
                                 "send_completion_event": True}},
            "en-IN": {"provider": "sarvam", "model": "bulbul:v3", "voice": VOICE,
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


ENTITIES = [
    {"name": "student_age", "kind": "custom", "dataType": "text",
     "description": "Student's stated age or date of birth (eligibility check).",
     "example": "22 saal", "pii": True},
    {"name": "qualification", "kind": "custom", "dataType": "text",
     "description": ("Student's highest education so far — 12th pass, pursuing "
                     "graduation (which year), or graduation complete."),
     "example": "B.Tech final year"},
    {"name": "student_area", "kind": "custom", "dataType": "text",
     "description": ("Locality the student lives in, to confirm the nearest "
                     "centre and travel convenience."),
     "example": "Maninagar, Ahmedabad"},
    {"name": "visit_day", "kind": "custom", "dataType": "text",
     "description": ("Day the student will visit the seminar when tomorrow "
                     "does not work for them."),
     "example": "Saturday"},
    {"name": "parents_joining", "kind": "custom", "dataType": "text",
     "description": ("Whether the student's parents will accompany them to "
                     "the seminar (parents unlock the scholarship offer)."),
     "example": "haan, papa aayenge"},
    {"name": "callback_time", "kind": "custom", "dataType": "text",
     "description": "Preferred time to call back when the student is busy.",
     "example": "shaam ko 6 baje"},
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


CONNECTIONS = [
    {
        "name": "Frankfinn Book Seminar Seat",
        "description": ("Books the student's seat for the free career "
                        "counselling seminar in Frankfinn's CRM and triggers "
                        "the confirmation SMS (appointment number, centre "
                        "address, date and timing) to the student's number. "
                        "Real CRM endpoint pending — the reserved .example "
                        "host guarantees the workflow's failure edge (spoken "
                        "graceful fallback) until the production URL and "
                        "credentials are configured. The response contract "
                        "and the sample payload used by Testing Studio live "
                        "in responseSchema.example."),
        "method": "POST",
        "url": "https://crm-integration.frankfinn.example/api/v1/seminar-appointments",
        "isStateChanging": True, "requireConfirmation": False,
        "timeoutMs": 6000, "retries": 1,
        "bodyTemplate": {
            "center_code": "FIVT_AHMEDABAD",
            "seminar_type": "free_career_counselling_seminar",
            "entry_window": "10:15-11:30",
            "seminar_start": "11:40",
            "duration_minutes": 45,
        },
        "requestSchema": {
            "type": "object",
            "properties": {
                "student_age": {"type": "string"},
                "qualification": {"type": "string"},
                "student_area": {"type": "string"},
                "visit_day": {"type": "string"},
                "parents_joining": {"type": "string"},
            },
            "required": [],
        },
        "responseSchema": {
            "type": "object",
            "properties": {
                "appointment_number": {"type": "string"},
                "center_code": {"type": "string"},
                "center_name": {"type": "string"},
                "center_address": {"type": "string"},
                "seminar_date": {"type": "string"},
                "entry_window": {"type": "string"},
                "seminar_start_time": {"type": "string"},
                "duration_minutes": {"type": "integer"},
                "sms_sent": {"type": "boolean"},
                "sms_number_masked": {"type": "string"},
                "scholarship_note": {"type": "string"},
            },
            "example": BOOKING_SAMPLE_RESPONSE,
        },
        "sensitiveMasks": ["student_age"],
        "responseMapping": [
            {"source": "appointment_number", "target": "appointment_number"},
            {"source": "center_name", "target": "center_name"},
            {"source": "center_address", "target": "center_address"},
            {"source": "seminar_date", "target": "seminar_date"},
            {"source": "entry_window", "target": "entry_window"},
            {"source": "seminar_start_time", "target": "seminar_start_time"},
            {"source": "sms_sent", "target": "sms_sent"},
            {"source": "sms_number_masked", "target": "sms_number_masked"},
            {"source": "scholarship_note", "target": "scholarship_note"},
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
