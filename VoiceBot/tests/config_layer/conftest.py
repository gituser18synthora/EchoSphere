"""Fixtures for config_layer tests."""

import pytest

from config_layer.models import (
    AvailabilityConfig,
    ConversationIntelligenceConfig,
    EngineConfig,
    EscalationConfig,
    GoalsConfig,
    PersonalityConfig,
    VoicebotConfig,
    VoicebotStatus,
)


def _valid_config_dict(overrides=None):
    base = {
        "voicebot_id": "vb-1",
        "tenant_id": "t-1",
        "name": "Test Bot",
        "business_name": "Test Co",
        "status": "active",
        "phone_number_id": None,
        "personality": {
            "greeting_message": "Hello!",
            "conversation_style": "friendly_casual",
            "knowledge_base_source": "rag",
            "knowledge_base_id": None,
        },
        "goals": {
            "book_appointments": True,
            "capture_leads": False,
            "answer_faqs": True,
            "route_to_human": False,
            "send_sms_followup": False,
            "crm_integration_type": "none",
            "crm_config": {},
        },
        "intent_config": {
            "always_present_intents": {
                "general_query": "General question or information request",
                "greeting": "Hello or starting conversation",
                "goodbye": "Ending conversation",
            },
            "goal_intent_descriptions": {
                "book_appointment": "Schedule or book an appointment",
                "capture_lead": "New prospect or contact request",
            },
            "goal_flag_map": {
                "book_appointment": "book_appointments",
                "capture_lead": "capture_leads",
            },
        },
        "escalation": {
            "transfer_conditions": "agent, human",
            "max_call_duration": 10,
            "fallback_action": "transfer_to_agent",
            "transfer_message": "",
        },
        "availability": {
            "working_hours_start": "09:00",
            "working_hours_end": "17:00",
            "timezone": "UTC",
            "enable_24x7": False,
        },
        "engine": {
            "llm_provider_id": "openai",
            "llm_model_id": "gpt-4o",
            "stt_provider_id": "deepgram",
            "tts_provider_id": "elevenlabs",
            "voice_id": "voice-1",
            "voice_speed": 1.0,
            "voice_pitch": 1.0,
            "system_role": "You are helpful.",
            "primary_objectives": "Help the user.",
            "guardrails": "",
            "response_style": "concise_direct",
            "short_term_memory_scope": "session_only",
            "context_recall_between_calls": True,
            "memory_expiry_days": 180,
            "long_term_crm_linked": True,
            "enable_rag": True,
            "confidence_threshold": 0.75,
            "max_response_latency": 3.0,
            "fallback_provider_id": "openai",
            "fallback_model_id": "gpt-4o-mini",
        },
        "conversation_intelligence": {
            "primary_language": "en",
            "auto_language_detection": True,
            "fallback_language": "en",
            "intent_detection_model": "llm_native",
            "min_confidence_threshold": 0.75,
            "below_threshold_action": "ask_clarifying",
            "knowledge_source_priority": ["crm", "rag", "faq", "llm"],
            "context_window_tokens": 4096,
            "cross_session_recall": True,
            "memory_type": "structured",
            "memory_expiry_days": 150,
            "response_depth": "concise",
            "sentiment_adaptation": True,
            "smart_clarification": True,
        },
    }
    if overrides:
        for k, v in overrides.items():
            if isinstance(v, dict) and k in base and isinstance(base[k], dict):
                base[k] = {**base[k], **v}
            else:
                base[k] = v
    return base


@pytest.fixture
def valid_config_dict():
    return _valid_config_dict()


@pytest.fixture
def valid_config(valid_config_dict):
    return VoicebotConfig.model_validate(valid_config_dict)
