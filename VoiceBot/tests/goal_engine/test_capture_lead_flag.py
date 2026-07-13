"""Regression: legacy goal_flag_map uses capture_leads while GoalsConfig uses capture_lead."""

from unittest.mock import MagicMock

import pytest

from config_layer.models import VoicebotConfig
from goal_engine.engine import GoalEngine
from tests.orchestrator.conftest import _valid_config_dict


def test_capture_lead_enabled_when_flag_map_uses_capture_leads_plural():
    d = _valid_config_dict()
    d["goals"]["capture_lead"] = True
    d["intent_config"]["goal_flag_map"]["capture_lead"] = "capture_leads"
    cfg = VoicebotConfig.model_validate(d)
    engine = GoalEngine(MagicMock(), cfg)
    assert "capture_lead" in engine._handlers


def test_capture_lead_still_disabled_when_goal_off():
    d = _valid_config_dict()
    d["goals"]["capture_lead"] = False
    d["intent_config"]["goal_flag_map"]["capture_lead"] = "capture_leads"
    cfg = VoicebotConfig.model_validate(d)
    engine = GoalEngine(MagicMock(), cfg)
    assert "capture_lead" not in engine._handlers
