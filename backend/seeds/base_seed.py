"""Idempotent base seed — mandatory records only.

Safe to run any number of times: every insert is guarded by a natural-key
lookup; existing rows are never modified or deleted. No fake dashboard values
or dummy business records are created here (see demo_seed for the explicit
opt-in development dataset).
"""

import logging
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from shared.config import get_settings
from shared.ids import new_id
from backend.core.security import hash_password
from shared.db.mysql import get_sessionmaker
from shared.models import (
    AiConfigProfile,
    ApprovedModel,
    Country,
    Currency,
    DataRegion,
    Guardrail,
    GuardrailProfile,
    GuardrailProfileRule,
    Industry,
    Integration,
    Permission,
    Plan,
    PlatformTemplate,
    ProviderDef,
    ProviderPricing,
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
    ("tenant_user", "Tenant User", "tenant", "Work on the organization's shared VoiceBots: edit knowledge, prompts, voice, workflows and testing — no channels, integrations, settings or cost visibility."),
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
    # Workflows / testing
    ("manage_workflows", "Edit bot workflows", "tenant"),
    ("manage_testing", "Create & run test scenarios", "tenant"),
    # Voice cloning (distinct from manage_voices so a role may configure bot
    # voices without being able to create or manage cloned voices)
    ("manage_voice_clones", "Create & manage cloned voices", "tenant"),
    # Financial visibility: cost/pricing/spend fields in tenant analytics,
    # conversations, dashboards and exports
    ("costs.view", "View costs, pricing & spend", "tenant"),
    # Billing configuration (platform)
    ("manage_currencies", "Manage currencies", "platform"),
    ("manage_exchange_rates", "Manage exchange rates", "platform"),
    ("manage_pricing", "Manage provider pricing", "platform"),
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
        "manage_workflows", "manage_testing", "manage_voice_clones", "costs.view",
    ],
    # Tenant User works on the tenant's shared resources: full edit access to
    # knowledge, prompts, voice configuration, workflows and testing, but no
    # channels/integrations/settings/team/voice-cloning management and no
    # financial visibility (costs.view is deliberately withheld).
    "tenant_user": [
        "bots.view", "knowledge.view", "conversations.view", "analytics.view",
        "view_tenant_profile", "change_own_password",
        "manage_knowledge", "upload_knowledge_documents", "retry_knowledge_ingestion",
        "manage_prompts",
        "manage_voices",
        "manage_workflows", "manage_testing",
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

# ISO 3166-1 alpha-2/alpha-3 countries offered by the first regional rollout.
# This catalog is intentionally Asia-only for now.
COUNTRIES = [
    ("AF", "AFG", "Afghanistan"), ("AM", "ARM", "Armenia"), ("AZ", "AZE", "Azerbaijan"),
    ("BH", "BHR", "Bahrain"), ("BD", "BGD", "Bangladesh"), ("BT", "BTN", "Bhutan"),
    ("BN", "BRN", "Brunei"), ("KH", "KHM", "Cambodia"), ("CN", "CHN", "China"),
    ("CY", "CYP", "Cyprus"), ("GE", "GEO", "Georgia"), ("IN", "IND", "India"),
    ("ID", "IDN", "Indonesia"), ("IR", "IRN", "Iran"), ("IQ", "IRQ", "Iraq"),
    ("IL", "ISR", "Israel"), ("JP", "JPN", "Japan"), ("JO", "JOR", "Jordan"),
    ("KZ", "KAZ", "Kazakhstan"), ("KW", "KWT", "Kuwait"), ("KG", "KGZ", "Kyrgyzstan"),
    ("LA", "LAO", "Laos"), ("LB", "LBN", "Lebanon"), ("MY", "MYS", "Malaysia"),
    ("MV", "MDV", "Maldives"), ("MN", "MNG", "Mongolia"), ("MM", "MMR", "Myanmar"),
    ("NP", "NPL", "Nepal"), ("KP", "PRK", "North Korea"), ("OM", "OMN", "Oman"),
    ("PK", "PAK", "Pakistan"), ("PS", "PSE", "Palestine"), ("PH", "PHL", "Philippines"),
    ("QA", "QAT", "Qatar"), ("SA", "SAU", "Saudi Arabia"), ("SG", "SGP", "Singapore"),
    ("KR", "KOR", "South Korea"), ("LK", "LKA", "Sri Lanka"), ("SY", "SYR", "Syria"),
    ("TW", "TWN", "Taiwan"), ("TJ", "TJK", "Tajikistan"), ("TH", "THA", "Thailand"),
    ("TL", "TLS", "Timor-Leste"), ("TR", "TUR", "Türkiye"), ("TM", "TKM", "Turkmenistan"),
    ("AE", "ARE", "United Arab Emirates"), ("UZ", "UZB", "Uzbekistan"),
    ("VN", "VNM", "Vietnam"), ("YE", "YEM", "Yemen"),
]

AI_PROFILES = [
    # (code, name, cost_category, description, overrides)
    # Provider/model choices must stay inside the governed provider matrix
    # (STT=Sarvam, TTS=Sarvam/ElevenLabs, LLM=OpenAI, Embedding=OpenAI).
    ("low_cost", "Low Cost", "low",
     "Cheapest viable stack for high-volume simple flows.",
     {"llm_model": "gpt-4o-mini", "retrieval_top_k": 4,
      "max_output_tokens": 300, "temperature": 0.3}),
    ("balanced", "Balanced", "medium",
     "Balanced latency, quality and cost — the default starting point.",
     {"llm_model": "gpt-4o-mini", "retrieval_top_k": 6}),
    ("high_accuracy", "High Accuracy", "high",
     "Best answer quality; larger models and deeper retrieval.",
     {"llm_model": "gpt-4o", "tts_provider": "elevenlabs",
      "tts_model": "eleven_flash_v2_5", "default_voice": "vp-el-monika",
      "retrieval_top_k": 10, "max_output_tokens": 900, "temperature": 0.2}),
    ("low_latency", "Low Latency", "medium",
     "Tuned for fastest turn-taking on voice calls.",
     {"llm_model": "gpt-4o-mini", "retrieval_top_k": 3, "max_output_tokens": 250,
      "response_timeout_ms": 4000}),
    ("enterprise", "Enterprise", "high",
     "Enterprise defaults with fallback providers and generous limits.",
     {"llm_model": "gpt-4o", "retrieval_top_k": 8, "max_output_tokens": 800,
      "fallback_providers": [{"tts_provider": "elevenlabs", "tts_model": "eleven_flash_v2_5"}]}),
    ("custom", "Custom", "medium",
     "Start empty and configure every provider and model manually.", {}),
]

PROVIDERS = [
    # (kind, code, name, requires_api_key, description, status)
    # Governed matrix: only Sarvam (STT), Sarvam+ElevenLabs (TTS), OpenAI
    # (LLM+Embedding) are active. Other vendors stay in the catalog inactive so
    # existing references keep resolving to stable IDs. The mock provider is a
    # dev/test pseudo-provider — it is excluded from production by the catalog
    # layer, not by status.
    ("stt", "openai", "OpenAI Whisper", True, "Whisper speech-to-text via the OpenAI API.", "inactive"),
    ("stt", "deepgram", "Deepgram", True,
     "Conversational realtime STT (Flux) with model-integrated turn detection.",
     "active"),
    ("stt", "assemblyai", "AssemblyAI", True, "Batch and realtime STT.", "inactive"),
    ("stt", "sarvam", "Sarvam AI", True, "Indic-language STT (saarika).", "active"),
    ("stt", "azure", "Azure Speech", True, "Microsoft Azure speech-to-text.", "inactive"),
    ("stt", "google", "Google Cloud STT", True, "Google Cloud speech-to-text.", "inactive"),
    ("stt", "mock", "Mock STT (dev)", False, "Deterministic development STT — no external calls.", "active"),
    ("tts", "openai", "OpenAI TTS", True, "OpenAI text-to-speech voices.", "inactive"),
    ("tts", "elevenlabs", "ElevenLabs", True, "High-fidelity neural voices.", "active"),
    ("tts", "sarvam", "Sarvam AI", True, "Indic-language TTS (bulbul).", "active"),
    ("tts", "azure", "Azure Speech", True, "Microsoft Azure neural voices.", "inactive"),
    ("tts", "google", "Google Cloud TTS", True, "Google Cloud neural voices.", "inactive"),
    ("tts", "mock", "Mock TTS (dev)", False, "Deterministic development TTS — no external calls.", "active"),
    ("llm", "openai", "OpenAI", True, "GPT model family.", "active"),
    ("llm", "anthropic", "Anthropic", True, "Claude model family.", "inactive"),
    ("llm", "azure", "Azure OpenAI", True, "GPT models on Azure.", "inactive"),
    ("llm", "google", "Google Gemini", True, "Gemini model family.", "inactive"),
    ("llm", "mock", "Mock LLM (dev)", False, "Deterministic development LLM — no external calls.", "active"),
    ("embedding", "openai", "OpenAI Embeddings", True, "text-embedding-3 family.", "active"),
    ("embedding", "mock", "Mock Embeddings (dev)", False, "Hash-based development embedder.", "active"),
    ("voice", "platform", "Platform Voices", False, "Built-in platform voice catalog.", "active"),
    ("voice", "elevenlabs", "ElevenLabs Voices", True, "ElevenLabs voice catalog.", "active"),
    ("voice", "azure", "Azure Voice Catalog", True, "Azure neural voice catalog.", "inactive"),
    ("voice", "google", "Google Voice Catalog", True, "Google Cloud voice catalog.", "inactive"),
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
    # (code, name, category, description, enforcement, enabled, is_mandatory)
    # Mandatory rules apply to EVERY tenant regardless of profile and cannot
    # be disabled via the API — the runtime enforces them even when the
    # guardrail lookup fails (see shared/guardrails/loader.py).
    ("pii_redaction", "PII redaction in transcripts", "Privacy",
     "Redacts card numbers, Aadhaar, PAN and phone numbers from stored transcripts and logs.", "redact", True, True),
    ("secret_leakage_prevention", "Secret & credential leakage prevention", "Privacy",
     "Redacts API keys, tokens and passwords from model output, transcripts and process logs.", "redact", True, True),
    ("unsafe_tool_call_block", "Unsafe tool-call blocking", "Security",
     "Blocks tool/API calls outside the bot's allow-list and any tool call after a blocking guardrail fired in the same turn.", "block", True, True),
    ("prompt_injection_protection", "Prompt-injection protection", "Security",
     "Flags injection attempts in caller speech and retrieved knowledge; scope rules force the bot back on-goal.", "flag", True, True),
    ("medical_advice_boundary", "Medical advice boundary", "Safety",
     "Blocks diagnosis or dosage advice; routes to licensed staff.", "block", True, False),
    ("payment_collection_restriction", "Payment collection restriction", "Compliance",
     "Bots may reference balances but never collect card numbers by voice.", "block", True, False),
    ("booking_commitment_restriction", "Booking & fare commitment restriction", "Compliance",
     "Blocks guaranteed bookings, refunds, fee waivers or free upgrades the bot cannot verify; tool-confirmed facts may still be stated.", "block", True, False),
    ("competitor_mention_flag", "Competitor mention flag", "Brand",
     "Flags conversations where competitors are discussed for QA review.", "flag", False, False),
    ("profanity_deescalation", "Profanity / abuse de-escalation", "Safety",
     "Switches to calm register and offers human handover on repeated abuse.", "flag", True, False),
    # Development/sandbox restrictions — profile rules for internal bots, so
    # a test bot can never dial real numbers or hit production account APIs.
    ("outbound_call_block", "Telephony calls disabled", "Development",
     "Blocks telephony calls for this bot — browser/web test sessions only.", "block", True, False),
    ("state_changing_tool_block", "Account-changing tools disabled", "Development",
     "Blocks state-changing (non-mock) tool/API executions for this bot.", "block", True, False),
]

GUARDRAIL_PROFILES = [
    # (code, name, description, [guardrail codes])
    # Mandatory platform rules are implied in every profile — profiles only
    # list the additional, industry-flavored rules.
    ("standard", "Standard",
     "Baseline conversational safety: PII redaction plus abuse de-escalation on top of the mandatory platform rules.",
     ["profanity_deescalation"]),
    ("healthcare", "Healthcare",
     "Standard plus a medical-advice boundary — no diagnosis or dosage advice by voice.",
     ["profanity_deescalation", "medical_advice_boundary"]),
    ("finance", "Finance",
     "Standard plus payment & advice restrictions — balances may be referenced, card numbers never collected.",
     ["profanity_deescalation", "payment_collection_restriction"]),
    ("travel_hospitality", "Travel and Hospitality",
     "Standard plus booking & payment restrictions — no guaranteed bookings, refunds or free upgrades by voice, and card numbers are never collected.",
     ["profanity_deescalation", "payment_collection_restriction",
      "booking_commitment_restriction"]),
    ("development", "Development / Internal",
     "Internal test bots: no real telephony calls, no state-changing tools. Mandatory platform rules still apply.",
     ["outbound_call_block", "state_changing_tool_block"]),
]

# Industry code → recommended default profile code (a suggestion surfaced at
# onboarding, never a lock). Unmapped industries default to "standard".
INDUSTRY_DEFAULT_PROFILES = {
    "healthcare": "healthcare",
    "banking": "finance",
    "insurance": "finance",
    "financial_services": "finance",
    "travel_hospitality": "travel_hospitality",
}

# Legacy approved-model registry shown on the AI Governance page. Entries must
# stay inside the governed provider matrix; out-of-matrix vendors are
# deprecated by reconcile_provider_governance, never deleted.
MODELS = [
    ("gpt-4o-mini", "OpenAI", "conversation", "approved", 0.0006, 320),
    ("gpt-4o", "OpenAI", "conversation", "approved", 0.005, 640),
    ("gpt-4.1-mini", "OpenAI", "classification", "approved", 0.0007, 300),
    ("text-embedding-3-small", "OpenAI", "embedding", "approved", 0.00002, 45),
    ("text-embedding-3-large", "OpenAI", "embedding", "approved", 0.00013, 60),
]

INTEGRATIONS = [
    ("Epic EHR", "Healthcare", "Appointment slots, patient verification and scheduling."),
    ("Zendesk", "Support", "Help-center articles as a live knowledge connector."),
    ("Salesforce Health Cloud", "CRM", "Sync caller context and escalation cases."),
    ("Slack", "Notifications", "Publish, rollback and alert notifications to channels."),
    ("Genesys Cloud", "Contact Center", "Warm-transfer escalations into agent queues."),
    ("Microsoft Teams", "Notifications", "Approval requests and daily digest cards."),
]

# Platform Health is probed live from the hosts/ports in .env
# (backend/core/service_health.py) — there is nothing to seed. Seeded rows
# here previously reported "API gateway — 100% uptime" whether or not
# anything was running.

# ISO 4217 display currencies. USD is the platform base currency; exchange
# rates are configured by Super Admin — never hardcoded here.
CURRENCIES = [
    # (code, name, symbol, decimal_places, is_base)
    ("USD", "US Dollar", "$", 2, True),
    ("INR", "Indian Rupee", "₹", 2, False),
    ("EUR", "Euro", "€", 2, False),
    ("GBP", "British Pound", "£", 2, False),
    ("AED", "UAE Dirham", "د.إ", 2, False),  # already a supported plan currency
]

# Provider prices. LLM/embedding rows are carried over from the existing
# approved-model registry (MODELS above). STT/TTS rows were verified against
# the providers' official pricing pages on 2026-07-27:
# - Sarvam (sarvam.ai/api-pricing, INR only, no official USD price):
#   STT (saarika:v2.5) and STT-Translate (saaras:v3) ₹30/hour of audio,
#   charged per second rounded up; TTS bulbul:v3 ₹30 per 10K characters
#   ("beta pricing") = ₹3 per 1K; bulbul:v2 ₹15/10K = ₹1.5 per 1K.
#   INR prices convert to USD via the Super-Admin-managed exchange rate at
#   usage time — rates are never hardcoded here.
# - OpenAI (developers.openai.com/api/docs/pricing, re-verified 2026-07-31):
#   text models are quoted per 1M tokens split three ways — input, cached
#   input and output — so they are priced with the matching split components
#   rather than one blended per-token rate (see OPENAI_LLM_PRICES).
#   Embeddings $0.02/1M (3-small) and $0.13/1M (3-large). Transcription is
#   per minute of audio: whisper-1 and gpt-4o-transcribe $0.006,
#   gpt-transcribe $0.0045, gpt-4o-mini-transcribe $0.003. TTS is per
#   character: tts-1 $15/1M, tts-1-hd $30/1M.
# - Deepgram (deepgram.com/pricing): Flux conversational STT pay-as-you-go
#   flux-general-multi $0.0078/min, flux-general-en $0.0065/min (verified
#   2026-08); nova-3 streaming pay-as-you-go
#   $0.0058/min multilingual (mono-English is $0.0048/min — the platform is
#   multilingual, so the multilingual rate applies); nova-2 streaming
#   $0.35/hour (FAQ: "unchanged rates for existing deployments"); true
#   per-second billing, no round-up.
# - ElevenLabs (elevenlabs.io/pricing/api): Flash/Turbo v2.5 API usage
#   $0.05 per 1K characters (0.5 credits/char); Eleven v3 bills 1 credit/char
#   — twice the Flash rate — so $0.10 per 1K characters. Billed in USD.
# Super Admin updates these under Platform Configuration → Provider Pricing;
# usage events snapshot the price they were costed with, so historical costs
# never change.
# OpenAI text-model list prices in USD per 1M tokens, as published:
# (model_code, input, cached input, output). A None cached rate means the
# model has no discounted cached-input tier — no row is created for it.
OPENAI_LLM_PRICES = [
    ("gpt-5.6-sol", "5.00", "0.50", "30.00"),
    ("gpt-5.6-terra", "2.00", "0.20", "12.00"),
    ("gpt-5.6-luna", "0.20", "0.02", "1.20"),
    ("gpt-5.1", "1.25", "0.125", "10.00"),
    ("gpt-5", "1.25", "0.125", "10.00"),
    ("gpt-5-mini", "0.25", "0.025", "2.00"),
    ("gpt-5-nano", "0.05", "0.005", "0.40"),
    ("gpt-4.1", "2.00", "0.50", "8.00"),
    ("gpt-4.1-mini", "0.40", "0.10", "1.60"),
    ("gpt-4.1-nano", "0.10", "0.025", "0.40"),
    ("gpt-4o", "2.50", "1.25", "10.00"),
    ("gpt-4o-mini", "0.15", "0.075", "0.60"),
]

PROVIDER_PRICING = [
    # (provider_code, capability, model_code, component, unit, unit_price, currency)
    # ── LLM ──────────────────────────────────────────────────────────────
    *[
        ("openai", "llm", model, component, "per_1m_tokens", price, "USD")
        for model, price_in, price_cached, price_out in OPENAI_LLM_PRICES
        for component, price in (
            ("input_tokens", price_in),
            ("cached_input_tokens", price_cached),
            ("output_tokens", price_out),
        )
        if price is not None
    ],
    # ── Embedding ────────────────────────────────────────────────────────
    ("openai", "embedding", "text-embedding-3-small", "tokens", "per_1m_tokens", "0.02", "USD"),
    ("openai", "embedding", "text-embedding-3-large", "tokens", "per_1m_tokens", "0.13", "USD"),
    # ── STT ──────────────────────────────────────────────────────────────
    ("sarvam", "stt", "saarika:v2.5", "audio_seconds", "per_hour", "30", "INR"),
    ("sarvam", "stt", "saaras:v3", "audio_seconds", "per_hour", "30", "INR"),
    ("openai", "stt", "whisper-1", "audio_seconds", "per_minute", "0.006", "USD"),
    ("openai", "stt", "gpt-transcribe", "audio_seconds", "per_minute", "0.0045", "USD"),
    ("openai", "stt", "gpt-4o-transcribe", "audio_seconds", "per_minute", "0.006", "USD"),
    ("openai", "stt", "gpt-4o-mini-transcribe", "audio_seconds", "per_minute", "0.003", "USD"),
    ("deepgram", "stt", "flux-general-multi", "audio_seconds", "per_minute", "0.0078", "USD"),
    ("deepgram", "stt", "flux-general-en", "audio_seconds", "per_minute", "0.0065", "USD"),
    ("deepgram", "stt", "nova-3", "audio_seconds", "per_minute", "0.0058", "USD"),
    ("deepgram", "stt", "nova-2", "audio_seconds", "per_hour", "0.35", "USD"),
    # ── TTS ──────────────────────────────────────────────────────────────
    ("sarvam", "tts", "bulbul:v3", "characters", "per_1k_characters", "3", "INR"),
    ("sarvam", "tts", "bulbul:v2", "characters", "per_1k_characters", "1.5", "INR"),
    ("elevenlabs", "tts", "eleven_flash_v2_5", "characters", "per_1k_characters", "0.05", "USD"),
    ("elevenlabs", "tts", "eleven_v3", "characters", "per_1k_characters", "0.10", "USD"),
    ("elevenlabs", "tts", "eleven_turbo_v2_5", "characters", "per_1k_characters", "0.05", "USD"),
    ("openai", "tts", "tts-1", "characters", "per_1m_characters", "15.00", "USD"),
    ("openai", "tts", "tts-1-hd", "characters", "per_1m_characters", "30.00", "USD"),
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
               "guardrails": 0, "guardrail_profiles": 0,
               "models": 0, "integrations": 0,
               "settings": 0, "templates": 0, "users": 0,
               "industries": 0, "countries": 0, "data_regions": 0,
               "ai_profiles": 0, "providers": 0,
               "currencies": 0, "provider_pricing": 0}
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

        countries: dict[str, Country] = {}
        for i, (iso2, iso3, name) in enumerate(COUNTRIES):
            row = db.scalar(select(Country).where(Country.iso2 == iso2))
            if row is None:
                row = Country(
                    iso2=iso2, iso3=iso3, name=name, region="Asia", sort_order=i,
                )
                db.add(row)
                created["countries"] += 1
            countries[name] = row
        db.flush()  # Assign numeric country IDs before creating Data Region FKs.

        for i, (code, name, country, region, desc) in enumerate(DATA_REGIONS):
            row = db.scalar(select(DataRegion).where(DataRegion.code == code))
            country_row = countries.get(country or "")
            if row is None:
                db.add(DataRegion(
                    id=new_id("dr"), code=code, name=name, country=country,
                    country_id=country_row.id if country_row else None,
                    region=region, description=desc, sort_order=i,
                    infrastructure_ready=False,
                ))
                created["data_regions"] += 1
            elif country_row is not None and not row.country_id:
                # Backfill only the structured country reference; user-edited
                # deployment/service settings remain untouched.
                row.country_id = country_row.id
                row.country = country_row.name

        for i, (code, name, cost, desc, overrides) in enumerate(AI_PROFILES):
            if db.scalar(select(AiConfigProfile).where(AiConfigProfile.code == code)) is None:
                profile = AiConfigProfile(
                    id=new_id("aip"), code=code, name=name, description=desc,
                    cost_category=cost, sort_order=i,
                    stt_provider="sarvam", stt_model="saaras:v3",
                    llm_provider="openai", llm_model="gpt-4o-mini",
                    tts_provider="sarvam", tts_model="bulbul:v3", default_voice="vp-sv-shubh",
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

        for i, (kind, code, name, needs_key, desc, provider_status) in enumerate(PROVIDERS):
            exists = db.scalar(
                select(ProviderDef).where(ProviderDef.kind == kind, ProviderDef.code == code)
            )
            if exists is None:
                db.add(ProviderDef(
                    id=new_id("prov"), kind=kind, code=code, name=name,
                    description=desc, requires_api_key=needs_key, sort_order=i,
                    secret_ref=f"env:{code.upper()}_API_KEY" if needs_key else None,
                    status=provider_status,
                ))
                created["providers"] += 1

        # The mock TTS pseudo-provider simulates voice cloning so the whole
        # clone flow is exercisable without external accounts (config-driven —
        # provider_catalog.supports_voice_cloning; mock is never listed in
        # production). Converge-only: an operator's explicit setting wins.
        mock_tts = db.scalar(select(ProviderDef).where(
            ProviderDef.kind == "tts", ProviderDef.code == "mock"))
        if mock_tts is not None and "voice_cloning" not in (mock_tts.config or {}):
            mock_tts.config = {**(mock_tts.config or {}), "voice_cloning": True}

        for vid, name, gender, langs, accent, styles, latency, premium, sample in VOICES:
            if db.get(VoiceProfile, vid) is None:
                db.add(VoiceProfile(
                    id=vid, name=name, gender=gender, languages=langs, accent=accent,
                    styles=styles, latency_ms=latency, premium=premium, sample_text=sample,
                    provider="platform",
                ))
                created["voices"] += 1

        # Provider model catalog + provider voices (Sarvam speakers, ElevenLabs voices).
        from backend.seeds.provider_catalog_seed import (
            reconcile_provider_governance,
            seed_provider_catalog,
        )

        created.update(seed_provider_catalog(db))
        # Converge provider/model activation to the governed matrix on every
        # bootstrap so long-lived databases pick up governance changes too.
        created.update(reconcile_provider_governance(db))

        guardrail_map: dict[str, Guardrail] = {}
        for code, name, category, desc, enforcement, enabled, mandatory in GUARDRAILS:
            row = db.scalar(select(Guardrail).where(
                or_(Guardrail.code == code, Guardrail.name == name)
            ))
            if row is None:
                row = Guardrail(
                    id=new_id("gr"), code=code, name=name, category=category,
                    description=desc, enforcement=enforcement, enabled=enabled,
                    is_mandatory=mandatory,
                )
                db.add(row)
                created["guardrails"] += 1
            else:
                # Converge safety-critical columns: pre-code rows get their
                # stable code, and mandatory platform rules are forced on —
                # a mandatory guardrail must never stay disabled or demoted.
                if row.code is None:
                    row.code = code
                if mandatory and not (row.is_mandatory and row.enabled):
                    row.is_mandatory = True
                    row.enabled = True
            guardrail_map[code] = row
        db.flush()  # profile rules reference guardrail ids

        for code, name, desc, rule_codes in GUARDRAIL_PROFILES:
            profile = db.scalar(select(GuardrailProfile).where(GuardrailProfile.code == code))
            if profile is None:
                profile = GuardrailProfile(
                    id=new_id("gp"), code=code, name=name, description=desc,
                )
                db.add(profile)
                db.flush()
                for rule_code in rule_codes:
                    guardrail = guardrail_map.get(rule_code)
                    if guardrail is not None:
                        db.add(GuardrailProfileRule(
                            id=new_id("gpr"), profile_id=profile.id,
                            guardrail_id=guardrail.id,
                        ))
                created["guardrail_profiles"] += 1
            # Existing profiles keep their operator-managed rule membership.

        # Recommended per-industry defaults — fill only where unset so a
        # Super Admin's explicit choice is never overwritten on re-seed.
        profile_ids = dict(db.execute(
            select(GuardrailProfile.code, GuardrailProfile.id)
            .where(GuardrailProfile.is_deleted.is_(False))
        ).all())
        for industry in db.scalars(select(Industry).where(Industry.is_deleted.is_(False))):
            if industry.default_guardrail_profile_id:
                continue
            profile_code = INDUSTRY_DEFAULT_PROFILES.get(industry.code, "standard")
            if profile_ids.get(profile_code):
                industry.default_guardrail_profile_id = profile_ids[profile_code]

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

        for i, (code, name, symbol, places, is_base) in enumerate(CURRENCIES):
            if db.scalar(select(Currency).where(Currency.code == code)) is None:
                db.add(Currency(
                    id=new_id("cur"), code=code, name=name, symbol=symbol,
                    decimal_places=places, is_base=is_base, sort_order=i,
                ))
                created["currencies"] += 1
        db.flush()  # provider_pricing rows reference currencies.code

        for provider_code, capability, model_code, component, unit, price, currency in PROVIDER_PRICING:
            exists = db.scalar(
                select(ProviderPricing).where(
                    ProviderPricing.provider_code == provider_code,
                    ProviderPricing.capability == capability,
                    ProviderPricing.model_code == model_code,
                    ProviderPricing.component == component,
                )
            )
            if exists is None:
                db.add(ProviderPricing(
                    id=new_id("ppr"), provider_code=provider_code, capability=capability,
                    model_code=model_code, component=component, unit=unit,
                    unit_price=Decimal(price), currency_code=currency,
                ))
                created["provider_pricing"] += 1

        for name, category, desc in INTEGRATIONS:
            if db.scalar(select(Integration).where(Integration.name == name)) is None:
                db.add(Integration(id=new_id("ig"), name=name, category=category, description=desc))
                created["integrations"] += 1

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
