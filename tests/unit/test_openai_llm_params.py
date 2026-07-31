"""Model-specific OpenAI Chat Completions request parameters."""

from shared.providers.llm.openai_llm import _chat_generation_params


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
