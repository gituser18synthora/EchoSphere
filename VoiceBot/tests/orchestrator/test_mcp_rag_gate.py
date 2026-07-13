"""Tests for per-voicebot enable_rag gating of search_knowledge_base."""

from unittest.mock import MagicMock, patch

import pytest

from voicebot.config_layer.models import EngineConfig, VoicebotConfig
from voicebot.orchestrator.orchestrator import VoiceBotOrchestrator, _RAG_TOOL_NAME


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}


def _minimal_config(*, enable_rag: bool) -> VoicebotConfig:
    return VoicebotConfig(
        voicebot_id="vb-1",
        tenant_id="tenant-1",
        name="Test Bot",
        business_name="Test Co",
        engine=EngineConfig(enable_rag=enable_rag),
    )


@pytest.fixture
def orchestrator_factory():
    def _make(enable_rag: bool) -> VoiceBotOrchestrator:
        with patch("voicebot.orchestrator.orchestrator.ModelFactory") as mf:
            mf.create_stt.return_value = MagicMock()
            mf.create_tts.return_value = MagicMock()
            mf.create_llm.return_value = MagicMock()
            with patch("voicebot.orchestrator.orchestrator.Settings") as settings_cls:
                settings_cls.return_value = MagicMock(
                    mcp_server_url="",
                    mcp_api_key="",
                )
                return VoiceBotOrchestrator(_minimal_config(enable_rag=enable_rag))

    return _make


def test_rag_tool_excluded_when_enable_rag_false(orchestrator_factory):
    orch = orchestrator_factory(enable_rag=False)
    tools = [_tool(_RAG_TOOL_NAME), _tool("send_email")]
    filtered = orch._filter_mcp_tools(tools)
    names = [t["function"]["name"] for t in filtered]
    assert _RAG_TOOL_NAME not in names
    assert "send_email" in names


def test_rag_tool_included_when_enable_rag_true(orchestrator_factory):
    orch = orchestrator_factory(enable_rag=True)
    tools = [_tool(_RAG_TOOL_NAME), _tool("send_email")]
    filtered = orch._filter_mcp_tools(tools)
    names = [t["function"]["name"] for t in filtered]
    assert _RAG_TOOL_NAME in names
    assert "send_email" in names
