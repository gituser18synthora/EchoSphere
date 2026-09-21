"""Model-specific OpenAI Chat Completions request parameters."""

from shared.providers.llm.openai_llm import _chat_generation_params


async def test_json_output_is_opt_in_and_does_not_affect_normal_generation():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from shared.providers.llm.openai_llm import OpenAILLM

    provider = object.__new__(OpenAILLM)
    provider._model = "gpt-4o-mini"
    create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{}', tool_calls=[]), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
    ))
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    await provider.generate([{"role": "user", "content": "JSON please"}],
                            response_format={"type": "json_object"})
    assert create.call_args.kwargs["response_format"] == {"type": "json_object"}
    await provider.generate([{"role": "user", "content": "Hello"}])
    assert "response_format" not in create.call_args.kwargs


def test_legacy_chat_model_keeps_sampling_controls():
    assert _chat_generation_params(
        "gpt-4o-mini", temperature=0.3, max_tokens=256
    ) == {
        "temperature": 0.3,
        "max_tokens": 256,
    }


def test_gpt5_mini_uses_supported_low_latency_controls():
    params = _chat_generation_params(
        "gpt-5-mini", temperature=0.3, max_tokens=256
    )
    assert params == {
        "max_completion_tokens": 256,
        "reasoning_effort": "minimal",
    }
    assert "max_tokens" not in params
    assert "temperature" not in params


def test_later_gpt5_family_avoids_release_specific_effort_guess():
    assert _chat_generation_params(
        "gpt-5.6-terra", temperature=0.3, max_tokens=256
    ) == {"max_completion_tokens": 256}
