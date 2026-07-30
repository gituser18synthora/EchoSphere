"""OpenAI chat LLM provider — streaming + tool calls.

Migrated from the legacy voice engines openai_adapter.py (the only legacy adapter
with working tool support), reshaped to the new typed interface.
"""

from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from shared.config import get_settings
from shared.providers.base import (
    LLMProvider,
    LLMResult,
    LLMStreamUsage,
    ProviderConfig,
    ProviderError,
)


class OpenAILLM(LLMProvider):
    name = "openai-llm"

    def __init__(self, config: ProviderConfig) -> None:
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.llm_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._client = AsyncOpenAI(api_key=key, timeout=config.timeout_seconds)
        self._model = config.model or settings.llm_model or "gpt-4o-mini"

    def _build_messages(self, messages: list[dict], system: str | None) -> list[dict]:
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        out.extend(messages)
        return out

    async def generate(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> LLMResult:
        kwargs: dict = {}
        if tools:
            kwargs["tools"] = tools
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=self._build_messages(messages, system),
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(self.name, "upstream", str(exc)[:200]) from exc
        choice = response.choices[0]
        tool_calls = [
            {
                "id": call.id,
                "name": call.function.name,
                "arguments": call.function.arguments,
            }
            for call in (choice.message.tool_calls or [])
        ]
        usage = response.usage
        return LLMResult(
            text=choice.message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
        )

    async def stream(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> AsyncIterator[str]:
        self.last_stream_usage = None
        try:
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=self._build_messages(messages, system),
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                # Final chunk carries provider-reported token usage — the
                # billing source of truth for the streaming voice path.
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    prompt_details = getattr(usage, "prompt_tokens_details", None)
                    completion_details = getattr(usage, "completion_tokens_details", None)
                    self.last_stream_usage = LLMStreamUsage(
                        input_tokens=int(usage.prompt_tokens or 0),
                        output_tokens=int(usage.completion_tokens or 0),
                        cached_tokens=int(getattr(prompt_details, "cached_tokens", 0) or 0),
                        reasoning_tokens=int(
                            getattr(completion_details, "reasoning_tokens", 0) or 0
                        ),
                    )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(self.name, "upstream", str(exc)[:200]) from exc
