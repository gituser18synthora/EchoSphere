"""Tests for GuardrailsEngine: blocklist, length, language, confidential, blocklist parse."""

import pytest

from orchestrator.call_state import CallState, Turn
from orchestrator.guardrails import GuardrailsEngine
from tests.orchestrator.conftest import _valid_config_dict


@pytest.fixture
def call_state():
    return CallState(
        call_id="c1",
        voicebot_id="vb-1",
        caller_phone="+911234567890",
        tenant_id="t1",
    )


def test_blocked_phrase_caught_case_insensitive(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"engine": {"guardrails": "Never discuss competitors."}})
    config = VoicebotConfig.model_validate(d)
    engine = GuardrailsEngine(config)
    result = engine.check("We should not discuss COMPETITORS in this call.", call_state)
    assert result.passed is False
    assert result.violation_type == "blocklist"
    assert "competitors" in (result.violation_detail or "").lower()


def test_clean_response_returns_passed_true(call_state):
    from config_layer.models import VoicebotConfig
    config = VoicebotConfig.model_validate(_valid_config_dict())
    engine = GuardrailsEngine(config)
    result = engine.check("I would be happy to help you with that.", call_state)
    assert result.passed is True
    assert result.violation_type is None


def test_concise_150_words_length_violation(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"conversation_intelligence": {"response_depth": "concise"}})
    config = VoicebotConfig.model_validate(d)
    engine = GuardrailsEngine(config)
    long_text = "word " * 150
    result = engine.check(long_text, call_state)
    assert result.passed is False
    assert result.violation_type == "length"


def test_concise_50_words_passes(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"conversation_intelligence": {"response_depth": "concise"}})
    config = VoicebotConfig.model_validate(d)
    engine = GuardrailsEngine(config)
    short_text = "word " * 50
    result = engine.check(short_text, call_state)
    assert result.passed is True


def test_detailed_200_words_passes(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"conversation_intelligence": {"response_depth": "detailed"}})
    config = VoicebotConfig.model_validate(d)
    engine = GuardrailsEngine(config)
    long_text = "word " * 200
    result = engine.check(long_text, call_state)
    assert result.passed is True


def test_hindi_mismatch_detected(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict(
        {"conversation_intelligence": {"auto_language_detection": False}}
    )
    config = VoicebotConfig.model_validate(d)
    call_state.detected_language = "hi"
    engine = GuardrailsEngine(config)
    result = engine.check("This is all English text with no Devanagari.", call_state)
    assert result.passed is False
    assert result.violation_type == "language"
    assert result.critical is False


def test_hindi_english_passes_when_auto_language_detection_on(call_state):
    from config_layer.models import VoicebotConfig
    config = VoicebotConfig.model_validate(_valid_config_dict())
    call_state.detected_language = "hi"
    engine = GuardrailsEngine(config)
    result = engine.check(
        "Thank you, I can help with that in English.",
        call_state,
    )
    assert result.passed is True


def test_english_detected_language_skips_language_check(call_state):
    from config_layer.models import VoicebotConfig
    config = VoicebotConfig.model_validate(_valid_config_dict())
    call_state.detected_language = "en"
    engine = GuardrailsEngine(config)
    result = engine.check("Any English response here.", call_state)
    assert result.passed is True or result.violation_type != "language"


def test_phone_number_caught_when_guardrails_confidential(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"engine": {"guardrails": "Do not share confidential data."}})
    config = VoicebotConfig.model_validate(d)
    engine = GuardrailsEngine(config)
    result = engine.check("Please call me at 9876543210.", call_state)
    assert result.passed is False
    assert result.violation_type == "confidential"


def test_phone_number_allowed_when_caller_already_shared_it(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"engine": {"guardrails": "Do not share confidential data."}})
    config = VoicebotConfig.model_validate(d)
    call_state.turns.append(
        Turn(
            turn_id=0,
            role="user",
            content="My contact is 9015214225 please note it.",
        )
    )
    engine = GuardrailsEngine(config)
    result = engine.check(
        "You provided the contact number 9015214225.",
        call_state,
        caller_utterance="What number did I give you?",
    )
    assert result.passed is True


