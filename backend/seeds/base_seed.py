"""Idempotent base seed — mandatory records only.

Safe to run any number of times: every insert is guarded by a natural-key
lookup; existing rows are never modified or deleted. No fake dashboard values
or dummy business records are created here (see demo_seed for the explicit
opt-in development dataset).
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.core.ids import new_id
from backend.core.security import hash_password
from backend.db.mysql import get_sessionmaker
from backend.models import (
    ApprovedModel,
    Guardrail,
    HealthMetric,
    Integration,
    Permission,
    Plan,
    PlatformTemplate,
    Role,
    RolePermission,
    SupportedLanguage,
    SystemSetting,
    User,
    VoiceProfile,
)

logger = logging.getLogger("backend.seed")

ROLES = [
    ("super_admin", "Super Admin", "platform", "Full platform governance: tenants, subscriptions, AI governance, telephony, security."),
    ("tenant_admin", "Tenant Admin", "tenant", "Build, test and publish the organization's VoiceBots; manage team and settings."),
    ("tenant_user", "Tenant User", "tenant", "Work within the organization's workspace: view bots, conversations and analytics."),
]

PERMISSIONS = [
    # (code, name, category)
    ("tenants.manage", "Manage tenants", "platform"),
    ("billing.manage", "Manage plans & billing", "platform"),
    ("governance.manage", "Manage AI governance", "platform"),
    ("telephony.manage", "Manage numbers & SIP", "platform"),
    ("security.manage", "Manage users, roles & audit", "platform"),
    ("monitoring.view", "View platform monitoring", "platform"),
    ("bots.view", "View VoiceBots", "tenant"),
    ("bots.manage", "Create & edit VoiceBots", "tenant"),
    ("bots.publish", "Publish & rollback releases", "tenant"),
    ("knowledge.view", "View knowledge", "tenant"),
    ("knowledge.manage", "Manage knowledge sources", "tenant"),
    ("prompts.manage", "Edit & approve prompts", "tenant"),
    ("conversations.view", "Review conversations", "tenant"),
    ("analytics.view", "View analytics", "tenant"),
    ("team.manage", "Manage team members", "tenant"),
    ("integrations.manage", "Manage integrations", "tenant"),
    ("settings.manage", "Manage tenant settings", "tenant"),
]

ROLE_PERMISSIONS = {
    "super_admin": [p[0] for p in PERMISSIONS],
    "tenant_admin": [
        "bots.view", "bots.manage", "bots.publish", "knowledge.view", "knowledge.manage",
        "prompts.manage", "conversations.view", "analytics.view", "team.manage",
        "integrations.manage", "settings.manage",
    ],
    "tenant_user": ["bots.view", "knowledge.view", "conversations.view", "analytics.view"],
}

PLANS = [
    ("starter", "Starter", 490, 2, 10000, 5),
    ("growth", "Growth", 2400, 8, 80000, 15),
    ("enterprise", "Enterprise", 9800, 20, 200000, 50),
]

LANGUAGES = [
    ("en-US", "English (US)", "English"),
    ("es-US", "Spanish (US)", "Español"),
    ("es-MX", "Spanish (MX)", "Español"),
    ("en-GB", "English (UK)", "English"),
    ("fr-FR", "French", "Français"),
    ("de-DE", "German", "Deutsch"),
    ("hi-IN", "Hindi", "हिन्दी"),
    ("vi-VN", "Vietnamese", "Tiếng Việt"),
]

VOICES = [
    ("vp-01", "Amara", "female", ["en-US", "es-US"], "American · warm", ["Empathetic", "Calm"], 210, False, "Hi there! I can help you book your next appointment in just a minute."),
    ("vp-02", "Nova", "female", ["en-US", "es-US", "fr-FR"], "American · bright", ["Friendly", "Energetic"], 190, True, "Thanks for calling! Let's find a time that works perfectly for you."),
    ("vp-03", "Atlas", "male", ["en-US"], "American · steady", ["Professional", "Reassuring"], 220, False, "I understand. Let me route you to the right team straight away."),
    ("vp-04", "Lyra", "female", ["en-GB", "en-US"], "British · crisp", ["Professional", "Concise"], 205, True, "Certainly. Your feedback helps us improve every visit."),
    ("vp-05", "Orion", "male", ["en-US", "es-US"], "American · deep", ["Calm", "Trustworthy"], 230, False, "I can help with that billing question — one moment please."),
    ("vp-06", "Sana", "female", ["en-US", "hi-IN"], "Neutral · soft", ["Empathetic", "Patient"], 215, False, "Take your time. I'm here to help whenever you're ready."),
    ("vp-07", "Kai", "neutral", ["en-US", "vi-VN"], "Neutral · modern", ["Friendly", "Clear"], 200, True, "Your refill is ready for pickup after 2 PM today."),
    ("vp-08", "Elena", "female", ["es-US", "es-MX"], "Latin American · warm", ["Empathetic", "Expressive"], 225, False, "Con mucho gusto le ayudo a encontrar una cita disponible."),
]

GUARDRAILS = [
    ("PII redaction in transcripts", "Privacy", "Redacts SSN, card numbers and DOB from stored transcripts and logs.", "redact", True),
    ("Medical advice boundary", "Safety", "Blocks diagnosis or dosage advice; routes to licensed staff.", "block", True),
    ("Payment collection restriction", "Compliance", "Bots may reference balances but never collect card numbers by voice.", "block", True),
    ("Competitor mention flag", "Brand", "Flags conversations where competitors are discussed for QA review.", "flag", False),
    ("Profanity / abuse de-escalation", "Safety", "Switches to calm register and offers human handover on repeated abuse.", "flag", True),
]

MODELS = [
    ("sonnet-5", "Anthropic", "conversation", "approved", 0.003, 640),
    ("haiku-4.5", "Anthropic", "classification", "approved", 0.0008, 210),
    ("opus-4.8", "Anthropic", "conversation", "testing", 0.012, 980),
    ("embed-multilingual-3", "VectorWorks", "embedding", "approved", 0.0001, 45),
    ("summarize-lite-2", "VectorWorks", "summarization", "deprecated", 0.0004, 380),
]

INTEGRATIONS = [
    ("Epic EHR", "Healthcare", "Appointment slots, patient verification and scheduling."),
    ("Zendesk", "Support", "Help-center articles as a live knowledge connector."),
    ("Salesforce Health Cloud", "CRM", "Sync caller context and escalation cases."),
    ("Slack", "Notifications", "Publish, rollback and alert notifications to channels."),
    ("Genesys Cloud", "Contact Center", "Warm-transfer escalations into agent queues."),
    ("Microsoft Teams", "Notifications", "Approval requests and daily digest cards."),
]

HEALTH_METRICS = [
    ("API gateway", "good", "100% uptime", "≥99.95%"),
    ("Call orchestration", "good", "—", "<250ms"),
    ("SIP trunks", "neutral", "—", "<0.5%"),
    ("STT latency", "neutral", "—", "<400ms"),
    ("LLM latency", "neutral", "—", "<800ms"),
    ("TTS latency", "neutral", "—", "<300ms"),
    ("Embedding queue", "neutral", "—", "<5 min"),
    ("Recording storage", "neutral", "—", "<80%"),
]

SYSTEM_SETTINGS = [
    ("platform.name", "AUREXION EchoSphere", "Display name of the platform"),
    ("platform.default_language", "en-US", "Default language for new bots"),
    ("voice.default_provider", {"provider": "platform", "voice_id": "vp-01", "speed": 1.0, "pitch": 1.0}, "Default voice-provider configuration template for new bots"),
    ("retention.transcripts_days", 90, "Default transcript retention window (days)"),
]

TEMPLATES = [
    ("prompt_library", "Identity verification (HIPAA)", "Two-factor caller verification before sharing any PHI.", {"category": "Compliance"}),
    ("prompt_library", "Payment reminder call", "Outbound dunning script with promise-to-pay capture.", {"category": "Billing"}),
    ("prompt_library", "NPS survey", "Three-question NPS survey with open-ended follow-up.", {"category": "Survey"}),
    ("prompt_library", "Order status lookup", "Order-tracking flow grounded in the commerce API.", {"category": "Retail"}),
    ("prompt_version", "Safety preamble v9 → v10 (draft)", "Adds jailbreak-resistance clause · ring: canary 5%", {"status": "draft"}),
    ("prompt_version", "Healthcare scaffold v3 → v4", "Published · approved by platform admin", {"status": "published"}),
    ("prompt_version", "Safety preamble v8 → v9", "Published · approved by platform admin", {"status": "published"}),
    ("knowledge_template", "Healthcare FAQ pack", "Starter FAQ structure for clinics: hours, insurance, prep.", {"items": 60}),
    ("knowledge_template", "Retail returns pack", "Returns, refunds and exchange policy scaffold.", {"items": 32}),
    ("knowledge_template", "Banking security pack", "Verification scripts and fraud escalation knowledge.", {"items": 44}),
    ("journey_template", "Appointment booking", "Slot lookup → verify → confirm → SMS recap.", {"nodes": 10}),
    ("journey_template", "Billing support", "Balance lookup → dispute triage → payment plan.", {"nodes": 8}),
    ("journey_template", "Lead qualification", "Capture → score → route to sales queue.", {"nodes": 7}),
    ("journey_template", "Order tracking", "Order id → status → proactive delay handling.", {"nodes": 6}),
    ("journey_template", "Password / account reset", "Verify → reset link → confirm.", {"nodes": 5}),
    ("journey_template", "Post-visit survey", "CSAT → NPS → open feedback.", {"nodes": 4}),
    ("action_block", "Send SMS", "Send a templated SMS via the tenant's provider.", {"icon": "message"}),
    ("action_block", "Send email", "Send a templated email.", {"icon": "mail"}),
    ("action_block", "Create CRM record", "Create or update a CRM contact/case.", {"icon": "database"}),
    ("action_block", "Book appointment", "Write a booking into the scheduling system.", {"icon": "calendar"}),
    ("action_block", "Warm transfer", "Transfer the call with context to an agent queue.", {"icon": "phone"}),
    ("action_block", "Webhook", "POST conversation context to an external URL.", {"icon": "zap"}),
    ("action_block", "Knowledge answer", "Answer from retrieval with citations.", {"icon": "book"}),
    ("action_block", "Escalation ticket", "Open a ticket in the connected helpdesk.", {"icon": "alert"}),
]


def run_base_seed(db: Session | None = None) -> dict:
    """Returns a summary of what was created (counts)."""
    own_session = db is None
    if own_session:
        db = get_sessionmaker()()
    created = {"roles": 0, "permissions": 0, "plans": 0, "languages": 0, "voices": 0,
               "guardrails": 0, "models": 0, "integrations": 0, "health_metrics": 0,
               "settings": 0, "templates": 0, "users": 0}
    try:
        role_map: dict[str, Role] = {}
        for code, name, scope, desc in ROLES:
            row = db.scalar(select(Role).where(Role.code == code))
            if row is None:
                row = Role(id=new_id("role"), code=code, name=name, scope=scope, description=desc)
                db.add(row)
                db.flush()
                created["roles"] += 1
            role_map[code] = row

        perm_map: dict[str, Permission] = {}
        for code, name, category in PERMISSIONS:
            row = db.scalar(select(Permission).where(Permission.code == code))
            if row is None:
                row = Permission(id=new_id("perm"), code=code, name=name, category=category)
                db.add(row)
                db.flush()
                created["permissions"] += 1
            perm_map[code] = row

        for role_code, perm_codes in ROLE_PERMISSIONS.items():
            role = role_map[role_code]
            for pc in perm_codes:
                perm = perm_map[pc]
                exists = db.scalar(
                    select(RolePermission).where(
                        RolePermission.role_id == role.id,
                        RolePermission.permission_id == perm.id,
                    )
                )
                if exists is None:
                    db.add(RolePermission(role_id=role.id, permission_id=perm.id))

        for code, name, price, bots, minutes, seats in PLANS:
            if db.scalar(select(Plan).where(Plan.code == code)) is None:
                db.add(Plan(
                    id=new_id("pl"), code=code, name=name, price_monthly=price,
                    bot_limit=bots, minutes_included=minutes, seats_included=seats,
                ))
                created["plans"] += 1

        for i, (code, name, native) in enumerate(LANGUAGES):
            if db.scalar(select(SupportedLanguage).where(SupportedLanguage.code == code)) is None:
                db.add(SupportedLanguage(
                    id=new_id("lang"), code=code, name=name, native_name=native, sort_order=i,
                ))
                created["languages"] += 1

        for vid, name, gender, langs, accent, styles, latency, premium, sample in VOICES:
            if db.get(VoiceProfile, vid) is None:
                db.add(VoiceProfile(
                    id=vid, name=name, gender=gender, languages=langs, accent=accent,
                    styles=styles, latency_ms=latency, premium=premium, sample_text=sample,
                    provider="platform",
                ))
                created["voices"] += 1

        for name, category, desc, enforcement, enabled in GUARDRAILS:
            if db.scalar(select(Guardrail).where(Guardrail.name == name)) is None:
                db.add(Guardrail(
                    id=new_id("gr"), name=name, category=category, description=desc,
                    enforcement=enforcement, enabled=enabled,
                ))
                created["guardrails"] += 1

        for name, provider, purpose, status, cost, latency in MODELS:
            exists = db.scalar(
                select(ApprovedModel).where(
                    ApprovedModel.name == name, ApprovedModel.purpose == purpose
                )
            )
            if exists is None:
                db.add(ApprovedModel(
                    id=new_id("md"), name=name, provider=provider, purpose=purpose,
                    status=status, cost_per_1k=cost, latency_p50=latency,
                ))
                created["models"] += 1

        for name, category, desc in INTEGRATIONS:
            if db.scalar(select(Integration).where(Integration.name == name)) is None:
                db.add(Integration(id=new_id("ig"), name=name, category=category, description=desc))
                created["integrations"] += 1

        for i, (name, status, value, target) in enumerate(HEALTH_METRICS):
            if db.scalar(select(HealthMetric).where(HealthMetric.name == name)) is None:
                db.add(HealthMetric(
                    id=new_id("hm"), name=name, status=status, value=value,
                    target=target, spark=[], sort_order=i,
                ))
                created["health_metrics"] += 1

        for key, value, desc in SYSTEM_SETTINGS:
            if db.scalar(select(SystemSetting).where(SystemSetting.key == key)) is None:
                db.add(SystemSetting(id=new_id("sys"), key=key, value=value, description=desc))
                created["settings"] += 1

        for i, (kind, name, desc, payload) in enumerate(TEMPLATES):
            exists = db.scalar(
                select(PlatformTemplate).where(
                    PlatformTemplate.kind == kind, PlatformTemplate.name == name
                )
            )
            if exists is None:
                db.add(PlatformTemplate(
                    id=new_id("pt"), kind=kind, name=name, description=desc,
                    payload=payload, sort_order=i,
                ))
                created["templates"] += 1

        # Super Admin account — created once, never overwritten, never deleted.
        settings = get_settings()
        email = settings.superadmin_email.lower()
        if db.scalar(select(User).where(User.email == email)) is None:
            if not settings.superadmin_password:
                logger.warning(
                    "SUPERADMIN_PASSWORD is not set — skipping super admin creation."
                )
            else:
                db.add(User(
                    id=new_id("usr"),
                    email=email,
                    name=settings.superadmin_name,
                    password_hash=hash_password(settings.superadmin_password),
                    role_id=role_map["super_admin"].id,
                    tenant_id=None,
                    status="active",
                ))
                created["users"] += 1

        db.commit()
        logger.info("Base seed complete: %s", {k: v for k, v in created.items() if v})
        return created
    except Exception:
        db.rollback()
        raise
    finally:
        if own_session:
            db.close()
