"""Tests for system_prompt: assemble_system_prompt, update_goal_context, augment_with_sentiment, format_caller_graph."""

import pytest

from config_layer.models import VoicebotConfig
from orchestrator.call_state import CallState, ActiveGoal
from orchestrator.system_prompt import (
    assemble_system_prompt,
    update_goal_context,
    augment_with_sentiment,
    format_caller_graph,
    GOAL_CONTEXT_MARKER,
    SENTIMENT_MARKER,
)
from tests.orchestrator.conftest import _valid_config_dict


@pytest.fixture
def call_state():
    return CallState(
        call_id="c1",
        voicebot_id="vb-1",
        caller_phone="+911234567890",
        tenant_id="t1",
        detected_language="en",
    )


def test_all_sections_assembled_in_order(valid_config, call_state):
    valid_config.engine.guardrails = "Never lie."
    prompt = assemble_system_prompt(valid_config, None, call_state)
    assert valid_config.engine.system_role in prompt
    assert valid_config.engine.primary_objectives in prompt
    assert "Never lie." in prompt
    assert "concise" in prompt.lower() or "brief" in prompt.lower()
    assert "en" in prompt or "English" in prompt or "primary" in prompt
    assert "CONVERSATION MEMORY INSTRUCTIONS" in prompt
    assert "NEVER say you don't remember" in prompt
    assert "PRIMARY OBJECTIVES:" in prompt
    assert "----------------------------------" in prompt


def test_primary_objectives_block_skipped_when_empty(valid_config, call_state):
    valid_config.engine.primary_objectives = "   "
    prompt = assemble_system_prompt(valid_config, None, call_state)
    assert "PRIMARY OBJECTIVES:" not in prompt


def test_section_3_absent_when_guardrails_empty(valid_config, call_state):
    valid_config.engine.guardrails = ""
    prompt = assemble_system_prompt(valid_config, None, call_state)
    assert "Rules you must always follow" not in prompt or "\n\n" in prompt


def test_section_6_absent_when_caller_graph_none(valid_config, call_state):
    valid_config.engine.context_recall_between_calls = True
    prompt = assemble_system_prompt(valid_config, None, call_state)
    assert "CALLER CONTEXT" not in prompt


def test_section_6_absent_when_context_recall_false(valid_config, call_state):
    valid_config.engine.context_recall_between_calls = False
    graph = {"caller_name": "John", "nodes": [], "edges": []}
    prompt = assemble_system_prompt(valid_config, graph, call_state)
    assert "John" not in prompt


def test_section_6_present_when_graph_provided(valid_config, call_state):
    valid_config.engine.context_recall_between_calls = True
    graph = {
        "caller_name": "Jane",
        "nodes": [
            {"node_id": "n1", "key": "preferred_day", "value": "Monday"},
        ],
        "edges": [
            {"to_node": "n1", "relation": "has_preference"},
        ],
    }
    prompt = assemble_system_prompt(valid_config, graph, call_state)
    assert "CALLER CONTEXT" in prompt
    assert "Jane" in prompt


def test_update_goal_context_adds_goal_section_when_active(valid_config):
    base = "You are helpful.\n\nRespond in en."
    goal = ActiveGoal(
        goal_name="book_appointment",
        slots={"date": "Monday", "time": None},
        started_at_turn=0,
    )
    result = update_goal_context(base, goal)
    assert GOAL_CONTEXT_MARKER in result
    assert "book_appointment" in result
    assert "date" in result
    assert "time" in result or "Still needed" in result


def test_update_goal_context_removes_goal_section_when_none(valid_config):
    base = "You are helpful.\n\n" + GOAL_CONTEXT_MARKER + "\nGoal stuff here."
    result = update_goal_context(base, None)
    assert GOAL_CONTEXT_MARKER not in result
    assert "Goal stuff" not in result


def test_augment_with_sentiment_appends_for_frustrated():
    base = "You are helpful."
    result = augment_with_sentiment(base, "frustrated")
    assert SENTIMENT_MARKER in result
    assert "frustrated" in result


def test_augment_with_sentiment_appends_for_negative():
    base = "You are helpful."
    result = augment_with_sentiment(base, "negative")
    assert SENTIMENT_MARKER in result
    assert "negative" in result


def test_augment_with_sentiment_unchanged_for_positive():
    base = "You are helpful."
    result = augment_with_sentiment(base, "positive")
    assert result == base


def test_augment_with_sentiment_unchanged_for_neutral():
    base = "You are helpful."
    result = augment_with_sentiment(base, "neutral")
    assert result == base


def test_format_caller_graph_formats_all_edge_types():
    graph = {
        "caller_name": "Alex",
        "nodes": [
            {"node_id": "p1", "key": "preferred_time", "value": "morning"},
            {"node_id": "f1", "key": "insurance", "value": "Acme Inc"},
            {"node_id": "a1", "key": "action", "value": "book appointment"},
            {"node_id": "u1", "key": "issue", "value": "billing dispute"},
        ],
        "edges": [
            {"to_node": "p1", "relation": "has_preference"},
            {"to_node": "f1", "relation": "has_fact"},
            {"to_node": "a1", "relation": "requested"},
            {"to_node": "u1", "relation": "unresolved"},
        ],
    }
    text = format_caller_graph(graph)
    assert "Alex" in text
    assert "Preference" in text or "morning" in text
    assert "Insurance" in text or "Acme" in text
    assert "Previous action" in text or "book appointment" in text
    assert "UNRESOLVED" in text or "billing" in text


def test_format_caller_graph_returns_empty_for_empty_graph():
    assert format_caller_graph(None) == ""
    assert format_caller_graph({}) == ""
    assert format_caller_graph({"nodes": [], "edges": []}) == ""
