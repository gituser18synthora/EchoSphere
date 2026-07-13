"""
Final end-to-end config test: adapters, prompts, context/memory, advanced settings.
Seeds MongoDB with model_providers + full voicebot config, then verifies load and validation.

Run from the voicebot directory:
    python scripts/run_final_config_test.py

Seeds voicebot_configs with the same field values as the admin UI / Mongo (see FULL_VOICEBOT_CONFIG).
Then run mic_test: python -m voicebot.test_runner.mic_test --voicebot-id vb_4dfa73dc775b

Also add the repo parent to sys.path so optional `import voicebot` style imports work elsewhere.

Requirements: pip install -r requirements.txt (pytz, motor, pydantic, etc.). MongoDB running.
Optional: Redis (script still runs if Redis is down).
View data: MongoDB Compass or mongosh — connect to MONGO_URI, database from .env (e.g. VoicebotDB).
"""

import asyncio
import sys
from pathlib import Path

_voicebot_root = Path(__file__).resolve().parents[1]
_repo_root = _voicebot_root.parent
# voicebot/ on path: config, config_layer, adapters, ...
sys.path.insert(0, str(_voicebot_root))
# Synthora-AI/ on path: allows `import voicebot` when running other entrypoints
sys.path.insert(0, str(_repo_root))

from config.settings import Settings
from config_layer.db import (
    MongoDB,
    COLLECTION_VOICEBOTS,
    COLLECTION_VOICEBOT_CONFIGS,
    COLLECTION_MODEL_PROVIDERS,
    COLLECTION_PHONE_NUMBERS,
    create_indexes,
)
from config_layer.exceptions import OutsideWorkingHoursError
from config_layer.loader import ConfigLoader
from config_layer.validator import ConfigValidator

