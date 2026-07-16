"""Deterministic LLM for tests.

Behavior: echoes configured responses (config.extra['responses'] round-robin),
or answers from provided context when the last user message contains a
question that matches supplied knowledge sources (naive keyword answer)."""

import asyncio
import itertools
from collections.abc import AsyncIterator

from backend.providers.base import LLMProvider, LLMResult, ProviderConfig


class MockLLM(LLMProvider):
    name = "mock-llm"

    def __init__(self, config: ProviderConfig) -> None:
        responses = config.extra.get("responses")
        self._cycle = itertools.cycle(responses) if responses else None

    def _answer(self, messages: list[dict], system: str | None) -> str:
        if self._cycle is not None:
            return next(self._cycle)
        # Grounded mode: if the system prompt carries retrieved context, quote it.
        if system and "Context:" in system:
            context = system.split("Context:", 1)[1].strip()
            if context:
                first_line = context.splitlines()[0][:200]
                return f"Based on the documentation: {first_line}"
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        return f"You said: {last_user[:100]}"

    async def generate(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> LLMResult:
        text = self._answer(messages, system)
        return LLMResult(text=text, input_tokens=10, output_tokens=len(text) // 4)

    async def stream(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> AsyncIterator[str]:
        text = self._answer(messages, system)
        for word in text.split(" "):
            await asyncio.sleep(0)  # yield control like a real stream
            yield word + " "
