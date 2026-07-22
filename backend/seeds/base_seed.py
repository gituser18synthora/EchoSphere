"""Idempotent base seed — mandatory records only.

Safe to run any number of times: every insert is guarded by a natural-key
lookup; existing rows are never modified or deleted. No fake dashboard values
or dummy business records are created here (see demo_seed for the explicit
opt-in development dataset).
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.config import get_settings
from shared.ids import new_id
from backend.core.security import hash_password
from shared.db.mysql import get_sessionmaker
from shared.models import (
    AiConfigProfile,
    ApprovedModel,
    DataRegion,
    Guardrail,
    HealthMetric,
    Industry,
    Integration,
    Permission,
    Plan,
    PlatformTemplate,
    ProviderDef,
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
    ("review_knowledge_chunks", "Review knowledge documents & chunks", "platform"),
    ("prompts.manage", "Edit & approve prompts", "tenant"),
    ("conversations.view", "Review conversations", "tenant"),
    ("analytics.view", "View analytics", "tenant"),
    ("team.manage", "Manage team members", "tenant"),
    ("integrations.manage", "Manage integrations", "tenant"),
    ("settings.manage", "Manage tenant settings", "tenant"),
    # Master data (platform)
    ("manage_master_data", "Manage platform master data", "platform"),
    ("manage_industries", "Manage industries", "platform"),
    ("manage_data_regions", "Manage data regions", "platform"),
    ("manage_plans", "Manage plans", "platform"),
    ("manage_ai_profiles", "Manage AI configuration profiles", "platform"),
    ("manage_languages", "Manage supported languages", "platform"),
    # Tenant profile
    ("view_tenant_profile", "View tenant profile", "tenant"),
    ("edit_tenant_profile", "Edit tenant profile", "tenant"),
    # Account security
    ("change_own_password", "Change own password", "account"),
    ("reset_user_password", "Reset another user's password", "account"),
    # Knowledge
    ("manage_knowledge", "Manage knowledge bases", "tenant"),
    ("upload_knowledge_documents", "Upload knowledge documents", "tenant"),
    ("retry_knowledge_ingestion", "Retry knowledge ingestion", "tenant"),
    # Prompts
    ("manage_prompts", "Create & edit prompts", "tenant"),
    ("approve_prompts", "Approve prompts", "tenant"),
    ("publish_prompts", "Publish prompts", "tenant"),
    # Voice / NLU / API
    ("manage_voices", "Manage voice configuration", "tenant"),
    ("manage_intents", "Manage intents", "tenant"),
    ("manage_entities", "Manage entities", "tenant"),
    ("manage_api_connections", "Manage API connections", "tenant"),
    ("test_api_connections", "Test API connections", "tenant"),
    # Channels
    ("manage_channels", "Manage deployment channels", "tenant"),
]

ROLE_PERMISSIONS = {
    "super_admin": [p[0] for p in PERMISSIONS],
    "tenant_admin": [
        "bots.view", "bots.manage", "bots.publish", "knowledge.view", "knowledge.manage",
        "prompts.manage", "conversations.view", "analytics.view", "team.manage",
        "integrations.manage", "settings.manage",
        "view_tenant_profile", "edit_tenant_profile",
        "change_own_password", "reset_user_password",
        "manage_knowledge", "upload_knowledge_documents", "retry_knowledge_ingestion",
        "manage_prompts", "approve_prompts", "publish_prompts",
        "manage_voices", "manage_intents", "manage_entities",
        "manage_api_connections", "test_api_connections",
        "manage_channels",
    ],
    "tenant_user": [
        "bots.view", "knowledge.view", "conversations.view", "analytics.view",
        "view_tenant_profile", "change_own_password",
    ],
}

INDUSTRIES = [
    # (code, name, icon, description)
    ("banking", "Banking", "bank", "Retail and corporate banking voice journeys."),
    ("insurance", "Insurance", "shield", "Policy servicing, claims intake and renewals."),
    ("healthcare", "Healthcare", "heart", "Appointments, triage routing and patient support."),
    ("call_center", "Call Center", "phone", "General inbound/outbound contact-center automation."),
    ("customer_support", "Customer Support", "help", "Product and account support desks."),
    ("sales", "Sales", "trend", "Lead qualification, follow-ups and campaign calls."),
    ("ecommerce", "E-commerce", "cart", "Orders, returns, delivery status and catalog queries."),
    ("education", "Education", "book", "Admissions, fee reminders and student services."),
    ("travel_hospitality", "Travel and Hospitality", "plane", "Bookings, itinerary changes and concierge."),
    ("real_estate", "Real Estate", "home", "Site-visit scheduling and listing enquiries."),
    ("automotive", "Automotive", "car", "Service booking, test drives and roadside assist."),
    ("telecom", "Telecom", "signal", "Plan changes, billing and outage support."),
    ("logistics", "Logistics", "truck", "Shipment tracking, pickups and delivery windows."),
    ("government", "Government Services", "landmark", "Citizen services and scheme helplines."),
    ("financial_services", "Financial Services", "coins", "Lending, cards, collections and advisory desks."),
    ("utilities", "Utilities", "zap", "Billing, outage reporting and new connections."),
    ("other", "Other", "grid", "Anything that does not fit the categories above."),
]

DATA_REGIONS = [
    # (code, name, country, region, description)
    ("in", "India", "India", "South Asia", "Configured operational region covering India."),
    ("in-mumbai", "India – Mumbai", "India", "South Asia", "Mumbai metro region."),
    ("in-hyderabad", "India – Hyderabad", "India", "South Asia", "Hyderabad metro region."),
    ("apac", "Asia Pacific", None, "APAC", "Asia-Pacific multi-country region."),
    ("eu", "Europe", None, "EU", "European Union data boundary."),
    ("us", "United States", "United States", "North America", "United States region."),
    ("me", "Middle East", None, "MEA", "Middle East region."),
    ("global", "Global", None, "Global", "No regional pinning — global routing."),
]

AI_PROFILES = [
    # (code, name, cost_category, description, overrides)
    ("low_cost", "Low Cost", "low",
     "Cheapest viable stack for high-volume simple flows.",
     {"llm_model": "gpt-4o-mini", "tts_model": "tts-1", "retrieval_top_k": 4,
      "max_output_tokens": 300, "temperature": 0.3}),
    ("balanced", "Balanced", "medium",
     "Balanced latency, quality and cost — the default starting point.",
     {"llm_model": "gpt-4o-mini", "tts_model": "tts-1", "retrieval_top_k": 6}),
    ("high_accuracy", "High Accuracy", "high",
     "Best answer quality; larger models and deeper retrieval.",
     {"llm_model": "gpt-4o", "tts_model": "tts-1-hd", "retrieval_top_k": 10,
      "max_output_tokens": 900, "temperature": 0.2}),
    ("low_latency", "Low Latency", "medium",
     "Tuned for fastest turn-taking on voice calls.",
     {"llm_model": "gpt-4o-mini", "retrieval_top_k": 3, "max_output_tokens": 250,
      "response_timeout_ms": 4000}),
    ("enterprise", "Enterprise", "high",
     "Enterprise defaults with fallback providers and generous limits.",
     {"llm_model": "gpt-4o", "retrieval_top_k": 8, "max_output_tokens": 800,
      "fallback_providers": [{"llm_provider": "anthropic", "llm_model": "claude-sonnet-5"}]}),
    ("custom", "Custom", "medium",
     "Start empty and configure every provider and model manually.", {}),
]

PROVIDERS = [
    # (kind, code, name, requires_api_key, description)
    ("stt", "openai", "OpenAI Whisper", True, "Whisper speech-to-text via the OpenAI API."),
    ("stt", "deepgram", "Deepgram", True, "Low-latency streaming STT."),
    ("stt", "assemblyai", "AssemblyAI", True, "Batch and realtime STT."),
    ("stt", "sarvam", "Sarvam AI", True, "Indic-language STT (saarika)."),
    ("stt", "azure", "Azure Speech", True, "Microsoft Azure speech-to-text."),
    ("stt", "google", "Google Cloud STT", True, "Google Cloud speech-to-text."),
    ("stt", "mock", "Mock STT (dev)", False, "Deterministic development STT — no external calls."),
    ("tts", "openai", "OpenAI TTS", True, "OpenAI text-to-speech voices."),
    ("tts", "elevenlabs", "ElevenLabs", True, "High-fidelity neural voices."),
    ("tts", "sarvam", "Sarvam AI", True, "Indic-language TTS (bulbul)."),
    ("tts", "azure", "Azure Speech", True, "Microsoft Azure neural voices."),
    ("tts", "google", "Google Cloud TTS", True, "Google Cloud neural voices."),
    ("tts", "mock", "Mock TTS (dev)", False, "Deterministic development TTS — no external calls."),
    ("llm", "openai", "OpenAI", True, "GPT model family."),
    ("llm", "anthropic", "Anthropic", True, "Claude model family."),
    ("llm", "azure", "Azure OpenAI", True, "GPT models on Azure."),
    ("llm", "google", "Google Gemini", True, "Gemini model family."),
    ("llm", "mock", "Mock LLM (dev)", False, "Deterministic development LLM — no external calls."),
    ("embedding", "openai", "OpenAI Embeddings", True, "text-embedding-3 family."),
    ("embedding", "mock", "Mock Embeddings (dev)", False, "Hash-based development embedder."),
    ("voice", "platform", "Platform Voices", False, "Built-in platform voice catalog."),
    ("voice", "elevenlabs", "ElevenLabs Voices", True, "ElevenLabs voice catalog."),
    ("voice", "azure", "Azure Voice Catalog", True, "Azure neural voice catalog."),
    ("voice", "google", "Google Voice Catalog", True, "Google Cloud voice catalog."),
]

# (code, name, monthly, bots, minutes, seats, recommended, description)
PLANS = [
    ("starter", "Starter", 490, 2, 10000, 5, False,
     "2 bots, 10k minutes, community support."),
    ("growth", "Growth", 2400, 8, 80000, 15, True,
     "8 bots, 80k minutes, standard SLA."),
    ("enterprise", "Enterprise", 9800, 20, 200000, 50, False,
     "20 bots, 200k minutes, 99.9% SLA, SSO."),
]

# (code, name, native_name, iso, script, direction, provider_support)
# provider_support lists which *configured provider adapters* claim the
# language — platform listing alone never implies STT/TTS availability.
_MAJOR_INDIC = {"stt": ["sarvam", "google", "azure", "openai"], "tts": ["sarvam", "google", "azure"],
                "llm": ["openai", "anthropic", "google"]}
_MINOR_INDIC = {"stt": [], "tts": [], "llm": ["openai", "anthropic", "google"]}
_GLOBAL = {"stt": ["openai", "deepgram", "assemblyai", "azure", "google"],
           "tts": ["openai", "elevenlabs", "azure", "google"],
           "llm": ["openai", "anthropic", "google"]}

LANGUAGES = [
    ("en-US", "English (US)", "English", "en", "Latin", "ltr", _GLOBAL),
    ("es-US", "Spanish (US)", "Español", "es", "Latin", "ltr", _GLOBAL),
    ("es-MX", "Spanish (MX)", "Español", "es", "Latin", "ltr", _GLOBAL),
    ("en-GB", "English (UK)", "English", "en", "Latin", "ltr", _GLOBAL),
    ("fr-FR", "French", "Français", "fr", "Latin", "ltr", _GLOBAL),
    ("de-DE", "German", "Deutsch", "de", "Latin", "ltr", _GLOBAL),
    ("vi-VN", "Vietnamese", "Tiếng Việt", "vi", "Latin", "ltr",
     {"stt": ["openai", "google"], "tts": ["google"], "llm": ["openai", "anthropic", "google"]}),
    # ── India ────────────────────────────────────────────────────────────
    ("en-IN", "English (India)", "English", "en", "Latin", "ltr", _MAJOR_INDIC),
    ("hi-IN", "Hindi", "हिन्दी", "hi", "Devanagari", "ltr", _MAJOR_INDIC),
    ("bn-IN", "Bengali", "বাংলা", "bn", "Bengali", "ltr", _MAJOR_INDIC),
    ("mr-IN", "Marathi", "मराठी", "mr", "Devanagari", "ltr", _MAJOR_INDIC),
    ("te-IN", "Telugu", "తెలుగు", "te", "Telugu", "ltr", _MAJOR_INDIC),
    ("ta-IN", "Tamil", "தமிழ்", "ta", "Tamil", "ltr", _MAJOR_INDIC),
    ("gu-IN", "Gujarati", "ગુજરાતી", "gu", "Gujarati", "ltr", _MAJOR_INDIC),
    ("ur-IN", "Urdu", "اردو", "ur", "Perso-Arabic", "rtl",
     {"stt": ["openai", "google", "azure"], "tts": ["google", "azure"], "llm": ["openai", "anthropic", "google"]}),
    ("kn-IN", "Kannada", "ಕನ್ನಡ", "kn", "Kannada", "ltr", _MAJOR_INDIC),
    ("or-IN", "Odia", "ଓଡ଼ିଆ", "or", "Odia", "ltr", _MAJOR_INDIC),
    ("ml-IN", "Malayalam", "മലയാളം", "ml", "Malayalam", "ltr", _MAJOR_INDIC),
    ("pa-IN", "Punjabi", "ਪੰਜਾਬੀ", "pa", "Gurmukhi", "ltr", _MAJOR_INDIC),
    ("as-IN", "Assamese", "অসমীয়া", "as", "Bengali", "ltr",
     {"stt": ["google"], "tts": ["google"], "llm": ["openai", "anthropic", "google"]}),
    ("mai-IN", "Maithili", "मैथिली", "mai", "Devanagari", "ltr", _MINOR_INDIC),
    ("sa-IN", "Sanskrit", "संस्कृतम्", "sa", "Devanagari", "ltr", _MINOR_INDIC),
    ("kok-IN", "Konkani", "कोंकणी", "kok", "Devanagari", "ltr", _MINOR_INDIC),
    ("ks-IN", "Kashmiri", "كٲشُر", "ks", "Perso-Arabic", "rtl", _MINOR_INDIC),
    ("doi-IN", "Dogri", "डोगरी", "doi", "Devanagari", "ltr", _MINOR_INDIC),
    ("mni-IN", "Manipuri", "ꯃꯤꯇꯩꯂꯣꯟ", "mni", "Meitei Mayek", "ltr", _MINOR_INDIC),
    ("brx-IN", "Bodo", "बड़ो", "brx", "Devanagari", "ltr", _MINOR_INDIC),
    ("sat-IN", "Santali", "ᱥᱟᱱᱛᱟᱲᱤ", "sat", "Ol Chiki", "ltr", _MINOR_INDIC),
    ("ne-IN", "Nepali", "नेपाली", "ne", "Devanagari", "ltr",
     {"stt": ["google"], "tts": ["google"], "llm": ["openai", "anthropic", "google"]}),
    ("sd-IN", "Sindhi", "سنڌي", "sd", "Perso-Arabic", "rtl", _MINOR_INDIC),
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
               "settings": 0, "templates": 0, "users": 0,
               "industries": 0, "data_regions": 0, "ai_profiles": 0, "providers": 0}
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

        for i, (code, name, price, bots, minutes, seats, recommended, desc) in enumerate(PLANS):
            row = db.scalar(select(Plan).where(Plan.code == code))
            if row is None:
                db.add(Plan(
                    id=new_id("pl"), code=code, name=name, price_monthly=price,
                    price_annual=price * 10, bot_limit=bots, minutes_included=minutes,
                    seats_included=seats, description=desc, is_recommended=recommended,
                    sort_order=i,
                ))
                created["plans"] += 1
            elif row.description is None:
                # Backfill the new metadata columns once on pre-existing rows.
                row.description = desc
                row.is_recommended = recommended
                row.sort_order = i

        for i, (code, name, native, iso, script, direction, support) in enumerate(LANGUAGES):
            row = db.scalar(select(SupportedLanguage).where(SupportedLanguage.code == code))
            if row is None:
                db.add(SupportedLanguage(
                    id=new_id("lang"), code=code, name=name, native_name=native,
                    iso_code=iso, script=script, direction=direction,
                    provider_support=support, sort_order=i,
                ))
                created["languages"] += 1
            else:
                # Backfill new metadata columns on pre-existing rows — NULL
                # fields only, user edits are never overwritten.
                if row.iso_code is None:
                    row.iso_code = iso
                if row.script is None:
                    row.script = script
                if row.provider_support is None:
                    row.provider_support = support
                if row.direction == "ltr" and direction == "rtl":
                    row.direction = direction

        for i, (code, name, icon, desc) in enumerate(INDUSTRIES):
            if db.scalar(select(Industry).where(Industry.code == code)) is None:
                db.add(Industry(
                    id=new_id("ind"), code=code, name=name, icon=icon,
                    description=desc, sort_order=i,
                ))
                created["industries"] += 1

        for i, (code, name, country, region, desc) in enumerate(DATA_REGIONS):
            if db.scalar(select(DataRegion).where(DataRegion.code == code)) is None:
                db.add(DataRegion(
                    id=new_id("dr"), code=code, name=name, country=country,
                    region=region, description=desc, sort_order=i,
                    infrastructure_ready=False,
                ))
                created["data_regions"] += 1

        for i, (code, name, cost, desc, overrides) in enumerate(AI_PROFILES):
            if db.scalar(select(AiConfigProfile).where(AiConfigProfile.code == code)) is None:
                profile = AiConfigProfile(
                    id=new_id("aip"), code=code, name=name, description=desc,
                    cost_category=cost, sort_order=i,
                    stt_provider="openai", stt_model="whisper-1",
                    llm_provider="openai", llm_model="gpt-4o-mini",
                    tts_provider="openai", tts_model="tts-1", default_voice="alloy",
                    embedding_provider="openai", embedding_model="text-embedding-3-small",
                    embedding_dimension=1536,
                )
                for key, value in overrides.items():
                    setattr(profile, key, value)
                if code == "custom":
                    for key in ("stt_provider", "stt_model", "llm_provider", "llm_model",
                                "tts_provider", "tts_model", "default_voice",
                                "embedding_provider", "embedding_model"):
                        setattr(profile, key, None)
                    profile.embedding_dimension = None
                db.add(profile)
                created["ai_profiles"] += 1

        for i, (kind, code, name, needs_key, desc) in enumerate(PROVIDERS):
            exists = db.scalar(
                select(ProviderDef).where(ProviderDef.kind == kind, ProviderDef.code == code)
            )
            if exists is None:
                db.add(ProviderDef(
                    id=new_id("prov"), kind=kind, code=code, name=name,
                    description=desc, requires_api_key=needs_key, sort_order=i,
                    secret_ref=f"env:{code.upper()}_API_KEY" if needs_key else None,
                ))
                created["providers"] += 1

        for vid, name, gender, langs, accent, styles, latency, premium, sample in VOICES:
            if db.get(VoiceProfile, vid) is None:
                db.add(VoiceProfile(
                    id=vid, name=name, gender=gender, languages=langs, accent=accent,
                    styles=styles, latency_ms=latency, premium=premium, sample_text=sample,
                    provider="platform",
                ))
                created["voices"] += 1

        # Provider model catalog + provider voices (Sarvam speakers, ElevenLabs voices).
        from backend.seeds.provider_catalog_seed import seed_provider_catalog

        created.update(seed_provider_catalog(db))

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