# --- Model providers (adapters) — same as seed_model_providers.py ---
PROVIDERS = [
    {"provider_id": "openai", "type": "llm", "adapter_class": "adapters.llm.openai_adapter.OpenAILLMAdapter", "display_name": "OpenAI", "models": ["gpt-4o", "gpt-4o-mini"], "min_tier": "starter", "is_active": True},
    {"provider_id": "anthropic", "type": "llm", "adapter_class": "adapters.llm.anthropic_adapter.AnthropicLLMAdapter", "display_name": "Anthropic Claude", "models": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"], "min_tier": "pro", "is_active": True},
    {"provider_id": "google", "type": "llm", "adapter_class": "adapters.llm.google_adapter.GoogleLLMAdapter", "display_name": "Google Gemini", "models": ["gemini-2.0-flash"], "min_tier": "pro", "is_active": True},
    {"provider_id": "deepgram", "type": "stt", "adapter_class": "adapters.stt.deepgram_adapter.DeepgramSTTAdapter", "display_name": "Deepgram Nova-2", "models": ["nova-2"], "min_tier": "starter", "is_active": True},
    {"provider_id": "whisper", "type": "stt", "adapter_class": "adapters.stt.whisper_adapter.WhisperSTTAdapter", "display_name": "OpenAI Whisper", "models": ["whisper-1"], "min_tier": "starter", "is_active": True},
    {"provider_id": "assemblyai", "type": "stt", "adapter_class": "adapters.stt.assemblyai_adapter.AssemblyAISTTAdapter", "display_name": "AssemblyAI", "models": ["best"], "min_tier": "pro", "is_active": True},
    {"provider_id": "sarvam_stt", "type": "stt", "adapter_class": "adapters.stt.sarvam_adapter.SarvamSTTAdapter", "display_name": "Sarvam AI STT", "models": ["saarika:v2.5"], "min_tier": "starter", "is_active": True},
    {"provider_id": "elevenlabs", "type": "tts", "adapter_class": "adapters.tts.elevenlabs_adapter.ElevenLabsTTSAdapter", "display_name": "ElevenLabs", "models": ["eleven_turbo_v2"], "min_tier": "starter", "is_active": True},
    {"provider_id": "azure_tts", "type": "tts", "adapter_class": "adapters.tts.azure_adapter.AzureTTSAdapter", "display_name": "Azure Speech", "models": ["neural"], "min_tier": "starter", "is_active": True},
    {"provider_id": "google_tts", "type": "tts", "adapter_class": "adapters.tts.google_adapter.GoogleTTSAdapter", "display_name": "Google TTS", "models": ["standard", "wavenet"], "min_tier": "starter", "is_active": True},
    {"provider_id": "sarvam_tts", "type": "tts", "adapter_class": "adapters.tts.sarvam_adapter.SarvamTTSAdapter", "display_name": "Sarvam AI TTS", "models": ["bulbul:v2"], "min_tier": "starter", "is_active": True},
]

# --- FULL_VOICEBOT_CONFIG: exact admin-UI / MongoDB voicebot_configs shape (UI strings, no _id).
# ConfigLoader applies config_layer.mongo_normalize before VoicebotConfig.model_validate.
# Omitted keys (personality, goals, intent_config, actions_automation) use VoicebotConfig defaults;
# goals.crm_* is synced from top-level crm_* in the normalizer.

FULL_VOICEBOT_CONFIG = {
    "voicebot_id": "vb_4dfa73dc775b",
    "tenant_id": "tenant-1",
    "name": "Sarah - Support AI",
    "business_name": "Acme Insurance",
    "status": "draft",
    "phone_number_id": "+15550001111",
    "crm_integration_type": "Salesforce",
    "crm_config": {
        "crm_account_id": "acme_001",
        "api_key": "sk-xxxx",
        "webhook_url": "https://acme.com/webhook",
    },
    "escalation": {
        "max_call_duration": 3,
        "fallback_action": "Transfer to Agent",
        "transfer_message": (
            "Please hold while I connect you to a human agent."
        ),
        "transfer_conditions": "",
    },
    "availability": {
        "enable_24x7": False,
        "working_hours_start": "09:00",
        "working_hours_end": "18:00",
        "timezone": "UTC",
    },
    "persona_behaviour": {
        "agent_role": "Insurance Advisor",
        "empathy_level": "Low",
        "enable_confirmation_prompts": False,
        "enable_proactive_assistance": False,
        "enable_response_summaries": False,
        "escalation_threshold": "Low",
        "formatting_style": "Structured",
        "greeting_style": "Formal",
        "industry_context": "Banking",
        "interrupt_handling": "Allow Interruption",
        "language_simplicity": "Basic",
        "personality_type": "Professional",
        "response_length": "Short",
    },
    "engine": {
        "confidence_threshold": 0.75,
        "context_recall_between_calls": True,
        "enable_rag": True,
        "fallback_model_id": "OPT-40 Mini",
        "guardrails": (
            "Do not share confidential data. Never commit pricing without verification."
        ),
        "llm_model_id": "gpt-4o-mini",
        "long_term_crm_linked": True,
        "max_response_latency": 1.5,
        "memory_expiry_days": 30,
        "primary_objectives": (
            "1. Answer customer queries.\n"
            "2. Capture lead information.\n"
            "3. Transfer complex issues to human agents."
        ),
        "response_style": "Concise & Direct",
        "short_term_memory_scope": "Session Only",
        "stt_provider_id": "sarvam_stt",
        "system_role": (
            "You are a professional AI Voice Assistant representing the business,"
        ),
        "tts_provider_id": "DevenLabs sarvam_tts",
        "voice_id": "anushka",
        "voice_pitch": 1,
        "voice_speed": 1,
    },
    "conversation_intelligence": {
        "auto_language_detection": True,
        "below_threshold_action": "Ask Clarying Question",
        "context_window_tokens": "4006",
        "cross_session_recall": True,
        "fallback_language": "English",
        "intent_detection_model": "LLM Native Intent Parsing",
        "knowledge_source_priority": [
            "CRM Customer Data",
            "RAG Knowledge Base",
            "FAQ Structured Answers",
            "LLM General Knowledge",
        ],
        "memory_expiry_days": 150,
        "memory_type": "Structured Memory (CRM Fields)",
        "min_confidence_threshold": 75,
        "primary_language": "English",
        "response_depth": "Concise",
        "sentiment_adaptation": True,
        "smart_clarification": True,
    },
    "caller_authentication": {
        "authentication_mode": "Silent (check CRM without asking)",
        "enable_authentication": True,
        "failure_handling": {
            "failure_message": (
                "Sorry, we could not verify your identity. Please contact support or try again later."
            ),
            "max_verification_attempts": 2,
            "on_failure_action": "Transfer to Human",
        },
        "verification_fields": [
            {
                "field_name": "Account Number",
                "verify_against": "Salesforce",
                "ask_as": "Voice Prompt",
                "required": True,
            },
            {
                "field_name": "Date of Birth",
                "verify_against": "Salesforce",
                "ask_as": "Voice Prompt",
                "required": True,
            },
            {
                "field_name": "Policy Number",
                "verify_against": "Custom API",
                "ask_as": "Voice Prompt",
                "required": False,
            },
            {
                "field_name": "PAN Number",
                "verify_against": "Custom API",
                "ask_as": "Voice Prompt",
                "required": True,
            },
        ],
    },
    "call_data_extraction": {
        "custom_fields": [
            {
                "field_name": "Policy Number",
                "data_type": "String",
                "extraction_method": "Entity extraction during call",
                "extraction_prompt": "",
                "required": True,
            },
            {
                "field_name": "Claim Amount",
                "data_type": "Number",
                "extraction_method": "Entity extraction during call",
                "extraction_prompt": "",
                "required": True,
            },
            {
                "field_name": "Preferred Contact Time",
                "data_type": "String",
                "extraction_method": "Entity extraction during call",
                "extraction_prompt": "",
                "required": False,
            },
        ],
        "standard_fields": {
            "call_duration": True,
            "call_intent_reason": True,
            "caller_phone_number": True,
            "customer_name": True,
            "goal_outcome": True,
            "language_detected": True,
            "sentiment": True,
        },
        "storage_destinations": [
            {
                "destination": "Salesforce",
                "destination_type": "CRM",
                "enabled": True,
            },
            {
                "destination": "HubSpot",
                "destination_type": "CRM",
                "enabled": True,
            },
            {
                "destination": "Zendesk",
                "destination_type": "Ticketing",
                "enabled": True,
            },
            {
                "destination": "Internal Database",
                "destination_type": "Built-in",
                "enabled": True,
            },
            {
                "destination": "Custom Webhook",
                "destination_type": "Enterprise",
                "enabled": True,
            },
        ],
    },
}

SEED_VOICEBOT_ID = FULL_VOICEBOT_CONFIG["voicebot_id"]
SEED_TEST_PHONE = "+15550001111"


async def main():
    settings = Settings()
    db_name = getattr(settings, "mongo_db_name", "VoicebotDB") or "VoicebotDB"

    print("=" * 60)
    print("FINAL CONFIG TEST: Adapters + Prompts + Settings -> MongoDB")
    print("=" * 60)

    print("\n1. Connecting to MongoDB...")
    await MongoDB.connect()
    db = MongoDB.db()
    print(f"   Connected to DB: {db_name} [OK]")

    print("\n2. Creating indexes...")
    await create_indexes()
    print("   Indexes created.")

    print("\n3. Seeding model_providers (adapters)...")
    for p in PROVIDERS:
        await db[COLLECTION_MODEL_PROVIDERS].update_one(
            {"provider_id": p["provider_id"], "type": p["type"]},
            {"$set": p},
            upsert=True,
        )
    print(f"   Upserted {len(PROVIDERS)} providers (LLM, STT, TTS).")

    print(f"\n4. Invalidating Redis cache for {SEED_VOICEBOT_ID} (so load picks up new config)...")
    loader = ConfigLoader()
    try:
        await loader._cache.invalidate(SEED_VOICEBOT_ID)
        print("   Cache invalidated.")
    except Exception as e:
        print(f"   Redis not available: {e} (continuing)")

    print("\n5. Saving voicebot + full config to MongoDB...")
    # voicebot_configs keeps status from FULL_VOICEBOT_CONFIG (e.g. draft). Incoming-call
    # resolution requires voicebots.status == active, so we set that row to active for local tests.
    await db[COLLECTION_VOICEBOTS].update_one(
        {"voicebot_id": SEED_VOICEBOT_ID},
        {"$set": {
            "voicebot_id": SEED_VOICEBOT_ID,
            "tenant_id": FULL_VOICEBOT_CONFIG["tenant_id"],
            "name": FULL_VOICEBOT_CONFIG["name"],
            "business_name": FULL_VOICEBOT_CONFIG["business_name"],
            "status": "active",
            "phone_number_id": FULL_VOICEBOT_CONFIG["phone_number_id"],
        }},
        upsert=True,
    )
    await db[COLLECTION_VOICEBOT_CONFIGS].update_one(
        {"voicebot_id": SEED_VOICEBOT_ID},
        {"$set": FULL_VOICEBOT_CONFIG},
        upsert=True,
    )
    print(f"   voicebots: 1 ({SEED_VOICEBOT_ID}, status active for inbound test)")
    print(f"   voicebot_configs: 1 (admin-UI-shaped document, status may be draft)")

    print("\n6. Saving phone number mapping...")
    await db[COLLECTION_PHONE_NUMBERS].update_one(
        {"phone_number": SEED_TEST_PHONE},
        {"$set": {
            "phone_number": SEED_TEST_PHONE,
            "voicebot_id": SEED_VOICEBOT_ID,
            "tenant_id": FULL_VOICEBOT_CONFIG["tenant_id"],
            "status": "active",
        }},
        upsert=True,
    )
    print(f"   {SEED_TEST_PHONE} -> {SEED_VOICEBOT_ID}")

    print("\n7. ConfigLoader: load by voicebot_id...")
    config = await loader.load(SEED_VOICEBOT_ID)
    print(f"   voicebot_id: {config.voicebot_id}")
    print(
        f"   engine.llm_provider_id / llm_model_id: "
        f"{config.engine.llm_provider_id} / {config.engine.llm_model_id}",
    )
    print(f"   engine.system_role (first 50 chars): {config.engine.system_role[:50]}...")
    print(f"   engine.short_term_memory_scope: {config.engine.short_term_memory_scope}")
    print(f"   engine.long_term_crm_linked: {config.engine.long_term_crm_linked}")
    print(f"   engine.memory_expiry_days: {config.engine.memory_expiry_days}")
    print(f"   engine.confidence_threshold: {config.engine.confidence_threshold}")
    print(f"   goals.crm_integration_type: {config.goals.crm_integration_type}")
    print(f"   engine.stt_provider_id: {config.engine.stt_provider_id}")
    print(f"   engine.tts_provider_id: {config.engine.tts_provider_id}")
    print(
        f"   conversation_intelligence.intent_detection_model: "
        f"{config.conversation_intelligence.intent_detection_model}",
    )
    print(f"   admin sections present: persona_behaviour={config.persona_behaviour is not None}, "
          f"caller_auth={config.caller_authentication is not None}, "
          f"extraction={config.call_data_extraction is not None}, "
          f"actions={config.actions_automation is not None}")

    print("\n8. ConfigLoader: load_for_incoming_call(phone)...")
    try:
        config_by_phone = await loader.load_for_incoming_call(SEED_TEST_PHONE)
        print(f"   Same config: {config_by_phone.voicebot_id == config.voicebot_id}")
    except OutsideWorkingHoursError as e:
        print(f"   Skipped (outside seeded working hours): {e}")

    print("\n9. ConfigValidator...")
    validator = ConfigValidator()
    errors = validator.validate(config)
    if errors:
        for e in errors:
            print(f"   [{e.severity}] {e.field}: {e.message}")
    else:
        print("   No errors — config is valid and ready to launch.")

    print("\n10. Redis cache (optional)...")
    try:
        await loader._cache.set(SEED_VOICEBOT_ID, config.to_cache_dict())
        print(f"   Cache set for {SEED_VOICEBOT_ID} (Redis OK).")
    except Exception as e:
        print(f"   Redis not available or error: {e} (Mongo load still works).")

    print("\n--- Collections in MongoDB (what you will see) ---")
    for col in [COLLECTION_VOICEBOTS, COLLECTION_VOICEBOT_CONFIGS, COLLECTION_MODEL_PROVIDERS, COLLECTION_PHONE_NUMBERS]:
        n = await db[col].count_documents({})
        print(f"   {col}: {n} document(s)")

    print("\n--- How to view in MongoDB ---")
    print(f"   Compass / mongosh: connect to {getattr(settings, 'mongo_uri', 'MONGO_URI from .env')}")
    print(f"   Database: {db_name}")
    print("   Collections: voicebots, voicebot_configs, model_providers, phone_numbers")
    print(f"   Example: db.voicebot_configs.findOne({{ voicebot_id: '{SEED_VOICEBOT_ID}' }})")

    await MongoDB.disconnect()
    print("\nDone. Config built, saved to Mongo, and verified via ConfigLoader + ConfigValidator.")


if __name__ == "__main__":
    asyncio.run(main())
