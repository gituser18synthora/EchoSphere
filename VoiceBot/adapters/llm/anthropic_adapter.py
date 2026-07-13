"""Anthropic LLM adapter using AsyncAnthropic."""

import asyncio
import time
from typing import Any

from anthropic import AnthropicError, APIError, APIConnectionError, AsyncAnthropic

from adapters.base import AdapterException, LLMAdapter, LLMResponse
from config.settings import Settings


def _merge_to_alternating(messages: list[dict]) -> list[dict[str, str]]:
    """Convert to strictly alternating user/assistant. Merge consecutive same role."""
    result: list[dict[str, str]] = []
    for m in messages:
        role = (m.get("role") or "user").lower()
        if role == "system":
            continue
        if role not in ("user", "assistant"):
            role = "user"
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        content = str(content).strip()
        if not content:
            continue
        if result and result[-1]["role"] == role:
            result[-1]["content"] = result[-1]["content"] + "\n" + content
        else:
            result.append({"role": role, "content": content})
    return result


class AnthropicLLMAdapter(LLMAdapter):
    """Anthropic Messages API via AsyncAnthropic. System prompt in `system` param."""

    def __init__(self, model_id: str = "claude-3-5-sonnet-20241022", **kwargs: Any) -> None:
        settings = Settings()
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
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
    ) -> LLMResponse:
        anthropic_messages = _merge_to_alternating(messages)
        if not anthropic_messages or anthropic_messages[0]["role"] != "user":
            anthropic_messages.insert(0, {"role": "user", "content": ""})
            anthropic_messages[0]["content"] = "(no user message)"

        start = time.perf_counter()

        async def _call() -> Any:
            return await self._client.messages.create(
                model=self._model_id,
                max_tokens=max_tokens,
                system=system_prompt or "",
                messages=anthropic_messages,
                temperature=temperature,
            )

        try:
            response = await asyncio.wait_for(_call(), timeout=self._timeout)
        except asyncio.TimeoutError as e:
            raise AdapterException(
                f"Anthropic request timed out after {self._timeout}s"
            ) from e
        except AnthropicError as e:
            status = getattr(e, "status_code", None)
            retry_after = None
            if hasattr(e, "response") and e.response is not None:
                h = getattr(e.response, "headers", None) or {}
                ra = h.get("retry-after")
                if ra is not None:
                    try:
                        retry_after = float(ra)
                    except (TypeError, ValueError):
                        pass
            if status == 401 or "auth" in str(e).lower():
                raise AdapterException(
                    f"Anthropic authentication failed: {e}"
                ) from e
            if status == 429 or "rate" in str(e).lower():
                raise AdapterException(
                    f"Anthropic rate limit exceeded: {e}",
                    retry_after=retry_after,
                ) from e
            raise AdapterException(f"Anthropic error: {e}") from e
        except APIConnectionError as e:
            raise AdapterException(
                f"Anthropic service unavailable: {e}"
            ) from e
        except APIError as e:
            status = getattr(e, "status_code", None)
            if status == 503 or (status and status >= 500):
                raise AdapterException(
                    f"Anthropic service unavailable: {e}"
                ) from e
            raise AdapterException(f"Anthropic API error: {e}") from e

        latency_ms = (time.perf_counter() - start) * 1000
        text = ""
        for block in getattr(response, "content", []):
            if getattr(block, "type", None) == "text":
                text += getattr(block, "text", "") or ""
        usage = getattr(response, "usage", None)
        input_tokens = int(usage.input_tokens) if usage else 0
        output_tokens = int(usage.output_tokens) if usage else 0
        return LLMResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            model_used=self._model_id,
        )
