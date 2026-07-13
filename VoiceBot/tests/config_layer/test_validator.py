"""Tests for ConfigValidator: valid config, goals, engine, voice, prompt, availability, escalation."""

import pytest

from config_layer.models import VoicebotConfig
from config_layer.validator import ConfigValidator, ValidationError


@pytest.mark.asyncio
async def test_fully_valid_config_returns_empty_errors(valid_config):
    validator = ConfigValidator()
    errors = validator.validate(valid_config)
    assert errors == []


def test_no_goals_enabled_error(valid_config_dict):
    valid_config_dict["goals"] = {
        "book_appointments": False,
        "capture_leads": False,
        "answer_faqs": False,
        "route_to_human": False,
        "send_sms_followup": False,
        "crm_integration_type": "none",
        "crm_config": {},
    }
    config = VoicebotConfig.model_validate(valid_config_dict)
    validator = ConfigValidator()
    errors = validator.validate(config)
    assert len(errors) >= 1
    assert any(e.field == "goals" for e in errors)


def test_empty_system_role_error(valid_config_dict):
    valid_config_dict["engine"]["system_role"] = ""
    config = VoicebotConfig.model_validate(valid_config_dict)
    validator = ConfigValidator()
    errors = validator.validate(config)
    assert any("system_role" in e.field for e in errors)


def test_empty_voice_id_error(valid_config_dict):
    valid_config_dict["engine"]["voice_id"] = ""
    config = VoicebotConfig.model_validate(valid_config_dict)
    validator = ConfigValidator()
    errors = validator.validate(config)
    assert any("voice_id" in e.field for e in errors)


def test_voice_speed_out_of_range_error(valid_config):
    valid_config.engine.voice_speed = 3.0
    validator = ConfigValidator()
    errors = validator.validate(valid_config)
    assert any("voice_speed" in e.field for e in errors)


def test_invalid_working_hours_format_error(valid_config_dict):
    valid_config_dict["availability"]["working_hours_start"] = "25:00"
    config = VoicebotConfig.model_validate(valid_config_dict)
    validator = ConfigValidator()
    errors = validator.validate(config)
    assert any("working_hours" in e.field or "availability" in e.field for e in errors)


def test_start_time_after_end_time_error(valid_config_dict):
    valid_config_dict["availability"]["working_hours_start"] = "18:00"
    valid_config_dict["availability"]["working_hours_end"] = "09:00"
    config = VoicebotConfig.model_validate(valid_config_dict)
    validator = ConfigValidator()
    errors = validator.validate(config)
    assert any("availability" in e.field for e in errors)


def test_invalid_timezone_error(valid_config_dict):
    valid_config_dict["availability"]["timezone"] = "Invalid/Timezone"
    config = VoicebotConfig.model_validate(valid_config_dict)
    validator = ConfigValidator()
    errors = validator.validate(config)
    assert any("timezone" in e.field for e in errors)


def test_max_call_duration_zero_error(valid_config_dict):
    valid_config_dict["escalation"]["max_call_duration"] = 0
    config = VoicebotConfig.model_validate(valid_config_dict)
    validator = ConfigValidator()
    errors = validator.validate(config)
    assert any("max_call_duration" in e.field for e in errors)


def test_unknown_llm_provider_id_error(valid_config_dict):
    valid_config_dict["engine"]["llm_provider_id"] = "unknown"
    config = VoicebotConfig.model_validate(valid_config_dict)
    validator = ConfigValidator()
    errors = validator.validate(config)
    assert any("llm_provider_id" in e.field for e in errors)
