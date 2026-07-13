"""Tests for LLM adapters: success, timeout, rate limit, auth failure, message format."""

import sys
from pathlib import Path

# #endregion

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.base import AdapterException, LLMResponse
from adapters.factory import ModelFactory
from adapters.llm.openai_adapter import OpenAILLMAdapter
from adapters.llm.anthropic_adapter import AnthropicLLMAdapter
from adapters.llm.google_adapter import GoogleLLMAdapter


@pytest.fixture
def openai_mock_response():
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = "Hello back"
    r.usage = MagicMock()
    r.usage.completion_tokens = 5
    r.usage.prompt_tokens = 10
    return r


class TestOpenAILLMAdapter:
    @pytest.mark.asyncio
    async def test_generate_success_returns_llm_response(
        self, sample_messages, sample_system_prompt, openai_mock_response
    ):
        with patch("adapters.llm.openai_adapter.Settings") as settings_mock:
            settings_mock.return_value.openai_api_key = "sk-test"
            settings_mock.return_value.llm_max_response_latency = 3.0
            with patch("adapters.llm.openai_adapter.AsyncOpenAI") as client_cls:
                client = AsyncMock()
                client.chat.completions.create = AsyncMock(
                    return_value=openai_mock_response
                )
                client_cls.return_value = client
                llm = OpenAILLMAdapter(model_id="gpt-4o")
                response = await llm.generate(
                    messages=sample_messages,
                    system_prompt=sample_system_prompt,
                )
        assert isinstance(response, LLMResponse)
        assert response.text == "Hello back"
        assert response.input_tokens >= 0
        assert response.output_tokens == 5
        assert response.latency_ms >= 0
        assert response.model_used == "gpt-4o"

    @pytest.mark.asyncio
    async def test_generate_openai_receives_system_and_messages(
        self, sample_messages, sample_system_prompt, openai_mock_response
    ):
        with patch("adapters.llm.openai_adapter.Settings") as settings_mock:
            settings_mock.return_value.openai_api_key = "sk-test"
            settings_mock.return_value.llm_max_response_latency = 3.0
            with patch("adapters.llm.openai_adapter.AsyncOpenAI") as client_cls:
                client = AsyncMock()
                client.chat.completions.create = AsyncMock(
                    return_value=openai_mock_response
                )
                client_cls.return_value = client
                llm = OpenAILLMAdapter(model_id="gpt-4o")
                await llm.generate(
                    messages=sample_messages,
                    system_prompt=sample_system_prompt,
                )
                call_kw = client.chat.completions.create.call_args
                messages = call_kw.kwargs.get("messages") or call_kw.args[1]
                assert messages[0]["role"] == "system"
                assert messages[0]["content"] == sample_system_prompt
                assert any(m.get("role") == "user" for m in messages)

    @pytest.mark.asyncio
    async def test_generate_timeout_raises_adapter_exception(
        self, sample_messages, sample_system_prompt
    ):
        import asyncio
        async def slow_call(*args, **kwargs):
            await asyncio.sleep(10)
        with patch("adapters.llm.openai_adapter.Settings") as settings_mock:
            settings_mock.return_value.openai_api_key = "sk-test"
            settings_mock.return_value.llm_max_response_latency = 0.01
            with patch("adapters.llm.openai_adapter.AsyncOpenAI") as client_cls:
                client = AsyncMock()
                client.chat.completions.create = slow_call
                client_cls.return_value = client
                llm = OpenAILLMAdapter(model_id="gpt-4o")
                with pytest.raises(AdapterException, match="timed out"):
                    await llm.generate(
                        messages=sample_messages,
                        system_prompt=sample_system_prompt,
                    )

    @pytest.mark.asyncio
    async def test_generate_auth_failure_raises_adapter_exception(
        self, sample_messages, sample_system_prompt
    ):
        with patch("adapters.llm.openai_adapter.Settings") as settings_mock:
            settings_mock.return_value.openai_api_key = "sk-test"
            settings_mock.return_value.llm_max_response_latency = 3.0
            with patch("adapters.llm.openai_adapter.AsyncOpenAI") as client_cls:
                client = AsyncMock()
                client.chat.completions.create = AsyncMock(
                    side_effect=Exception("401 Invalid API key")
                )
                client_cls.return_value = client
                llm = OpenAILLMAdapter(model_id="gpt-4o")
                with pytest.raises(AdapterException, match="authentication|Auth|401"):
                    await llm.generate(
                        messages=sample_messages,
                        system_prompt=sample_system_prompt,
                    )


@pytest.fixture
def anthropic_mock_response():
    r = MagicMock()
    r.content = [MagicMock(type="text", text="Hi!")]
    r.usage = MagicMock()
    r.usage.input_tokens = 10
    r.usage.output_tokens = 5
    return r


