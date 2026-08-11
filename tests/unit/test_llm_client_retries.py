"""LLM provider clients must disable SDK-internal retries.

Both SDKs default to 2 hidden retries with backoff; stacked under the voice
brain's (and the REST API's) own bounded retries that turns one slow
upstream call into an invisible multi-second tail. Retry policy belongs to
the callers — the clients are constructed with max_retries=0.
"""

import sys
import types

import shared.providers.llm.openai_llm as openai_llm_module
from shared.providers.base import ProviderConfig


def test_openai_client_disables_sdk_retries(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "sk-test-key")
    captured: dict = {}

    def fake_async_openai(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openai_llm_module, "AsyncOpenAI", fake_async_openai)
    openai_llm_module.OpenAILLM(
        ProviderConfig(provider="openai", api_key_reference="env:TEST_LLM_KEY")
    )
    assert captured["max_retries"] == 0
    assert captured["api_key"] == "sk-test-key"


def test_anthropic_client_disables_sdk_retries(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "sk-ant-test-key")
    captured: dict = {}

    def fake_async_anthropic(**kwargs):
        captured.update(kwargs)
        return object()

    # The anthropic SDK is lazy-imported inside __init__ — inject a fake
    # module so the test runs whether or not the SDK is installed.
    monkeypatch.setitem(
        sys.modules, "anthropic",
        types.SimpleNamespace(AsyncAnthropic=fake_async_anthropic),
    )
    from shared.providers.llm.anthropic_llm import AnthropicLLM

    AnthropicLLM(
        ProviderConfig(provider="anthropic", api_key_reference="env:TEST_LLM_KEY")
    )
    assert captured["max_retries"] == 0
    assert captured["api_key"] == "sk-ant-test-key"
