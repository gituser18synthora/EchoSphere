"""Google Gemini LLM adapter using the new google-genai SDK."""

import asyncio
import time
from typing import Any

from google import genai
from google.genai import types
from google.api_core import exceptions as google_exceptions

from adapters.base import AdapterException, LLMAdapter, LLMResponse
from config.settings import Settings


def _messages_to_contents(messages: list[dict]) -> list[types.Content]:
    """
    Translate unified messages format to Gemini Contents list.

    Rules:
    - Skip system messages (system_prompt handled separately via system_instruction)
    - user   -> role="user"
    - assistant -> role="model"
    - Consecutive same-role messages are merged into one Content with multiple Parts
    """
    contents: list[types.Content] = []

    for m in messages:
        role = (m.get("role") or "user").lower()

        if role == "system":
            continue  # handled via system_instruction at client level

        text = m.get("content") or ""
        if isinstance(text, list):
            # Handle OpenAI-style content blocks
            text = " ".join(
                p.get("text", "") for p in text if isinstance(p, dict)
            )

        gemini_role = "user" if role == "user" else "model"

        # Merge consecutive same-role turns
        if contents and contents[-1].role == gemini_role:
            existing_parts = list(contents[-1].parts)
            existing_parts.append(types.Part(text=str(text)))
            contents[-1] = types.Content(role=gemini_role, parts=existing_parts)
        else:
            contents.append(
                types.Content(
                    role=gemini_role,
                    parts=[types.Part(text=str(text))],
                )
            )

    return contents


class GoogleLLMAdapter(LLMAdapter):
    """
    Google Gemini adapter using google-genai SDK (replaces deprecated google-generativeai).

    System prompt is passed as system_instruction in GenerateContentConfig.
    Uses async client throughout — no executor fallback needed.
    """

    def __init__(self, model_id: str = "gemini-2.0-flash", **kwargs: Any) -> None:
        settings = Settings()
        self._model_id = model_id
        self._timeout: float = getattr(settings, "llm_max_response_latency", 3.0)

        # Async client — one instance, reused across calls
        self._client = genai.Client(api_key=settings.google_api_key)

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int = 300,
        temperature: float = 0.7,
    ) -> LLMResponse:

        contents = _messages_to_contents(messages)

        # Gemini requires at least one content item
        if not contents:
            contents = [
                types.Content(role="user", parts=[types.Part(text="")])
            ]

        config = types.GenerateContentConfig(
            system_instruction=system_prompt if system_prompt else None,
            max_output_tokens=max_tokens,
            temperature=temperature,
        )

        start = time.perf_counter()

        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model_id,
                    contents=contents,
                    config=config,
                ),
                timeout=self._timeout,
            )

        except asyncio.TimeoutError as e:
            raise AdapterException(
                f"Google LLM timed out after {self._timeout}s"
            ) from e
        except google_exceptions.Unauthenticated as e:
            raise AdapterException(f"Google authentication failed: {e}") from e
        except google_exceptions.ResourceExhausted as e:
            raise AdapterException(
                f"Google rate limit exceeded: {e}",
                retry_after=60.0,
            ) from e
        except google_exceptions.DeadlineExceeded as e:
            raise AdapterException(f"Google deadline exceeded: {e}") from e
        except google_exceptions.ServiceUnavailable as e:
            raise AdapterException(f"Google service unavailable: {e}") from e
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "rate" in err or "quota" in err:
                raise AdapterException(
                    f"Google rate limit: {e}", retry_after=60.0
                ) from e
            if "401" in err or "auth" in err or "api key" in err:
                raise AdapterException(f"Google authentication failed: {e}") from e
            raise AdapterException(f"Google API error: {e}") from e

        latency_ms = (time.perf_counter() - start) * 1000

        # Extract text from response
        text = ""
        if response.candidates:
            for part in response.candidates[0].content.parts:
                text += getattr(part, "text", "") or ""

        # Token usage
        usage = getattr(response, "usage_metadata", None)
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)

        return LLMResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            model_used=self._model_id,
        )