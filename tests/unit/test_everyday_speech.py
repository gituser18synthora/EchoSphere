"""Actual generation prompts use the shared rule with no extra model pass.

Providers and database reads are stubbed; prompt assembly and endpoint logic
are real. These tests do not measure model fluency or remote latency.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.routers import prompts, testing
from shared.bot_config import ResolvedBotConfig
from shared.orchestration.speech_style import (
    EVERYDAY_SPEECH_INSTRUCTION,
    spoken_reply_instruction,
)
from shared.orchestration.voice_identity import VoiceIdentity
from tests.unit.test_brain_language import _RecorderStub
from voice_runtime.brain import ConversationBrain


@pytest.mark.parametrize("tenant", ["tenant-a", "tenant-b"])
@pytest.mark.parametrize("locale", [
    "hi-IN", "ta-IN", "ml-IN", "te-IN", "kn-IN", "mr-IN", "bn-IN",
    "gu-IN", "pa-IN", "or-IN", "ur-IN", "en-IN",
])
def test_policy_applies_across_tenants_and_survives_language_switch(locale, tenant):
    config = ResolvedBotConfig(
        tenant_id=tenant, bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language=locale, languages=[locale, "en-IN"],
        stt={"provider": "sarvam"},
    )
    brain = ConversationBrain(config=config, llm=None, recorder=_RecorderStub())
    original = brain._language_instruction()
    assert original.count(EVERYDAY_SPEECH_INSTRUCTION) == 1
    assert "familiar English loanwords are allowed" in original
    brain._conversation_language = "en-IN"
    assert EVERYDAY_SPEECH_INSTRUCTION in brain._language_instruction()
    brain._conversation_language = locale
    assert brain._language_instruction() == original


@pytest.fixture
def preview_provider(monkeypatch):
    """No credentials, real provider requests, or database connections."""
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(
        text="Sample reply", input_tokens=10, output_tokens=3,
    )))
    settings = SimpleNamespace(
        tts_provider="mock", tts_voice="", llm_provider="mock", llm_model="mock",
        llm_api_key_reference="",
    )
    monkeypatch.setattr("shared.config.get_settings", lambda: settings)
    monkeypatch.setattr("shared.providers.factory.get_llm_provider", lambda config: llm)
    monkeypatch.setattr("shared.bot_config.resolve_voice_identity_for_settings",
                        lambda *a, **kw: VoiceIdentity(name="Ritu", gender="female"))
    return llm


def sample_prompt():
    return SimpleNamespace(
        id="prompt-x", bot_id="bot-x", active_version=1,
        versions=[SimpleNamespace(
            version=1, compiled_prompt="Tenant persona: use formal textbook language.",
        )],
    )


@pytest.mark.parametrize("locale,label", [
    ("hi-IN", "Hindi"), ("ta-IN", "Tamil"), ("ml-IN", "Malayalam"),
    ("te-IN", "Telugu"), ("kn-IN", "Kannada"), ("en-IN", "English"),
])
async def test_chat_uses_real_prompt_assembly_and_current_language(preview_provider, locale, label):
    llm = preview_provider
    # System prompt, optional context schema, optional voice settings.
    db = Mock()
    db.scalar.side_effect = [sample_prompt(), None, None]
    bot = SimpleNamespace(id="bot-x", tenant_id="tenant-a")
    body = testing.ChatTestRequest(message="A caller question", messages=[])
    extra = "\nWorkflow reference: ask for booking ID. Example language: Hindi."
    result = await testing._testing_llm_reply(
        db, bot, body, locale, extra_system=extra,
    )
    llm.generate.assert_awaited_once()
    system = llm.generate.call_args.kwargs["system"]
    assert system.endswith(spoken_reply_instruction(locale))
    assert system.count(EVERYDAY_SPEECH_INSTRUCTION) == 1
    assert f"ENTIRE reply must be in {label}" in system
    if label != "English":
        assert "Reply only in natural Indian English" not in system
        assert "ENTIRE reply must be in English" not in system
    assert system.index(extra) < system.index("# Reply language")
    assert "assistant_voice_gender = female" in system
    assert result == "Sample reply"


@pytest.mark.parametrize("base_prompt", [
    "Persona plus current workflow goals and facts.",
    "Rewrite just this question in the caller's language.",
    "Answer only the question using this knowledge-base passage.",
])
async def test_simulator_trace_matches_sent_prompt_and_uses_one_call(base_prompt):
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(text="சரி.")))
    trace = {"renderedPrompt": "An earlier base prompt", "route": "workflow"}
    result = await testing._simulate_llm_reply(
        llm, base_prompt, [], "Booking ID?", language="ta-IN", trace=trace,
    )
    llm.generate.assert_awaited_once()
    system = llm.generate.call_args.kwargs["system"]
    assert system == base_prompt + spoken_reply_instruction("ta-IN")
    assert trace["renderedPrompt"] == system
    assert trace["route"] == "workflow"
    assert llm.generate.call_args.args[0] == [{"role": "user", "content": "Booking ID?"}]
    assert result == "சரி."


async def test_failed_simulator_call_still_records_attempted_prompt():
    llm = SimpleNamespace(generate=AsyncMock(side_effect=RuntimeError("unavailable")))
    trace = {}
    result = await testing._simulate_llm_reply(
        llm, "Reference facts.", [], "Question", language="ml-IN", trace=trace,
    )
    llm.generate.assert_awaited_once()
    assert trace["renderedPrompt"] == llm.generate.call_args.kwargs["system"]
    assert result == "(LLM unavailable: RuntimeError)"


@pytest.mark.parametrize("locale", ["hi-IN", "ta-IN", "ml-IN", "en-IN"])
async def test_prompt_editor_preview_uses_same_rule(monkeypatch, preview_provider, locale):
    llm = preview_provider
    bot = SimpleNamespace(id="bot-x", tenant_id="tenant-a")
    monkeypatch.setattr(prompts, "_prompt_checked", lambda *a: sample_prompt())
    monkeypatch.setattr(prompts, "_bot_checked", lambda *a: bot)
    db = Mock()
    db.scalars.return_value.all.return_value = []
    db.scalar.return_value = None
    result = await prompts.test_prompt(
        "prompt-x", prompts.PromptTestRequest(
            message="Tell me about my booking", language=locale, use_knowledge=False,
        ), user=None, db=db,
    )
    llm.generate.assert_awaited_once()
    system = llm.generate.call_args.kwargs["system"]
    assert system.endswith(spoken_reply_instruction(locale))
    assert system.count(EVERYDAY_SPEECH_INSTRUCTION) == 1
    assert "Tenant persona" in system
    assert "assistant_voice_gender = female" in system
    assert result["data"]["response"] == "Sample reply"
    db.commit.assert_not_called()