class TestAnthropicLLMAdapter:
    @pytest.mark.asyncio
    async def test_generate_success_returns_llm_response(
        self, sample_messages, sample_system_prompt, anthropic_mock_response
    ):
        with patch("adapters.llm.anthropic_adapter.Settings") as settings_mock:
            settings_mock.return_value.anthropic_api_key = "sk-ant-test"
            settings_mock.return_value.llm_max_response_latency = 3.0
            with patch("adapters.llm.anthropic_adapter.AsyncAnthropic") as client_cls:
                client = AsyncMock()
                client.messages.create = AsyncMock(
                    return_value=anthropic_mock_response
                )
                client_cls.return_value = client
                llm = AnthropicLLMAdapter(model_id="claude-3-5-sonnet-20241022")
                response = await llm.generate(
                    messages=sample_messages,
                    system_prompt=sample_system_prompt,
                )
        assert isinstance(response, LLMResponse)
        assert "Hi!" in response.text
        assert response.input_tokens == 10
        assert response.output_tokens == 5
        assert response.model_used == "claude-3-5-sonnet-20241022"

    @pytest.mark.asyncio
    async def test_generate_anthropic_receives_system_and_alternating_messages(
        self, sample_messages, sample_system_prompt, anthropic_mock_response
    ):
        with patch("adapters.llm.anthropic_adapter.Settings") as settings_mock:
            settings_mock.return_value.anthropic_api_key = "sk-ant-test"
            settings_mock.return_value.llm_max_response_latency = 3.0
            with patch("adapters.llm.anthropic_adapter.AsyncAnthropic") as client_cls:
                client = AsyncMock()
                client.messages.create = AsyncMock(
                    return_value=anthropic_mock_response
                )
                client_cls.return_value = client
                llm = AnthropicLLMAdapter(model_id="claude-3-5-sonnet-20241022")
                await llm.generate(
                    messages=sample_messages,
                    system_prompt=sample_system_prompt,
                )
                call_kw = client.messages.create.call_args
                assert call_kw.kwargs.get("system") == sample_system_prompt
                messages = call_kw.kwargs.get("messages", [])
                roles = [m["role"] for m in messages]
                # Should alternate user/assistant (no system in messages)
                assert all(r in ("user", "assistant") for r in roles)


@pytest.fixture
def google_mock_response():
    r = MagicMock()
    r.candidates = [MagicMock()]
    r.candidates[0].content.parts = [MagicMock(text="Gemini says hi")]
    r.usage_metadata = MagicMock()
    r.usage_metadata.prompt_token_count = 10
    r.usage_metadata.candidates_token_count = 5
    return r


class TestGoogleLLMAdapter:
    @pytest.mark.asyncio
    async def test_generate_success_returns_llm_response(
        self, sample_messages, sample_system_prompt, google_mock_response
    ):
        with patch("adapters.llm.google_adapter.Settings") as settings_mock:
            settings_mock.return_value.google_api_key = "test-key"
            settings_mock.return_value.llm_max_response_latency = 3.0

            with patch("adapters.llm.google_adapter.genai") as genai_mock:
                client_mock = MagicMock()

                # Adapter awaits this — must be AsyncMock
                client_mock.aio.models.generate_content = AsyncMock(
                    return_value=google_mock_response
                )

                genai_mock.Client.return_value = client_mock

                llm = GoogleLLMAdapter(model_id="gemini-1.5-flash")

                response = await llm.generate(
                    messages=sample_messages,
                    system_prompt=sample_system_prompt,
                )

        assert isinstance(response, LLMResponse)
        assert "Gemini" in response.text
        assert response.input_tokens == 10
        assert response.output_tokens == 5
        assert response.model_used == "gemini-1.5-flash"

    @pytest.mark.asyncio
    async def test_generate_google_receives_contents_structure(
        self, sample_messages, sample_system_prompt, google_mock_response
    ):
        with patch("adapters.llm.google_adapter.Settings") as settings_mock:
            settings_mock.return_value.google_api_key = "test-key"
            settings_mock.return_value.llm_max_response_latency = 3.0

            with patch("adapters.llm.google_adapter.genai") as genai_mock:
                client_mock = MagicMock()

                client_mock.aio.models.generate_content = AsyncMock(
                    return_value=google_mock_response
                )

                genai_mock.Client.return_value = client_mock

                llm = GoogleLLMAdapter(model_id="gemini-1.5-flash")

                await llm.generate(
                    messages=sample_messages,
                    system_prompt=sample_system_prompt,
                )

                # Ensure correct API call happened
                client_mock.aio.models.generate_content.assert_called_once()

                call_kw = client_mock.aio.models.generate_content.call_args

                assert call_kw.kwargs["model"] == "gemini-1.5-flash"
                assert "contents" in call_kw.kwargs
                assert "config" in call_kw.kwargs

                # Validate system prompt passed via config
                config = call_kw.kwargs["config"]
                assert config.system_instruction == sample_system_prompt