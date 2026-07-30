"""Anthropic Messages API LLM provider — lazy-imports the anthropic SDK.

Migrated from the legacy voice engines anthropic_adapter.py, fixing two legacy
gaps: (a) tool calling is now supported — OpenAI-style function tools are
converted to Anthropic ``input_schema`` tools and response ``tool_use`` blocks
are mapped into LLMResult.tool_calls exactly like openai_llm.py (arguments as
a JSON string); (b) the ``system`` prompt is always passed through via the
``system=`` parameter, never dropped. Adds a token stream() implementation via
``client.messages.stream``.
"""

import json
from collections.abc import AsyncIterator

from shared.config import get_settings
from shared.providers.base import LLMProvider, LLMResult, ProviderConfig, ProviderError

_DEFAULT_MODEL = "claude-opus-4-8"

# Model families that reject sampling parameters (temperature/top_p/top_k);
# for these the API default is used and the temperature argument is dropped.
_NO_SAMPLING_PREFIXES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-5",
)


def _merge_to_alternating(messages: list[dict]) -> list[dict]:
    """Coerce OpenAI-style history into strictly alternating user/assistant."""
    result: list[dict] = []
    for message in messages:
        role = (message.get("role") or "user").lower()
        if role == "system":
            continue  # system prompts travel via the system= parameter
        if role not in ("user", "assistant"):
            role = "user"
        content = message.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        content = str(content).strip()
        if not content:
            continue
        if result and result[-1]["role"] == role:
            result[-1]["content"] = f"{result[-1]['content']}\n{content}"
        else:
            result.append({"role": role, "content": content})
    if not result or result[0]["role"] != "user":
        result.insert(0, {"role": "user", "content": "(no user message)"})
    return result


def _convert_tools(provider: str, tools: list[dict]) -> list[dict]:
    """Convert OpenAI-style function tools to Anthropic tool definitions."""
    converted: list[dict] = []
    for tool in tools:
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            function = tool["function"]
            converted.append({
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters")
                or {"type": "object", "properties": {}},
            })
        elif "name" in tool and "input_schema" in tool:
            converted.append(tool)  # already Anthropic-shaped
        else:
            raise ProviderError(provider, "invalid_input", "Unsupported tool definition shape")
    return converted


class AnthropicLLM(LLMProvider):
    name = "anthropic-llm"

    def __init__(self, config: ProviderConfig) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise ProviderError(
                self.name, "invalid_input",
                "anthropic SDK is not installed; run `pip install anthropic` "
                "to use the anthropic LLM provider",
            ) from exc
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.llm_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._client = AsyncAnthropic(api_key=key, timeout=config.timeout_seconds)
        self._model = config.model or _DEFAULT_MODEL

    def _request_kwargs(
        self,
        messages: list[dict],
        system: str | None,
        tools: list[dict] | None,
        temperature: float,
        max_tokens: int,
    ) -> dict:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": _merge_to_alternating(messages),
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = _convert_tools(self.name, tools)
        if not self._model.startswith(_NO_SAMPLING_PREFIXES):
            kwargs["temperature"] = temperature
        return kwargs

    async def generate(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> LLMResult:
        kwargs = self._request_kwargs(messages, system, tools, temperature, max_tokens)
        try:
            response = await self._client.messages.create(**kwargs)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK error types are lazy-loaded
            raise _categorize(self.name, exc) from exc

        text = ""
        tool_calls: list[dict] = []
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text += block.text or ""
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": json.dumps(block.input or {}),
                })
        usage = getattr(response, "usage", None)
        return LLMResult(
            text=text,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            tool_calls=tool_calls,
            finish_reason=getattr(response, "stop_reason", None),
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
        # Streaming feeds the realtime voice path — text deltas only, no tools
        # (matching the openai_llm.py streaming behavior).
        kwargs = self._request_kwargs(messages, system, None, temperature, max_tokens)
        self.last_stream_usage = None
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for delta in stream.text_stream:
                    if delta:
                        yield delta
                try:
                    final = await stream.get_final_message()
                    usage = getattr(final, "usage", None)
                    if usage is not None:
                        from shared.providers.base import LLMStreamUsage

                        self.last_stream_usage = LLMStreamUsage(
                            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                            cached_tokens=int(
                                getattr(usage, "cache_read_input_tokens", 0) or 0
                            ),
                        )
                except Exception:  # noqa: BLE001 — usage capture must not break the call
                    pass
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK error types are lazy-loaded
            raise _categorize(self.name, exc) from exc


def _categorize(provider: str, exc: Exception) -> ProviderError:
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return ProviderError(provider, "auth", str(exc)[:200])
    if status == 429:
        return ProviderError(provider, "rate_limit", str(exc)[:200])
    text = str(exc)
    lowered = text.lower()
    if "401" in text or "403" in text or "authentication" in lowered or "unauthorized" in lowered:
        return ProviderError(provider, "auth", text[:200])
    if "429" in text or "rate limit" in lowered or "rate_limit" in lowered:
        return ProviderError(provider, "rate_limit", text[:200])
    if "timeout" in lowered or "timed out" in lowered:
        return ProviderError(provider, "timeout", text[:200])
    return ProviderError(provider, "upstream", text[:200])
