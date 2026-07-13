"""OpenAI LLM adapter using AsyncOpenAI."""

import asyncio
import time
from typing import Any, Optional

import tiktoken
from openai import AsyncOpenAI
from openai import APIError, APITimeoutError, AuthenticationError, RateLimitError

from adapters.base import AdapterException, LLMAdapter, LLMResponse
from config.settings import Settings


def _count_tokens(messages: list[dict], model: str = "gpt-4o") -> int:
    """Count input tokens with tiktoken for budget tracking."""
    try:
        enc = tiktoken.encoding_for_model(model)
    except KeyError:
        enc = tiktoken.get_encoding("cl100k_base")
    total = 0
    for msg in messages:
        content = msg.get("content", "") or ""
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        total += 4 + len(enc.encode(str(content)))
    return total


class OpenAILLMAdapter(LLMAdapter):
    """OpenAI chat completions via AsyncOpenAI. Supports gpt-4o, gpt-4o-mini."""

    def __init__(self, model_id: str = "gpt-4o", **kwargs: Any) -> None:
        settings = Settings()
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model_id = model_id
        self._timeout = getattr(
            settings, "llm_max_response_latency", 3.0
        )

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int = 300,
        temperature: float = 0.7,
        tools: Optional[list[dict]] = None,
    ) -> LLMResponse:
        # Build OpenAI messages: avoid duplicating system when orchestrator
        # already put system as messages[0] in _build_messages().
        openai_messages: list[dict[str, Any]] = []
        has_system = bool(messages) and messages[0].get("role") == "system"
        if system_prompt and not has_system:
            openai_messages.append({"role": "system", "content": system_prompt})

        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")

            if role == "tool":
                # Tool result message — pass through as-is for the tool call loop
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "content": content or "",
                })
            elif role == "assistant" and m.get("tool_calls"):
                # Assistant message that contains tool calls — reconstruct properly
                openai_messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in m["tool_calls"]
                    ],
                })
            elif role in ("system", "user", "assistant"):
                openai_messages.append({"role": role, "content": content})

        input_tokens = _count_tokens(openai_messages, self._model_id)
        start = time.perf_counter()

        async def _call() -> Any:
            kwargs: dict[str, Any] = dict(
                model=self._model_id,
                messages=openai_messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            # Only pass tools to OpenAI if we have them — avoids API errors
            # when tools list is empty or None.
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            return await self._client.chat.completions.create(**kwargs)

        try:
            response = await asyncio.wait_for(_call(), timeout=self._timeout)
        except asyncio.TimeoutError as e:
            raise AdapterException(
                f"OpenAI request timed out after {self._timeout}s"
            ) from e
        except AuthenticationError as e:
            raise AdapterException(f"OpenAI authentication failed: {e}") from e
        except RateLimitError as e:
            retry_after = None
            if hasattr(e, "response") and e.response is not None:
                h = getattr(e.response, "headers", None) or {}
                ra = h.get("retry-after")
                if ra is not None:
                    try:
                        retry_after = float(ra)
                    except (TypeError, ValueError):
                        pass
            raise AdapterException(
                f"OpenAI rate limit exceeded: {e}",
                retry_after=retry_after,
            ) from e
        except APITimeoutError as e:
            raise AdapterException(f"OpenAI request timed out: {e}") from e
        except APIError as e:
            status = getattr(e, "status_code", None)
            if status == 401 or (status and "auth" in str(e).lower()):
                raise AdapterException(f"OpenAI authentication failed: {e}") from e
            if status == 503 or (status and status >= 500):
                raise AdapterException(
                    f"OpenAI service unavailable: {e}"
                ) from e
            raise AdapterException(f"OpenAI API error: {e}") from e
        except Exception as e:
            if "401" in str(e) or "auth" in str(e).lower() or "unauthorized" in str(e).lower():
                raise AdapterException(f"OpenAI authentication failed: {e}") from e
            raise

        latency_ms = (time.perf_counter() - start) * 1000
        choice = response.choices[0] if response.choices else None
        message = choice.message if choice else None

        text = message.content if message else ""
        # Expose tool_calls on the response so the orchestrator tool loop can read them
        tool_calls = message.tool_calls if message and message.tool_calls else None

        usage = getattr(response, "usage", None)
        output_tokens = (
            int(usage.completion_tokens) if usage and usage.completion_tokens else 0
        )

        result = LLMResponse(
            text=text or "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            model_used=self._model_id,
        )
        # Attach tool_calls directly on the result object so _generate() can
        # detect them with getattr(response, "tool_calls", None).
        result.tool_calls = tool_calls
        return result