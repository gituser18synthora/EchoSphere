"""Tests for IntentEngine: prompt building, JSON parsing, fallbacks."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.call_state import CallState, IntentResult
from orchestrator.intent_engine import IntentEngine


@pytest.fixture
def mock_llm():
    m = MagicMock()
    m.generate = AsyncMock(
        return_value=MagicMock(
            text='{"intent": "general_query", "confidence": 0.85, "sentiment": "neutral"}'
        )
    )
    return m


@pytest.fixture
def call_state():
    return CallState(
        call_id="c1",
        voicebot_id="vb-1",
        caller_phone="+911234567890",
        tenant_id="t1",
    )


def test_prompt_includes_all_always_present_intents(valid_config, mock_llm):
    valid_config.goals.book_appointments = False
    valid_config.goals.capture_leads = False
    valid_config.goals.answer_faqs = False
    valid_config.goals.route_to_human = False
    valid_config.goals.send_sms_followup = False
    engine = IntentEngine(mock_llm, valid_config)
    prompt = engine._build_prompt("hello")
    ap = valid_config.intent_config.always_present_intents
    for intent in ap:
        assert intent in prompt
        assert ap[intent] in prompt


def test_book_appointment_present_when_enabled(valid_config, mock_llm):
    valid_config.goals.book_appointments = True
    engine = IntentEngine(mock_llm, valid_config)
    prompt = engine._build_prompt("book a slot")
    assert "book_appointment" in prompt
    assert valid_config.intent_config.goal_intent_descriptions["book_appointment"] in prompt


def test_book_appointment_absent_when_disabled(valid_config, mock_llm):
    valid_config.goals.book_appointments = False
    valid_config.goals.capture_leads = False
    valid_config.goals.answer_faqs = False
    valid_config.goals.route_to_human = False
    valid_config.goals.send_sms_followup = False
    engine = IntentEngine(mock_llm, valid_config)
    prompt = engine._build_prompt("book a slot")
    assert "book_appointment" not in prompt


def test_all_five_goal_intents_present_when_all_enabled(valid_config, mock_llm):
    valid_config.goals.book_appointments = True
    valid_config.goals.capture_leads = True
    valid_config.goals.answer_faqs = True
    valid_config.goals.route_to_human = True
    valid_config.goals.send_sms_followup = True
    engine = IntentEngine(mock_llm, valid_config)
    prompt = engine._build_prompt("help me")
    for intent in ("book_appointment", "capture_lead", "answer_faq", "route_to_human", "send_followup"):
        assert intent in prompt


def test_valid_json_parsed_correctly(mock_llm, valid_config):
    engine = IntentEngine(mock_llm, valid_config)
    result = engine._parse_response(
        '{"intent": "greeting", "confidence": 0.92, "sentiment": "positive"}'
    )
    assert result.intent == "greeting"
    assert result.confidence == 0.92
    assert result.sentiment == "positive"


def test_confidence_clamped_to_zero_one(mock_llm, valid_config):
    engine = IntentEngine(mock_llm, valid_config)
    r = engine._parse_response(
        '{"intent": "general_query", "confidence": 1.5, "sentiment": "neutral"}'
    )
    assert r.confidence == 1.0
    r = engine._parse_response(
        '{"intent": "general_query", "confidence": -0.2, "sentiment": "neutral"}'
    )
    assert r.confidence == 0.0


def test_invalid_sentiment_defaults_to_neutral(mock_llm, valid_config):
    engine = IntentEngine(mock_llm, valid_config)
    r = engine._parse_response(
        '{"intent": "greeting", "confidence": 0.8, "sentiment": "invalid"}'
    )
    assert r.sentiment == "neutral"


def test_malformed_json_returns_general_query_fallback(mock_llm, valid_config):
    engine = IntentEngine(mock_llm, valid_config)
    r = engine._parse_response("not json at all")
    assert r.intent == "general_query"
    assert r.confidence == 0.5
    assert r.sentiment == "neutral"


def test_markdown_fences_stripped_before_parsing(mock_llm, valid_config):
    engine = IntentEngine(mock_llm, valid_config)
    r = engine._parse_response(
        '```json\n{"intent": "goodbye", "confidence": 0.88, "sentiment": "neutral"}\n```'
    )
    assert r.intent == "goodbye"


@pytest.mark.asyncio
async def test_classify_returns_intent_result(mock_llm, valid_config, call_state):
    mock_llm.generate.return_value = MagicMock(
        text='{"intent": "book_appointment", "confidence": 0.91, "sentiment": "neutral"}'
    )
    engine = IntentEngine(mock_llm, valid_config)
    result = await engine.classify("I want to book an appointment", call_state)
    assert isinstance(result, IntentResult)
    assert result.intent == "book_appointment"
    assert result.confidence == 0.91