def test_phone_allowed_when_only_in_transcript_dialogue(call_state):
    """Echo context uses transcript_as_dialogue() so prior Caller lines qualify."""
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"engine": {"guardrails": "Do not share confidential data."}})
    config = VoicebotConfig.model_validate(d)
    call_state.turns.append(
        Turn(
            turn_id=0,
            role="user",
            content="Please save 8123456789 as my mobile.",
        )
    )
    engine = GuardrailsEngine(config)
    result = engine.check(
        "Saved. Your mobile is 8123456789.",
        call_state,
        caller_utterance="Did you get my mobile?",
    )
    assert result.passed is True


def test_strict_mode_blocks_phone_even_when_in_transcript(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict(
        {
            "engine": {
                "guardrails": "Do not share confidential data.",
                "guardrails_config": {"allow_user_provided_data": False},
            }
        }
    )
    config = VoicebotConfig.model_validate(d)
    call_state.turns.append(
        Turn(
            turn_id=0,
            role="user",
            content="My contact is 9015214225 please note it.",
        )
    )
    engine = GuardrailsEngine(config)
    result = engine.check(
        "You provided the contact number 9015214225.",
        call_state,
        caller_utterance="What number did I give you?",
    )
    assert result.passed is False
    assert result.violation_type == "confidential"


def test_account_number_allowed_when_in_transcript(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"engine": {"guardrails": "Keep confidential information private."}})
    config = VoicebotConfig.model_validate(d)
    call_state.turns.append(
        Turn(
            turn_id=0,
            role="user",
            content="My billing account is 88776655.",
        )
    )
    engine = GuardrailsEngine(config)
    result = engine.check(
        "Thanks — I see account 88776655 here.",
        call_state,
        caller_utterance="Which account did I mention?",
    )
    assert result.passed is True


def test_account_number_blocked_when_not_in_transcript(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"engine": {"guardrails": "Keep confidential information private."}})
    config = VoicebotConfig.model_validate(d)
    engine = GuardrailsEngine(config)
    result = engine.check(
        "Your account acc# 9911223344 is updated.",
        call_state,
        caller_utterance="Anything else?",
    )
    assert result.passed is False
    assert result.violation_type == "confidential"


def test_phone_allowed_when_it_came_from_caller_graph_in_system_prompt(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"engine": {"guardrails": "Do not share confidential data."}})
    config = VoicebotConfig.model_validate(d)
    # System prompt contains graph node echoed from a previous call
    call_state.system_prompt = (
        "CALLER CONTEXT:\n- Contact Number: 9015214225\n- Name: Kartik Sharma"
    )
    engine = GuardrailsEngine(config)
    result = engine.check(
        "The contact number you provided is 9015214225.",
        call_state,
        caller_utterance="What was my contact number?",
    )
    assert result.passed is True


def test_email_caught_when_confidential_in_guardrails(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"engine": {"guardrails": "Keep confidential information private."}})
    config = VoicebotConfig.model_validate(d)
    engine = GuardrailsEngine(config)
    result = engine.check("My email is user@example.com for follow-up.", call_state)
    assert result.passed is False
    assert result.violation_type == "confidential"


def test_no_confidential_check_when_guardrails_silent(call_state):
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"engine": {"guardrails": ""}})
    config = VoicebotConfig.model_validate(d)
    engine = GuardrailsEngine(config)
    result = engine.check("Call me at 9876543210 or user@example.com.", call_state)
    assert result.passed is True


def test_blocklist_parse_never_discuss_competitors():
    from config_layer.models import VoicebotConfig
    d = _valid_config_dict({"engine": {"guardrails": "Never discuss competitors."}})
    config = VoicebotConfig.model_validate(d)
    engine = GuardrailsEngine(config)
    assert "discuss competitors" in engine._blocklist
