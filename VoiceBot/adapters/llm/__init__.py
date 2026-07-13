# Lazy-load adapters that require optional packages (anthropic, google)
# so that loading openai_adapter does not require them.
from adapters.llm.openai_adapter import OpenAILLMAdapter

__all__ = ["OpenAILLMAdapter", "AnthropicLLMAdapter", "GoogleLLMAdapter"]


def __getattr__(name: str):
    if name == "AnthropicLLMAdapter":
        from adapters.llm.anthropic_adapter import AnthropicLLMAdapter
        return AnthropicLLMAdapter
    if name == "GoogleLLMAdapter":
        from adapters.llm.google_adapter import GoogleLLMAdapter
        return GoogleLLMAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
