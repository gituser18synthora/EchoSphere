"""Tests for VoiceBotOrchestrator: initialize, handle_utterance, pipeline stops, end_call."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.base import AdapterException, LLMResponse, STTResponse
from orchestrator.call_state import CallState
from orchestrator.exceptions import OrchestratorNotInitializedError
from orchestrator.orchestrator import VoiceBotOrchestrator


def _make_mock_stt(text="hello", confidence=0.9, detected_language="en"):
    mock = MagicMock()
    mock.transcribe = AsyncMock(
        return_value=STTResponse(
            text=text,
            detected_language=detected_language,
            confidence=confidence,
            is_final=True,
        )
    )
    return mock


def _make_mock_tts():
    mock = MagicMock()
    async def stream(*args, **kwargs):
        yield b"\x00\x00" * 400  # 100ms at 8kHz
    mock.synthesize_stream = stream
    return mock


def _make_mock_llm(text="Hi there!"):
    mock = MagicMock()
    mock.generate = AsyncMock(
        return_value=LLMResponse(
            text=text,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100.0,
            model_used="gpt-4o",
        )
    )
    return mock


def _fake_build_llm_messages(call_state, current_text, knowledge_content):
    if knowledge_content:
        cur = f"Context: {knowledge_content}\n\nUser: {current_text}"
    else:
        cur = current_text
    return [
        {"role": "system", "content": call_state.system_prompt or ""},
        {"role": "user", "content": cur},
    ]


async def _fake_async_build_llm_messages(
    call_state, current_text, knowledge_content, **_kwargs
):
    return _fake_build_llm_messages(
        call_state, current_text, knowledge_content
    )


async def _fake_get_full_transcript(call_state):
    return call_state.transcript_as_dialogue()


@pytest.fixture
def mock_adapters():
    """Patch ModelFactory + ContextManager (no real Redis/Mongo)."""
    stt = _make_mock_stt()
    tts = _make_mock_tts()
    llm = _make_mock_llm()
    cm = MagicMock()
    cm.on_call_start = AsyncMock(return_value=None)
    cm.sync_session = AsyncMock()
    cm.persist_turn = AsyncMock()
    cm.on_call_end = AsyncMock()
    cm.get_full_transcript = AsyncMock(side_effect=_fake_get_full_transcript)
    cm.build_llm_messages = AsyncMock(
        side_effect=_fake_async_build_llm_messages
    )
    with patch("orchestrator.orchestrator.ModelFactory") as mf, patch(
        "orchestrator.orchestrator.ContextManager", return_value=cm
    ):
        mf.create_stt.return_value = stt
        mf.create_tts.return_value = tts
        mf.create_llm.return_value = llm
        yield {"stt": stt, "tts": tts, "llm": llm, "context_manager": cm}


@pytest.mark.asyncio
async def test_initialize_creates_call_state_with_correct_fields(
    valid_config, mock_adapters
):
    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    assert orch.call_state is not None
    assert orch.call_state.call_id == "c1"
    assert orch.call_state.caller_phone == "+911234567890"
    assert orch.call_state.voicebot_id == valid_config.voicebot_id
    assert orch.call_state.tenant_id == valid_config.tenant_id


@pytest.mark.asyncio
async def test_initialize_returns_bytes_greeting_audio(
    valid_config, mock_adapters
):
    orch = VoiceBotOrchestrator(valid_config)
    audio = await orch.initialize(call_id="c1", caller_phone="+911234567890")
    assert isinstance(audio, bytes)
    assert len(audio) > 0


@pytest.mark.asyncio
async def test_initialize_assembles_non_empty_system_prompt(
    valid_config, mock_adapters
):
    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    assert orch.call_state.system_prompt
    assert valid_config.engine.system_role in orch.call_state.system_prompt


@pytest.mark.asyncio
async def test_initialize_handles_null_caller_name_in_graph(
    valid_config, mock_adapters
):
    mock_adapters["context_manager"].on_call_start = AsyncMock(
        return_value={
            "caller_name": None,
            "caller_email": None,
            "nodes": [],
            "edges": [],
        }
    )
    orch = VoiceBotOrchestrator(valid_config)
    audio = await orch.initialize(call_id="c1", caller_phone="+911234567890")
    assert isinstance(audio, bytes)
    assert len(audio) > 0


@pytest.mark.asyncio
async def test_handle_utterance_raises_before_initialize(
    valid_config, mock_adapters
):
    orch = VoiceBotOrchestrator(valid_config)
    with pytest.raises(OrchestratorNotInitializedError):
        await orch.handle_utterance(b"\x00\x00" * 800)


@pytest.mark.asyncio
async def test_handle_utterance_returns_bytes_on_valid_audio(
    valid_config, mock_adapters
):
    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    mock_adapters["llm"].generate.return_value = LLMResponse(
        text="I can help with that.",
        input_tokens=20,
        output_tokens=6,
        latency_ms=150.0,
        model_used="gpt-4o",
    )
    audio = await orch.handle_utterance(b"\x00\x00" * 800)
    assert isinstance(audio, bytes)
    assert len(audio) > 0


@pytest.mark.asyncio
async def test_pipeline_stops_at_step2_when_stt_returns_empty(
    valid_config, mock_adapters
):
    mock_adapters["stt"].transcribe.return_value = STTResponse(
        text="",
        detected_language="en",
        confidence=0.5,
        is_final=True,
    )
    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    audio = await orch.handle_utterance(b"\x00\x00" * 800)
    assert isinstance(audio, bytes)
    mock_adapters["llm"].generate.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_handles_low_confidence_stt(
    valid_config, mock_adapters
):
    """
    Low STT confidence (<0.4) returns None text; pipeline speaks a repeat
    prompt without calling the main LLM. Intent classifier (also LLM) is
    called because classify() is invoked after transcription returns None
    → actually the pipeline returns early at the 'if not text' branch, so
    no LLM call at all.
    """
    mock_adapters["stt"].transcribe.return_value = STTResponse(
        text="mumble",
        detected_language="en",
        confidence=0.3,
        is_final=True,
    )
    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    # reset call count from initialize's intent classify etc.
    mock_adapters["llm"].generate.reset_mock()
    audio = await orch.handle_utterance(b"\x00\x00" * 800)
    assert isinstance(audio, bytes)
    # Pipeline stops at STT (low confidence → None text); no LLM call
    mock_adapters["llm"].generate.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_stops_at_step3_when_escalation_keyword(
    valid_config, mock_adapters
):
    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    mock_adapters["stt"].transcribe.return_value = STTResponse(
        text="I want to speak to a human agent",
        detected_language="en",
        confidence=0.95,
        is_final=True,
    )
    audio = await orch.handle_utterance(b"\x00\x00" * 800)
    assert orch.call_state.escalation_triggered
    assert orch.call_state.escalation_reason == "transfer_requested"


@pytest.mark.asyncio
async def test_pipeline_stops_at_step3_when_max_duration_exceeded(
    valid_config, mock_adapters
):
    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    orch.call_state.call_start_time = datetime.utcnow() - timedelta(
        minutes=15
    )
    valid_config.escalation.max_call_duration = 10
    mock_adapters["stt"].transcribe.return_value = STTResponse(
        text="hello",
        detected_language="en",
        confidence=0.9,
        is_final=True,
    )
    await orch.handle_utterance(b"\x00\x00" * 800)
    assert orch.call_state.escalation_triggered
    assert orch.call_state.escalation_reason == "max_duration"


@pytest.mark.asyncio
async def test_fallback_llm_used_when_primary_raises_adapter_exception(
    valid_config, mock_adapters
):
    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    # First call = intent classification (must succeed); second = step 9 generate (raise)
    intent_resp = MagicMock(
        text='{"intent": "general_query", "confidence": 0.9, "sentiment": "neutral"}'
    )
    mock_adapters["llm"].generate.side_effect = [
        intent_resp,
        AdapterException("timeout"),
    ]
    fallback_llm = _make_mock_llm("Fallback response here.")
    with patch.object(orch, "_get_fallback_llm", return_value=fallback_llm):
        audio = await orch.handle_utterance(b"\x00\x00" * 800)
    assert isinstance(audio, bytes)
    fallback_llm.generate.assert_called_once()


@pytest.mark.asyncio
async def test_end_call_returns_extraction_dict_on_normal_end(
    valid_config, mock_adapters
):
    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    orch.call_state.add_turn("user", "I need help", intent="general_query", confidence=0.8)
    orch.call_state.add_turn("assistant", "Sure.")
    orch.call_state.turn_count = 1
    mock_adapters["llm"].generate.return_value = LLMResponse(
        text='{"caller_name": null, "nodes": [], "edges": [], "summary": "Test."}',
        input_tokens=50,
        output_tokens=20,
        latency_ms=100.0,
        model_used="gpt-4o",
    )
    extraction = await orch.end_call(reason="normal")
    assert extraction is not None
    assert "summary" in extraction
    assert "nodes" in extraction
    assert "edges" in extraction
    mock_adapters["context_manager"].on_call_end.assert_awaited()


@pytest.mark.asyncio
async def test_end_call_returns_none_when_privacy_flag_true(
    valid_config, mock_adapters
):
    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    orch.call_state.privacy_deletion_requested = True
    orch.call_state.add_turn("user", "delete my data", intent="privacy_request", confidence=0.9)
    orch.call_state.add_turn("assistant", "Noted.")
    extraction = await orch.end_call(reason="normal")
    assert extraction is None
    mock_adapters["context_manager"].on_call_end.assert_awaited_once()


@pytest.mark.asyncio
async def test_end_call_handles_extraction_parse_failure_gracefully(
    valid_config, mock_adapters
):
    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    orch.call_state.add_turn("user", "hi", intent="greeting", confidence=0.9)
    orch.call_state.add_turn("assistant", "Hello!")
    orch.call_state.turn_count = 1
    mock_adapters["llm"].generate.return_value = LLMResponse(
        text="not valid json at all {{{",
        input_tokens=50,
        output_tokens=20,
        latency_ms=100.0,
        model_used="gpt-4o",
    )
    extraction = await orch.end_call(reason="normal")
    assert extraction is not None
    assert extraction.get("summary") == "Extraction failed"


@pytest.mark.asyncio
async def test_running_summary_generated_at_turn_5(
    valid_config, mock_adapters
):
    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    # Per utterance: 1 intent classify + 1 generate response; after 5th turn, 1 summary
    responses = []
    for i in range(5):
        responses.append(
            MagicMock(
                text='{"intent": "general_query", "confidence": 0.9, "sentiment": "neutral"}'
            )
        )
        responses.append(
            LLMResponse(
                text=f"Response {i}",
                input_tokens=10,
                output_tokens=5,
                latency_ms=50.0,
                model_used="gpt-4o",
            )
        )
    responses.append(
        LLMResponse(
            text="Summary of the conversation so far.",
            input_tokens=20,
            output_tokens=10,
            latency_ms=50.0,
            model_used="gpt-4o",
        )
    )
    mock_adapters["llm"].generate.side_effect = responses
    for i in range(5):
        mock_adapters["stt"].transcribe.return_value = STTResponse(
            text=f"utterance {i}",
            detected_language="en",
            confidence=0.9,
            is_final=True,
        )
        await orch.handle_utterance(b"\x00\x00" * 800)
    assert orch.call_state.running_summary is not None


@pytest.mark.asyncio
async def test_sentiment_augments_system_prompt_at_turn_3(
    valid_config, mock_adapters
):
    from orchestrator.system_prompt import SENTIMENT_MARKER

    orch = VoiceBotOrchestrator(valid_config)
    await orch.initialize(call_id="c1", caller_phone="+911234567890")
    mock_adapters["llm"].generate.side_effect = [
        LLMResponse(
            text='{"intent": "general_query", "confidence": 0.9, "sentiment": "neutral"}',
            input_tokens=10,
            output_tokens=15,
            latency_ms=50.0,
            model_used="gpt-4o",
        ),
        LLMResponse(
            text="Reply one",
            input_tokens=10,
            output_tokens=5,
            latency_ms=50.0,
            model_used="gpt-4o",
        ),
        LLMResponse(
            text='{"intent": "general_query", "confidence": 0.9, "sentiment": "frustrated"}',
            input_tokens=10,
            output_tokens=15,
            latency_ms=50.0,
            model_used="gpt-4o",
        ),
        LLMResponse(
            text="Reply two",
            input_tokens=10,
            output_tokens=5,
            latency_ms=50.0,
            model_used="gpt-4o",
        ),
        LLMResponse(
            text='{"intent": "general_query", "confidence": 0.9, "sentiment": "negative"}',
            input_tokens=10,
            output_tokens=15,
            latency_ms=50.0,
            model_used="gpt-4o",
        ),
        LLMResponse(
            text="Reply three",
            input_tokens=10,
            output_tokens=5,
            latency_ms=50.0,
            model_used="gpt-4o",
        ),
    ]
    for _ in range(3):
        mock_adapters["stt"].transcribe.return_value = STTResponse(
            text="something",
            detected_language="en",
            confidence=0.9,
            is_final=True,
        )
        await orch.handle_utterance(b"\x00\x00" * 800)
    assert SENTIMENT_MARKER in orch.call_state.system_prompt
