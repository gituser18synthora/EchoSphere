"""Development demo dataset — explicit opt-in via `python -m backend.cli seed --demo`.

Ports the previous frontend mock fixtures into MySQL + MongoDB so the app shows
the same development data, now database-driven. Idempotent: rows keep their
legacy IDs (tn-001, bot-101, …) and are inserted only when missing; existing
records are never modified or deleted.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone

from pymongo import MongoClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.config import get_settings
from shared.ids import new_id
from backend.core.security import hash_password
from shared.db.mysql import get_sessionmaker
from shared.models import (
    ApiConnection,
    BotLanguage,
    ChannelConfig,
    ConversationSession,
    EntityDef,
    Intent,
    Invoice,
    KnowledgeGap,
    KnowledgeSource,
    PhoneNumber,
    Plan,
    PlatformAlert,
    Prompt,
    PromptVersion,
    Release,
    Role,
    SipTrunk,
    Subscription,
    Tenant,
    TenantSetting,
    TestScenario,
    UsageRecord,
    User,
    VoiceBot,
    VoiceBotReadiness,
    VoiceBotSetting,
    Workflow,
)

logger = logging.getLogger("backend.demo_seed")

DEMO_PASSWORD = "Demo@2026!"

TENANTS = [
    # id, name, domain, industry, region, plan, status, health, admin_email, calls_month, mrr, ai_cost_month
    ("tn-001", "Meridian Health Group", "meridianhealth.com", "Healthcare", "US-East", "enterprise", "active", "good", "ops@meridianhealth.com", 48210, 12400, 3820),
    ("tn-002", "Northwind Insurance", "northwind.io", "Insurance", "US-West", "enterprise", "active", "warning", "admin@northwind.io", 36100, 9800, 2910),
    ("tn-003", "Velora Retail", "velora.shop", "Retail", "EU-Central", "growth", "active", "good", "it@velora.shop", 21050, 4200, 1470),
    ("tn-004", "Apex Logistics", "apexlogistics.com", "Logistics", "US-East", "growth", "active", "good", "support@apexlogistics.com", 15800, 3600, 1180),
    ("tn-005", "Banco Sol", "bancosol.mx", "Banking", "LATAM", "enterprise", "active", "serious", "ti@bancosol.mx", 29400, 8900, 2540),
    ("tn-006", "TalkFlow Telecom", "talkflow.net", "Telecom", "APAC", "growth", "trial", "good", "eval@talkflow.net", 4100, 0, 380),
    ("tn-007", "Cobalt Airlines", "cobaltair.com", "Travel", "EU-West", "enterprise", "active", "good", "digital@cobaltair.com", 33900, 10600, 3140),
    ("tn-008", "Quill & Co.", "quillco.com", "Legal", "US-East", "starter", "suspended", "critical", "office@quillco.com", 0, 490, 0),
    ("tn-009", "Grove Utilities", "groveutilities.com", "Utilities", "US-Central", "growth", "provisioning", "neutral", "admin@groveutilities.com", 0, 2400, 0),
]

USERS = [
    # id, email, name, role_code, tenant_id, status
    ("usr-demo-super", "alex.rivera@aurexion.com", "Alex Rivera", "super_admin", None, "active"),
    ("usr-demo-priya", "priya.sharma@meridianhealth.com", "Priya Sharma", "tenant_admin", "tn-001", "active"),
    ("usr-demo-marcus", "marcus.webb@meridianhealth.com", "Marcus Webb", "tenant_admin", "tn-001", "active"),
    ("usr-demo-dana", "dana.okafor@meridianhealth.com", "Dana Okafor", "tenant_admin", "tn-001", "active"),
    ("usr-demo-sam", "sam.ellery@meridianhealth.com", "Sam Ellery", "tenant_user", "tn-001", "active"),
    ("usr-demo-jordan", "jordan.liu@meridianhealth.com", "Jordan Liu", "tenant_user", "tn-001", "invited"),
]

READINESS = [
    ("r1", "Knowledge sources indexed", "knowledge"),
    ("r2", "Voice selected & tuned", "voice"),
    ("r3", "Core prompts approved", "prompts"),
    ("r4", "Intents validated", "intents"),
    ("r5", "Workflow published", "workflows"),
    ("r6", "Channel connected", "channels"),
    ("r7", "Regression suite passing", "testing"),
]

BOTS = [
    # id, name, use_case, description, langs, status, version, live, owner_uid, health, containment, cost, csat, voice, done_flags, calls_day
    ("bot-101", "Appointment Concierge", "Appointment booking", "Books, reschedules and cancels patient appointments across 14 clinics.", ["en-US", "es-US"], "published", "v2.4.1", "v2.4.1", "usr-demo-priya", "good", 78, 0.14, 4.5, "vp-02", [1, 1, 1, 1, 1, 1, 1], 612),
    ("bot-102", "Billing Helpdesk", "Billing support", "Answers billing questions, payment plans and insurance coverage checks.", ["en-US"], "published", "v1.9.0", "v1.8.2", "usr-demo-marcus", "warning", 64, 0.19, 4.1, "vp-05", [1, 1, 1, 0, 1, 1, 0], 289),
    ("bot-103", "Pharmacy Refill Line", "Prescription refills", "Automates refill requests and pickup notifications for pharmacy patients.", ["en-US", "es-US", "vi-VN"], "in_review", "v0.9.0", None, "usr-demo-priya", "good", 0, 0, 0, "vp-01", [1, 1, 1, 1, 1, 0, 1], 0),
    ("bot-104", "Lab Results Assistant", "Results & FAQs", "Securely shares lab result availability and answers preparation FAQs.", ["en-US"], "draft", "v0.3.2", None, "usr-demo-dana", "neutral", 0, 0, 0, None, [1, 0, 0, 0, 0, 0, 0], 0),
    ("bot-105", "After-Hours Triage", "Nurse triage routing", "Screens after-hours calls and routes urgent cases to the on-call nurse line.", ["en-US", "es-US"], "published", "v3.1.0", "v3.1.0", "usr-demo-marcus", "good", 71, 0.17, 4.4, "vp-03", [1, 1, 1, 1, 1, 1, 1], 148),
    ("bot-106", "Patient Feedback Survey", "Post-visit surveys", "Runs short post-visit CSAT surveys over WhatsApp and web chat.", ["en-US"], "rolled_back", "v1.2.0", "v1.1.3", "usr-demo-dana", "serious", 88, 0.05, 4.7, "vp-04", [1, 1, 1, 1, 1, 1, 0], 96),
]

KNOWLEDGE = [
    ("ks-01", "bot-101", "bot", "document", "Clinic Locations & Hours", "clinic-directory-2026.pdf", "indexed", 214, 1840, 96, 4820, 2),
    ("ks-02", "bot-101", "bot", "document", "Appointment Policy Handbook", "appt-policies-v4.docx", "indexed", 156, 920, 91, 3110, 5),
    ("ks-03", "bot-101", "bot", "url", "Insurance Providers Page", "meridianhealth.com/insurance", "stale", 48, 210, 62, 1890, 28),
    ("ks-04", "bot-101", "bot", "faq", "Top 60 Patient FAQs", "60 curated Q&A pairs", "indexed", 60, 84, 98, 6240, 3),
    ("ks-05", "bot-102", "bot", "document", "Billing Codes Reference", "billing-codes-2026.xlsx", "indexing", 0, 3400, 0, 0, 0),
    ("ks-06", "bot-102", "bot", "connector", "Zendesk Help Center", "Zendesk · 412 articles", "indexed", 1893, 12100, 88, 5470, 0),
    ("ks-07", None, "tenant", "document", "HIPAA Communication Guidelines", "hipaa-comms-guide.pdf", "indexed", 89, 640, 94, 2130, 13),
    ("ks-08", "bot-105", "bot", "document", "Triage Severity Protocols", "triage-protocols-v7.pdf", "failed", 0, 2210, 0, 0, 1),
    ("ks-09", "bot-103", "bot", "url", "Pharmacy Services", "meridianhealth.com/pharmacy", "indexed", 71, 260, 90, 0, 2),
    ("ks-10", None, "tenant", "faq", "Holiday Hours FAQ", "12 curated Q&A pairs", "pending", 0, 9, 0, 0, None),
]

GAPS = [
    ("kg-1", "Do you accept Aetna Medicare Advantage?", 142, "Insurance Providers Page (stale — re-sync)"),
    ("kg-2", "Can I get a same-day X-ray appointment?", 87, "Add imaging services doc"),
    ("kg-3", "What is the copay for a telehealth visit?", 63, "Billing Codes Reference (indexing)"),
    ("kg-4", "Is parking validated at the Oakwood clinic?", 31, "Add facility amenities FAQ"),
]

INTENTS = [
    ("in-01", "book_appointment", "Caller wants to schedule a new appointment", ["I need to see a doctor", "book me an appointment", "can I come in tomorrow", "schedule a visit with Dr. Reyes", "I want to make an appointment for my son"], 0.72, 0.91, "Booking workflow", ["date", "clinic", "provider"], "active", 7, 24, 24),
    ("in-02", "reschedule_appointment", "Caller wants to move an existing appointment", ["change my appointment", "move my visit to next week", "I can't make it Friday"], 0.72, 0.87, "Reschedule workflow", ["date", "appointment_id"], "active", 5, 18, 19),
    ("in-03", "cancel_appointment", "Caller wants to cancel", ["cancel my appointment", "I need to cancel Friday's visit"], 0.75, 0.93, "Cancel workflow", ["appointment_id"], "active", 4, 12, 12),
    ("in-04", "insurance_question", "Coverage and network questions", ["do you take Blue Cross", "is my insurance accepted"], 0.7, 0.66, "Knowledge answer", ["insurer"], "needs_samples", 3, 7, 11),
    ("in-05", "talk_to_human", "Explicit request for a person", ["let me talk to someone", "front desk please", "operator"], 0.6, 0.95, "Human handover", [], "active", 2, 9, 9),
    ("in-06", "clinic_hours", "Opening hours and locations", ["what time do you open", "are you open Saturday"], 0.7, 0.9, "Knowledge answer", ["clinic"], "active", 3, 10, 10),
]

ENTITIES = [
    ("en-01", "date", "system", "“next Tuesday at 3” → 2026-07-07T15:00", False, ["book_appointment", "reschedule_appointment"]),
    ("en-02", "clinic", "custom", "“Oakwood” → clinic_id 14", False, ["book_appointment", "clinic_hours"]),
    ("en-03", "provider", "custom", "“Dr. Reyes” → provider_id 88", False, ["book_appointment"]),
    ("en-04", "appointment_id", "regex", "“APT-58201” → 58201", False, ["reschedule_appointment", "cancel_appointment"]),
    ("en-05", "insurer", "custom", "“Blue Cross” → payer BCBS", False, ["insurance_question"]),
    ("en-06", "date_of_birth", "system", "“March 4th 1985” → 1985-03-04", True, ["identity_verification"]),
    ("en-07", "phone_number", "system", "“555 0142” → +15550142", True, ["identity_verification", "book_appointment"]),
]

APIS = [
    ("api-01", "bot-101", "EHR Slot Availability", "GET", "https://api.meridianhealth.com/ehr/v2/slots", "oauth2", "secret://tenants/tn-001/ehr-oauth", 4000, 2, [{"from": "$.slots[*].start", "to": "available_times"}, {"from": "$.slots[*].provider.name", "to": "provider_name"}], "healthy", 340, 6),
    ("api-02", "bot-101", "Create Appointment", "POST", "https://api.meridianhealth.com/ehr/v2/appointments", "oauth2", "secret://tenants/tn-001/ehr-oauth", 6000, 1, [{"from": "$.appointment.id", "to": "appointment_id"}, {"from": "$.appointment.confirmed_at", "to": "appointment_date"}], "healthy", 520, 4),
    ("api-03", "bot-101", "SMS Confirmation", "POST", "https://api.meridianhealth.com/notify/sms", "api_key", "secret://tenants/tn-001/notify-key", 3000, 3, [{"from": "$.message_id", "to": "sms_id"}], "degraded", 1840, 2),
    ("api-04", "bot-102", "Billing Balance Lookup", "GET", "https://api.meridianhealth.com/billing/v1/balance", "bearer", "secret://tenants/tn-001/billing-token", 4000, 2, [{"from": "$.balance.amount", "to": "balance_due"}], "failing", 0, 3),
    ("api-05", "bot-105", "On-call Roster", "GET", "https://api.meridianhealth.com/staff/oncall", "api_key", "secret://tenants/tn-001/staff-key", 2500, 2, [{"from": "$.oncall.phone", "to": "oncall_number"}], "healthy", 180, 1),
]

WORKFLOW_NODES = [
    {"id": "n1", "kind": "start", "label": "Call starts", "sub": "Voice · WhatsApp", "x": 40, "y": 40},
    {"id": "n2", "kind": "message", "label": "Welcome greeting", "sub": "Prompt v4", "x": 40, "y": 150},
    {"id": "n3", "kind": "intent", "label": "Detect intent", "sub": "6 intents", "x": 40, "y": 260},
    {"id": "n4", "kind": "api", "label": "EHR Slot Availability", "sub": "GET · 340ms p50", "x": 252, "y": 190},
    {"id": "n5", "kind": "condition", "label": "Slots found?", "sub": "available_times > 0", "x": 252, "y": 310},
    {"id": "n6", "kind": "api", "label": "Create Appointment", "sub": "POST", "x": 462, "y": 250},
    {"id": "n7", "kind": "message", "label": "Confirm & recap", "sub": "Prompt v3", "x": 462, "y": 370},
    {"id": "n8", "kind": "knowledge", "label": "Answer from knowledge", "sub": "4 sources", "x": 252, "y": 60},
    {"id": "n9", "kind": "handover", "label": "Front desk handover", "sub": "Queue: reception", "x": 462, "y": 60},
    {"id": "n10", "kind": "end", "label": "End call", "sub": "Survey via SMS", "x": 462, "y": 480},
]
WORKFLOW_EDGES = [
    {"id": "e1", "from": "n1", "to": "n2"},
    {"id": "e2", "from": "n2", "to": "n3"},
    {"id": "e3", "from": "n3", "to": "n4", "label": "book / reschedule"},
    {"id": "e4", "from": "n3", "to": "n8", "label": "FAQ"},
    {"id": "e5", "from": "n4", "to": "n5"},
    {"id": "e6", "from": "n5", "to": "n6", "label": "yes"},
    {"id": "e7", "from": "n5", "to": "n9", "label": "no slots"},
    {"id": "e8", "from": "n6", "to": "n7"},
    {"id": "e9", "from": "n8", "to": "n9", "label": "low confidence"},
    {"id": "e10", "from": "n7", "to": "n10"},
]
WORKFLOW_ISSUES = [
    {"nodeId": "n9", "level": "warning", "message": "No after-hours fallback configured for handover when reception queue is closed."},
    {"nodeId": "n8", "level": "warning", "message": "Knowledge source “Insurance Providers Page” is stale (28 days)."},
]

CHANNELS = [
    ("ch-101-voice", "bot-101", "voice", "live", "+1 (415) 555-0119 · 4 lines", "Booking journey v12", {"at": "2026-07-02T09:00:00Z", "ok": True, "message": "Test call completed · 3.2s connect"}),
    ("ch-101-wa", "bot-101", "whatsapp", "live", "Business acct · meridian-health", "Booking journey v12", {"at": "2026-07-01T14:00:00Z", "ok": True, "message": "Template messages verified"}),
    ("ch-101-web", "bot-101", "web", "testing", "widget key wgt_…f24e", "Booking journey v12", {"at": "2026-07-03T08:30:00Z", "ok": False, "message": "CORS origin missing for portal.meridianhealth.com"}),
    ("ch-102-voice", "bot-102", "voice", "live", "+1 (415) 555-0184 · 2 lines", "Billing journey v8", None),
    ("ch-105-voice", "bot-105", "voice", "live", "+1 (415) 555-0161 · 2 lines", "Triage journey v5", None),
    ("ch-106-wa", "bot-106", "whatsapp", "live", "Business acct · meridian-health", "Survey journey v3", None),
    ("ch-106-web", "bot-106", "web", "live", "widget key wgt_…a91c", "Survey journey v3", None),
]

SCENARIOS = [
    ("ts-01", "Happy path — new booking (EN)", "Booking", 9, True, None, None),
    ("ts-02", "Happy path — new booking (ES)", "Booking", 9, True, None, None),
    ("ts-03", "Reschedule with appointment ID", "Booking", 7, True, None, None),
    ("ts-04", "No slots available → handover", "Edge cases", 6, False, 5, "Expected handover message, got fallback prompt (low intent confidence 0.58)"),
    ("ts-05", "Insurance question from knowledge", "Knowledge", 4, False, 3, "Retrieved chunk from stale source; answer outdated"),
    ("ts-06", "Explicit human request", "Edge cases", 3, True, None, None),
    ("ts-07", "Caller interrupts mid-sentence", "Voice UX", 5, True, None, None),
    ("ts-08", "Background noise / low ASR", "Voice UX", 6, None, None, None),
]

PHONE_NUMBERS = [
    ("pn-01", "+1 (415) 555-0119", "US", "tn-001", "bot-101", "Twilio", "assigned", 1.15),
    ("pn-02", "+1 (415) 555-0184", "US", "tn-001", "bot-102", "Twilio", "assigned", 1.15),
    ("pn-03", "+44 20 7946 0958", "GB", "tn-007", None, "Voxbone", "assigned", 2.4),
    ("pn-04", "+52 55 4170 8821", "MX", "tn-005", None, "Telnyx", "assigned", 3.1),
    ("pn-05", "+1 (628) 555-0022", "US", None, None, "Twilio", "available", 1.15),
    ("pn-06", "+49 30 901820", "DE", "tn-003", None, "Voxbone", "porting", 2.2),
]

SIP_TRUNKS = [
    ("st-01", "Twilio trunk US-East-1", "Twilio", "US-East", 240, 118, 0.2, "healthy"),
    ("st-02", "Voxbone trunk EU-West-2", "Voxbone", "EU-West", 120, 64, 8.2, "degraded"),
    ("st-03", "Telnyx trunk LATAM-1", "Telnyx", "LATAM", 80, 22, 0.4, "healthy"),
]

ALERTS = [
    ("al-01", "critical", "Telephony trunk EU-West-2 degraded — 8.2% call failures", "SIP · Voxbone trunk 3", "telephony", None, "open", 2),
    ("al-02", "serious", "Banco Sol: containment dropped 14pts after v3.0 publish", "Tenant tn-005 · anomaly detector", "tenant", "tn-005", "acknowledged", 3),
    ("al-03", "warning", "Embedding queue backlog above 10 min (14,220 chunks)", "Knowledge pipeline", "ai", None, "open", 5),
    ("al-04", "warning", "Northwind Insurance approaching plan minute limit (92%)", "Usage metering", "tenant", "tn-002", "open", 14),
    ("al-05", "good", "STT provider latency recovered to p95 280ms", "AI health monitor", "ai", None, "resolved", 17),
]

TRANSCRIPT_BOOKING = [
    {"turn": 1, "speaker": "bot", "text": "Hi, thanks for calling Meridian Health. I can help you book, change or cancel an appointment. How can I help today?", "promptVersion": "greeting v4", "latencyMs": 480, "costUsd": 0.004},
    {"turn": 2, "speaker": "user", "text": "Yeah hi, I need to see Dr. Reyes sometime this week if possible."},
    {"turn": 3, "speaker": "bot", "text": "Sure — let me check Dr. Reyes' availability this week.", "intent": "book_appointment", "confidence": 0.94, "apiCalls": [{"name": "EHR Slot Availability", "ms": 356, "ok": True}], "latencyMs": 820, "costUsd": 0.006},
    {"turn": 4, "speaker": "bot", "text": "Dr. Reyes has Thursday at 10:15 AM or Friday at 2:40 PM at the Oakwood clinic. Would either of those work?", "chunksUsed": ["Clinic Locations & Hours §3"], "latencyMs": 510, "costUsd": 0.005},
    {"turn": 5, "speaker": "user", "text": "Thursday morning works great."},
    {"turn": 6, "speaker": "bot", "text": "Perfect. Can I confirm your date of birth for verification?", "intent": "book_appointment", "confidence": 0.9, "latencyMs": 430, "costUsd": 0.004},
    {"turn": 7, "speaker": "user", "text": "March 4th, 1985."},
    {"turn": 8, "speaker": "bot", "text": "Thanks. You're all set for Thursday at 10:15 AM with Dr. Reyes at Oakwood. You'll get a text confirmation shortly. Anything else?", "apiCalls": [{"name": "Create Appointment", "ms": 540, "ok": True}, {"name": "SMS Confirmation", "ms": 1620, "ok": True}], "promptVersion": "closing v3", "latencyMs": 940, "costUsd": 0.008},
    {"turn": 9, "speaker": "user", "text": "No that's it, thank you!"},
]

TRANSCRIPT_ESCALATION = [
    {"turn": 1, "speaker": "bot", "text": "Hi, thanks for calling Meridian Health billing support. How can I help?", "promptVersion": "greeting v2", "latencyMs": 460, "costUsd": 0.004},
    {"turn": 2, "speaker": "user", "text": "I got charged twice for my last visit and I'm pretty upset about it."},
    {"turn": 3, "speaker": "bot", "text": "I'm sorry about that — let me pull up your balance.", "intent": "billing_dispute", "confidence": 0.81, "apiCalls": [{"name": "Billing Balance Lookup", "ms": 4000, "ok": False}], "latencyMs": 4620, "costUsd": 0.007},
    {"turn": 4, "speaker": "bot", "text": "I'm having trouble reaching the billing system right now. Let me connect you with a billing specialist.", "promptVersion": "escalation v5", "latencyMs": 520, "costUsd": 0.004},
    {"turn": 5, "speaker": "user", "text": "Fine, please hurry."},
]

CONVERSATIONS = [
    # id, bot, channel, caller, minutes_ago, dur, sentiment, intents, contained, esc_reason, csat, cost, lang, qa, flagged, transcript_key
    ("cv-9001", "bot-101", "voice", "+1 •••-0184", 78, 154, "positive", ["book_appointment"], True, None, 5, 0.14, "en-US", 96, False, "booking"),
    ("cv-9002", "bot-102", "voice", "+1 •••-3327", 89, 208, "negative", ["billing_dispute"], False, "API failure — Billing Balance Lookup timeout", 2, 0.21, "en-US", 61, True, "escalation"),
    ("cv-9003", "bot-101", "whatsapp", "+1 •••-8850", 102, 96, "neutral", ["reschedule_appointment"], True, None, 4, 0.06, "es-US", 88, False, "booking6"),
    ("cv-9004", "bot-105", "voice", "+1 •••-2211", 478, 312, "negative", ["urgent_symptoms", "talk_to_human"], False, "Urgency rule — routed to on-call nurse", 4, 0.24, "en-US", 92, False, "escalation4"),
    ("cv-9005", "bot-101", "voice", "+1 •••-6402", 125, 187, "neutral", ["insurance_question", "book_appointment"], True, None, 3, 0.16, "en-US", 74, True, "booking8"),
    ("cv-9006", "bot-106", "whatsapp", "+1 •••-1177", 140, 64, "positive", ["survey_response"], True, None, 5, 0.03, "en-US", 98, False, "booking4"),
    ("cv-9007", "bot-101", "voice", "+1 •••-9034", 158, 243, "negative", ["insurance_question"], False, "Low intent confidence (0.58) after 2 fallbacks", 2, 0.2, "en-US", 58, True, "escalation"),
    ("cv-9008", "bot-105", "voice", "+1 •••-4415", 552, 126, "neutral", ["clinic_hours"], True, None, 4, 0.11, "es-US", 90, False, "booking5"),
]

TRANSCRIPTS = {
    "booking": TRANSCRIPT_BOOKING,
    "booking4": TRANSCRIPT_BOOKING[:4],
    "booking5": TRANSCRIPT_BOOKING[:5],
    "booking6": TRANSCRIPT_BOOKING[:6],
    "booking8": TRANSCRIPT_BOOKING[:8],
    "escalation": TRANSCRIPT_ESCALATION,
    "escalation4": TRANSCRIPT_ESCALATION[:4],
}


def _lcg(seed: int):
    s = seed & 0xFFFFFFFF

    def rand() -> float:
        nonlocal s
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        return s / 4294967296

    return rand


def _series(seed: int, n: int, base: float, spread: float, trend: float = 0.0) -> list[float]:
    r = _lcg(seed)
    return [max(0.0, base + trend * i + (r() - 0.5) * spread) for i in range(n)]


def run_demo_seed(db: Session | None = None, days: int = 90) -> dict:
    own = db is None
    if own:
        db = get_sessionmaker()()
    created: dict[str, int] = {}

    def bump(key: str):
        created[key] = created.get(key, 0) + 1

    try:
        roles = {r.code: r for r in db.scalars(select(Role)).all()}
        plans = {p.code: p for p in db.scalars(select(Plan)).all()}
        if not roles or not plans:
            raise RuntimeError("Base seed must run before the demo seed.")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        today = date.today()

        # Tenants + subscriptions + invoices + settings
        for (tid, name, domain, industry, region, plan_code, status, health,
             admin_email, calls_month, mrr, ai_cost) in TENANTS:
            if db.get(Tenant, tid) is None:
                db.add(Tenant(
                    id=tid, name=name, domain=domain, industry=industry, region=region,
                    status=status, health=health, admin_email=admin_email,
                ))
                db.flush()  # tenant row must exist before its subscription/settings
                bump("tenants")
            if db.scalar(select(Subscription).where(Subscription.tenant_id == tid)) is None and status != "provisioning":
                plan = plans[plan_code]
                db.add(Subscription(
                    id=f"sub-{tid}", tenant_id=tid, plan_id=plan.id,
                    seats=plan.seats_included, bot_limit=plan.bot_limit,
                    minutes_included=plan.minutes_included,
                    renews_at=date(today.year, today.month, 1) + timedelta(days=32),
                    status="past_due" if status == "suspended" else ("trial" if status == "trial" else "active"),
                    mrr=mrr,
                ))
                bump("subscriptions")
            if db.scalar(select(TenantSetting).where(TenantSetting.tenant_id == tid)) is None:
                db.add(TenantSetting(
                    id=f"tset-{tid}", tenant_id=tid, display_name=name,
                    timezone="America/New_York" if region and region.startswith("US") else "UTC",
                    default_languages=["en-US"],
                ))

        db.flush()  # tenants must exist before dependent rows

        inv_month = (today.replace(day=1) - timedelta(days=1))
        for i, (tid, amount, status) in enumerate([
            ("tn-001", 12400, "paid"), ("tn-002", 11240, "paid"), ("tn-005", 9320, "open"),
            ("tn-007", 10600, "paid"), ("tn-008", 490, "past_due"), ("tn-003", 4200, "paid"),
        ]):
            inv_id = f"INV-{inv_month.year}-{inv_month.month:02d}{11 + i}"
            if db.get(Invoice, inv_id) is None:
                db.add(Invoice(
                    id=inv_id, tenant_id=tid, period=inv_month.strftime("%b %Y"),
                    amount=amount, status=status, issued_at=inv_month.replace(day=1),
                ))
                bump("invoices")

        # Users
        for uid, email, name, role_code, tid, status in USERS:
            if db.get(User, uid) is None and db.scalar(select(User).where(User.email == email)) is None:
                db.add(User(
                    id=uid, email=email, name=name,
                    password_hash=hash_password(DEMO_PASSWORD),
                    role_id=roles[role_code].id, tenant_id=tid, status=status,
                ))
                bump("users")

        db.flush()  # users must exist before bots reference owner_user_id

        # Bots
        for (bid, name, use_case, desc, langs, status, version, live, owner,
             health, containment, cost, csat, voice, done, calls_day) in BOTS:
            if db.get(VoiceBot, bid) is None:
                db.add(VoiceBot(
                    id=bid, tenant_id="tn-001", name=name, use_case=use_case,
                    description=desc, status=status, version=version, live_version=live,
                    owner_user_id=owner, health=health, containment=containment,
                    avg_cost_per_call=cost, csat=csat, voice_id=voice,
                    published_at=now - timedelta(days=9) if live else None,
                ))
                for j, (key, label, tab) in enumerate(READINESS):
                    db.add(VoiceBotReadiness(
                        id=f"rd-{bid}-{key}", bot_id=bid, item_key=key, label=label,
                        done=bool(done[j]), studio_tab=tab, sort_order=j,
                    ))
                for code in langs:
                    db.add(BotLanguage(bot_id=bid, language_code=code))
                bump("bots")

        db.flush()  # bots must exist before dependent rows

        if db.scalar(select(VoiceBotSetting).where(VoiceBotSetting.bot_id == "bot-101")) is None:
            db.add(VoiceBotSetting(
                id="vbs-101", bot_id="bot-101", tenant_id="tn-001", voice_id="vp-02",
                speed=1.0, pause_ms=350, empathy=65, energy=45,
                language_voice_map={"en-US": "vp-02", "es-US": "vp-08"},
            ))

        # Knowledge
        for ksid, bot_id, scope, ktype, name, detail, status, chunks, size_kb, quality, usage, sync_days in KNOWLEDGE:
            if db.get(KnowledgeSource, ksid) is None:
                db.add(KnowledgeSource(
                    id=ksid, tenant_id="tn-001", bot_id=bot_id, scope=scope, type=ktype,
                    name=name, detail=detail, status=status, chunks=chunks,
                    size_kb=size_kb, quality=quality, usage_30d=usage,
                    last_sync_at=(now - timedelta(days=sync_days)) if sync_days is not None else None,
                ))
                bump("knowledge")

        for gid, question, freq, suggestion in GAPS:
            if db.get(KnowledgeGap, gid) is None:
                db.add(KnowledgeGap(
                    id=gid, tenant_id="tn-001", bot_id="bot-101", question=question,
                    frequency=freq, last_asked_at=now - timedelta(hours=6),
                    suggested_source=suggestion,
                ))
                bump("gaps")

        # Prompts + versions
        prompts_data = [
            ("pr-01", "greeting", "Welcome greeting", ["{caller_name}", "{clinic_name}"], "approved", 4, [
                (4, "usr-demo-priya", "Priya Sharma", 21, "Warmer tone, mention Spanish option", [
                    {"language": "en-US", "content": "Hi {caller_name}, thanks for calling {clinic_name}. I can help you book, change or cancel an appointment. You can also say “Spanish” at any time. How can I help today?"},
                    {"language": "es-US", "content": "Hola {caller_name}, gracias por llamar a {clinic_name}. Puedo ayudarle a reservar, cambiar o cancelar una cita. ¿Cómo puedo ayudarle hoy?"},
                ]),
                (3, "usr-demo-marcus", "Marcus Webb", 44, "Shortened opener", [
                    {"language": "en-US", "content": "Thanks for calling {clinic_name}. I can help with appointments. How can I help?"},
                    {"language": "es-US", "content": "Gracias por llamar a {clinic_name}. Puedo ayudarle con citas. ¿Cómo puedo ayudarle?"},
                ]),
            ]),
            ("pr-02", "fallback", "Low-confidence fallback", [], "approved", 2, [
                (2, "usr-demo-priya", "Priya Sharma", 33, "Offer menu of options", [
                    {"language": "en-US", "content": "Sorry, I didn’t quite catch that. You can say things like “book an appointment”, “reschedule”, or “talk to the front desk”."},
                    {"language": "es-US", "content": "Perdón, no le entendí bien. Puede decir “reservar una cita”, “cambiar mi cita” o “hablar con recepción”."},
                ]),
            ]),
            ("pr-03", "escalation", "Handover to front desk", ["{queue_wait}"], "pending_approval", 5, [
                (6, "usr-demo-dana", "Dana Okafor", 12, "Adds live wait-time variable — awaiting approval", [
                    {"language": "en-US", "content": "No problem — I’ll connect you with the front desk. The current wait is about {queue_wait}. Please stay on the line."},
                    {"language": "es-US", "content": "Con gusto le comunico con recepción. La espera actual es de {queue_wait}. Por favor, no cuelgue."},
                ]),
                (5, "usr-demo-priya", "Priya Sharma", 42, "Approved baseline", [
                    {"language": "en-US", "content": "No problem — I’ll connect you with the front desk now. Please stay on the line."},
                    {"language": "es-US", "content": "Con gusto le comunico con recepción. Por favor, no cuelgue."},
                ]),
            ]),
            ("pr-04", "closing", "Call wrap-up", ["{appointment_date}"], "approved", 3, [
                (3, "usr-demo-priya", "Priya Sharma", 25, "Confirmation recap", [
                    {"language": "en-US", "content": "You’re all set for {appointment_date}. You’ll get a text confirmation shortly. Anything else I can help with?"},
                    {"language": "es-US", "content": "Su cita quedó para {appointment_date}. Recibirá una confirmación por mensaje de texto. ¿Algo más en que pueda ayudarle?"},
                ]),
            ]),
            ("pr-05", "hold", "Lookup hold message", [], "draft", 1, [
                (1, "usr-demo-dana", "Dana Okafor", 12, "New draft", [
                    {"language": "en-US", "content": "One moment while I check the schedule for you…"},
                ]),
            ]),
        ]
        for pid, ptype, name, variables, state, active, versions in prompts_data:
            if db.get(Prompt, pid) is None:
                db.add(Prompt(
                    id=pid, tenant_id="tn-001", bot_id="bot-101", type=ptype, name=name,
                    variables=variables, state=state, active_version=active,
                ))
                for ver, uid, uname, days_ago, note, variants in versions:
                    db.add(PromptVersion(
                        id=f"prv-{pid}-{ver}", prompt_id=pid, version=ver,
                        edited_by=uname, edited_by_user_id=uid,
                        edited_at=now - timedelta(days=days_ago), note=note, variants=variants,
                    ))
                bump("prompts")

        # Intents / entities
        for iid, name, desc, samples, threshold, avg_conf, route, ents, status, ver, tp, tt in INTENTS:
            if db.get(Intent, iid) is None:
                db.add(Intent(
                    id=iid, tenant_id="tn-001", bot_id="bot-101", name=name,
                    description=desc, samples=samples, confidence_threshold=threshold,
                    avg_confidence_30d=avg_conf, route=route, entities=ents,
                    status=status, version=ver, test_pass=tp, test_total=tt,
                ))
                bump("intents")
        for eid, name, kind, example, pii, used_by in ENTITIES:
            if db.get(EntityDef, eid) is None:
                db.add(EntityDef(
                    id=eid, tenant_id="tn-001", name=name, kind=kind, example=example,
                    pii=pii, used_by=used_by,
                ))
                bump("entities")

        # API connections
        for aid, bot_id, name, method, url, auth, secret, timeout, retries, mapping, status, latency, ver in APIS:
            if db.get(ApiConnection, aid) is None:
                db.add(ApiConnection(
                    id=aid, tenant_id="tn-001", bot_id=bot_id, name=name, method=method,
                    url=url, auth_type=auth, secret_ref=secret, timeout_ms=timeout,
                    retries=retries, response_mapping=mapping, status=status,
                    last_tested_at=now - timedelta(hours=4), last_latency_ms=latency,
                    version=ver,
                ))
                bump("apis")

        # Workflows (bot-101 rich; simple defaults for the rest)
        if db.get(Workflow, "wf-01") is None:
            db.add(Workflow(
                id="wf-01", tenant_id="tn-001", bot_id="bot-101", name="Booking journey",
                version=12, status="approved", nodes=WORKFLOW_NODES, edges=WORKFLOW_EDGES,
                issues=WORKFLOW_ISSUES, updated_by="usr-demo-priya",
            ))
            bump("workflows")
        for bid, wname, ver in [
            ("bot-102", "Billing journey", 8), ("bot-103", "Refill journey", 4),
            ("bot-104", "Results journey", 1), ("bot-105", "Triage journey", 5),
            ("bot-106", "Survey journey", 3),
        ]:
            wid = f"wf-{bid}"
            if db.get(Workflow, wid) is None:
                db.add(Workflow(
                    id=wid, tenant_id="tn-001", bot_id=bid, name=wname, version=ver,
                    status="approved" if ver > 1 else "draft",
                    nodes=[
                        {"id": "n1", "kind": "start", "label": "Call starts", "x": 40, "y": 40},
                        {"id": "n2", "kind": "intent", "label": "Detect intent", "x": 40, "y": 150},
                        {"id": "n3", "kind": "knowledge", "label": "Answer from knowledge", "x": 252, "y": 90},
                        {"id": "n4", "kind": "handover", "label": "Handover", "x": 252, "y": 210},
                        {"id": "n5", "kind": "end", "label": "End call", "x": 462, "y": 150},
                    ],
                    edges=[
                        {"id": "e1", "from": "n1", "to": "n2"},
                        {"id": "e2", "from": "n2", "to": "n3", "label": "FAQ"},
                        {"id": "e3", "from": "n2", "to": "n4", "label": "human"},
                        {"id": "e4", "from": "n3", "to": "n5"},
                        {"id": "e5", "from": "n4", "to": "n5"},
                    ],
                    issues=[],
                    updated_by="usr-demo-priya",
                ))
                bump("workflows")

        # Channels
        for cid, bot_id, ctype, status, detail, wname, last_test in CHANNELS:
            if db.get(ChannelConfig, cid) is None:
                db.add(ChannelConfig(
                    id=cid, tenant_id="tn-001", bot_id=bot_id, type=ctype, status=status,
                    detail=detail, workflow_name=wname, last_test=last_test,
                ))
                bump("channels")

        # Scenarios
        run_at = (now - timedelta(hours=5)).isoformat() + "Z"
        for sid, name, suite, steps, passed, failed_step, reason in SCENARIOS:
            if db.get(TestScenario, sid) is None:
                last_run = None
                if passed is not None:
                    last_run = {"at": run_at, "pass": passed}
                    if not passed:
                        last_run["failedStep"] = failed_step
                        last_run["reason"] = reason
                db.add(TestScenario(
                    id=sid, tenant_id="tn-001", bot_id="bot-101", name=name,
                    suite=suite, steps=steps, last_run=last_run,
                ))
                bump("scenarios")

        # Releases
        releases = [
            ("rel-06", "v2.5.0", "review", "Adds live wait-time to escalation prompt; re-synced insurance knowledge; 2 new intent samples.", "Dana Okafor", None, None, [
                {"id": "c1", "label": "All regression tests passing", "ok": False, "detail": "2 of 8 scenarios failing"},
                {"id": "c2", "label": "Prompts approved", "ok": False, "detail": "Escalation prompt v6 pending approval"},
                {"id": "c3", "label": "Knowledge sources fresh (<14 days)", "ok": False, "detail": "1 stale source"},
                {"id": "c4", "label": "Workflow validation clean", "ok": False, "detail": "2 warnings"},
                {"id": "c5", "label": "Channels tested", "ok": False, "detail": "Web widget test failing"},
                {"id": "c6", "label": "No unresolved critical alerts", "ok": True},
            ], [
                {"area": "Prompts", "change": "Escalation prompt v5 → v6 (adds {queue_wait})", "kind": "changed"},
                {"area": "Knowledge", "change": "Insurance Providers Page re-sync scheduled", "kind": "changed"},
                {"area": "Intents", "change": "insurance_question +2 samples", "kind": "added"},
            ]),
            ("rel-05", "v2.4.1", "published", "Hotfix: SMS confirmation retries raised to 3.", "Priya Sharma", "Marcus Webb", 9, [
                {"id": "c1", "label": "All regression tests passing", "ok": True},
                {"id": "c2", "label": "Prompts approved", "ok": True},
                {"id": "c3", "label": "Knowledge sources fresh (<14 days)", "ok": True},
                {"id": "c4", "label": "Workflow validation clean", "ok": True},
                {"id": "c5", "label": "Channels tested", "ok": True},
                {"id": "c6", "label": "No unresolved critical alerts", "ok": True},
            ], [{"area": "APIs", "change": "SMS Confirmation retries 1 → 3", "kind": "changed"}]),
            ("rel-04", "v2.4.0", "published", "Spanish language variant for all prompts; Elena voice mapping for es-US.", "Priya Sharma", "Marcus Webb", 18, [], [
                {"area": "Prompts", "change": "es-US variants added to 5 prompts", "kind": "added"},
                {"area": "Voice", "change": "es-US → Elena mapping", "kind": "added"},
            ]),
            ("rel-03", "v2.3.2", "rolled_back", "Aggressive barge-in tuning caused callers to be cut off. Rolled back 4h after publish.", "Marcus Webb", "Priya Sharma", 25, [], [
                {"area": "Voice", "change": "Barge-in sensitivity high → default", "kind": "changed"},
            ]),
        ]
        for rid, version, stage, notes, req_by, appr_by, pub_days, checklist, diff in releases:
            if db.get(Release, rid) is None:
                db.add(Release(
                    id=rid, tenant_id="tn-001", bot_id="bot-101", version=version,
                    stage=stage, notes=notes, requested_by=req_by, approved_by=appr_by,
                    published_at=(now - timedelta(days=pub_days)) if pub_days else None,
                    checklist=checklist, diff=diff,
                ))
                bump("releases")

        # Phone numbers / SIP trunks / alerts
        for pid, number, country, tid, bid, provider, status, cost in PHONE_NUMBERS:
            if db.get(PhoneNumber, pid) is None:
                db.add(PhoneNumber(
                    id=pid, number=number, country=country, tenant_id=tid, bot_id=bid,
                    provider=provider, status=status, monthly_cost=cost,
                ))
                bump("numbers")
        for stid, name, provider, region, capacity, active, failure, status in SIP_TRUNKS:
            if db.get(SipTrunk, stid) is None:
                db.add(SipTrunk(
                    id=stid, name=name, provider=provider, region=region,
                    capacity_lines=capacity, active_calls=active, failure_pct=failure,
                    status=status,
                ))
                bump("trunks")
        for aid, severity, title, source, scope, tid, status, hours_ago in ALERTS:
            if db.get(PlatformAlert, aid) is None:
                db.add(PlatformAlert(
                    id=aid, severity=severity, title=title, source=source, scope=scope,
                    tenant_id=tid, status=status, occurred_at=now - timedelta(hours=hours_ago),
                ))
                bump("alerts")

        # Conversations (MySQL metadata) + transcripts (MongoDB documents)
        settings = get_settings()
        mongo = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=4000)
        transcripts = mongo[settings.mongodb_database]["conversation_transcripts"]
        for (cid, bot_id, channel, caller, mins_ago, dur, sentiment, intents,
             contained, esc, csat, cost, lang, qa, flagged, tkey) in CONVERSATIONS:
            if db.get(ConversationSession, cid) is None:
                started = now - timedelta(minutes=mins_ago)
                db.add(ConversationSession(
                    id=cid, tenant_id="tn-001", bot_id=bot_id, channel=channel,
                    caller_masked=caller, started_at=started, duration_sec=dur,
                    sentiment=sentiment, intents=intents, contained=contained,
                    escalation_reason=esc, csat=csat, cost_usd=cost, language=lang,
                    qa_score=qa, flagged=flagged,
                ))
                transcripts.update_one(
                    {"session_id": cid},
                    {"$set": {
                        "session_id": cid, "tenant_id": "tn-001", "bot_id": bot_id,
                        "user_id": None, "status": "completed",
                        "turns": TRANSCRIPTS[tkey],
                        "metadata": {"channel": channel, "language": lang},
                        "updated_at": now,
                    }, "$setOnInsert": {"created_at": started}},
                    upsert=True,
                )
                bump("conversations")
        mongo.close()

        # Usage history (deterministic) — tenant rollups + per-bot rows for tn-001
        dates = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
        if not db.scalar(select(UsageRecord).where(UsageRecord.tenant_id == "tn-001").limit(1)):
            for t_idx, (tid, _, _, _, _, _, status, _, _, calls_month, _, ai_cost) in enumerate(TENANTS):
                if calls_month <= 0:
                    continue
                base_calls = calls_month / 30
                base_ai = ai_cost / 30
                calls_s = _series(21 + t_idx, days, base_calls, base_calls * 0.35, base_calls * 0.004)
                rate_s = _series(22 + t_idx, days, 74, 8, 0.02)
                csat_s = _series(23 + t_idx, days, 4.4, 0.4)
                for i, d in enumerate(dates):
                    calls = round(calls_s[i])
                    contained = round(calls * min(rate_s[i], 98) / 100)
                    db.add(UsageRecord(
                        id=f"ur-{tid}-{d.isoformat()}", tenant_id=tid, bot_id=None, date=d,
                        calls=calls, contained_calls=contained, escalations=calls - contained,
                        minutes=round(calls * 3.2, 1), csat_avg=round(min(csat_s[i], 5.0), 2),
                        cost_llm=round(base_ai * 0.60, 2), cost_tts=round(base_ai * 0.22, 2),
                        cost_stt=round(base_ai * 0.18, 2), cost_telephony=round(calls * 0.081, 2),
                    ))
                bump_key = "usage_days"
                created[bump_key] = created.get(bump_key, 0) + days
            # Per-bot rows for tn-001's active bots
            for b_idx, (bid, calls_day) in enumerate([("bot-101", 612), ("bot-102", 289), ("bot-105", 148), ("bot-106", 96)]):
                calls_s = _series(41 + b_idx, days, calls_day, calls_day * 0.3, 0)
                rate_s = _series(51 + b_idx, days, 72, 10, 0)
                for i, d in enumerate(dates):
                    calls = round(calls_s[i])
                    contained = round(calls * min(rate_s[i], 98) / 100)
                    db.add(UsageRecord(
                        id=f"ur-{bid}-{d.isoformat()}", tenant_id="tn-001", bot_id=bid, date=d,
                        calls=calls, contained_calls=contained, escalations=calls - contained,
                        minutes=round(calls * 3.0, 1), csat_avg=4.4,
                        cost_llm=round(calls * 0.055, 2), cost_tts=round(calls * 0.02, 2),
                        cost_stt=round(calls * 0.017, 2), cost_telephony=round(calls * 0.081, 2),
                    ))

        db.commit()
        logger.info("Demo seed complete: %s", created)
        return created
    except Exception:
        db.rollback()
        raise
    finally:
        if own:
            db.close()
