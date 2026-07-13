"""Fixtures for orchestrator tests."""

import pytest

from config_layer.models import VoicebotConfig


def _valid_config_dict(overrides=None):
    base = {
        "voicebot_id": "vb-1",
        "tenant_id": "t-1",
        "name": "Test Bot",
        "business_name": "Test Co",
        "status": "active",
        "phone_number_id": None,
        "personality": {
            "greeting_message": "Hello! How can I help?",
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
                "general_query": "Caller is asking a general question or requesting information",
                "escalation_request": "Caller explicitly asks to speak to a human, manager, or supervisor",
                "off_topic": "Caller is talking about something unrelated to the business",
                "greeting": "Caller is saying hello or starting the conversation",
                "goodbye": "Caller is ending the conversation or saying bye",
                "goal_abandon": "Caller says forget it, never mind, cancel, stop current action",
                "privacy_request": "Caller asks to delete data, forget information, right to erasure.",
            },
            "goal_intent_descriptions": {
                "book_appointment": "Caller wants to schedule, book, or reserve an appointment",
                "capture_lead": "Caller is a new prospect expressing interest or wanting to be contacted",
                "answer_faq": "Caller has a specific question answerable from the knowledge base",
                "route_to_human": "Caller wants to be transferred to a specific department or person",
                "send_followup": "Caller requests information to be sent via SMS or WhatsApp",
            },
            "goal_flag_map": {
                "book_appointment": "book_appointments",
                "capture_lead": "capture_leads",
                "answer_faq": "answer_faqs",
                "route_to_human": "route_to_human",
                "send_followup": "send_sms_followup",
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
            "stt_provider_id": "sarvam_stt",
            "tts_provider_id": "sarvam_tts",
            "voice_id": "voice-1",
            "voice_speed": 1.0,
            "voice_pitch": 1.0,
            "system_role": "You are helpful.",
            "primary_objectives": "Help the user.",
            "guardrails": "Never discuss competitors.",
            "response_style": "concise_direct",
            "short_term_memory_scope": "session_only",
            "memory_expiry_days": 180,
            "long_term_crm_linked": True,
            "context_recall_between_calls": True,
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
