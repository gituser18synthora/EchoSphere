"""Google Gemini LLM provider — lazy-imports the google-genai SDK.

Migrated from VoiceBot/adapters/llm/google_adapter.py
(google-genai, the successor of the deprecated google-generativeai SDK).
Tool calling is not supported for this provider: passing a non-empty
``tools`` list raises ProviderError(invalid_input). Adds a stream()
implementation via ``client.aio.models.generate_content_stream``.
"""

import asyncio
from collections.abc import AsyncIterator

from backend.config import get_settings
from backend.providers.base import LLMProvider, LLMResult, ProviderConfig, ProviderError

_DEFAULT_MODEL = "gemini-2.0-flash"


class GoogleLLM(LLMProvider):
    name = "google-llm"

    def __init__(self, config: ProviderConfig) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ProviderError(
                self.name, "invalid_input",
                "google-genai SDK is not installed; run `pip install google-genai` "
                "to use the google LLM provider",
            ) from exc
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.llm_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        self._types = types
        self._client = genai.Client(api_key=key)
        self._model = config.model or _DEFAULT_MODEL
        self._timeout = config.timeout_seconds

    def _to_contents(self, messages: list[dict]) -> list:
        """Translate OpenAI-style messages to Gemini Contents (user/model),
        merging consecutive same-role turns. System messages are skipped —
        they travel via system_instruction."""
        types = self._types
        contents: list = []
        for message in messages:
            role = (message.get("role") or "user").lower()
            if role == "system":
                continue
            text = message.get("content") or ""
            if isinstance(text, list):
                text = " ".join(
                    part.get("text", "") for part in text if isinstance(part, dict)
                )
            gemini_role = "user" if role == "user" else "model"
            part = types.Part(text=str(text))
            if contents and contents[-1].role == gemini_role:
                parts = list(contents[-1].parts) + [part]
                contents[-1] = types.Content(role=gemini_role, parts=parts)
            else:
                contents.append(types.Content(role=gemini_role, parts=[part]))
        if not contents:
            contents.append(
                types.Content(role="user", parts=[types.Part(text="(no user message)")])
            )
        return contents

    def _generation_config(self, system: str | None, temperature: float, max_tokens: int):
        return self._types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=max_tokens,
            temperature=temperature,
        )

    @staticmethod
    def _reject_tools(provider: str, tools: list[dict] | None) -> None:
        if tools:
            raise ProviderError(
                provider, "invalid_input", "tool calling not supported for google provider"
            )

    async def generate(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> LLMResult:
        self._reject_tools(self.name, tools)
        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=self._to_contents(messages),
                    config=self._generation_config(system, temperature, max_tokens),
                ),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            raise ProviderError(
                self.name, "timeout", f"Request timed out after {self._timeout}s"
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK error types are lazy-loaded
            raise _categorize(self.name, exc) from exc

        text = ""
        finish_reason = None
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            candidate = candidates[0]
            content = getattr(candidate, "content", None)
            for part in (getattr(content, "parts", None) or []):
                text += getattr(part, "text", "") or ""
            raw_finish = getattr(candidate, "finish_reason", None)
            finish_reason = str(raw_finish) if raw_finish is not None else None
        usage = getattr(response, "usage_metadata", None)
        return LLMResult(
            text=text,
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
            finish_reason=finish_reason,
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
        self._reject_tools(self.name, tools)
        try:
            stream = await asyncio.wait_for(
                self._client.aio.models.generate_content_stream(
                    model=self._model,
                    contents=self._to_contents(messages),
                    config=self._generation_config(system, temperature, max_tokens),
                ),
                timeout=self._timeout,
            )
            async for chunk in stream:
                delta = getattr(chunk, "text", None)
                if delta:
                    yield delta
        except TimeoutError as exc:
            raise ProviderError(
                self.name, "timeout", f"Request timed out after {self._timeout}s"
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK error types are lazy-loaded
            raise _categorize(self.name, exc) from exc


def _categorize(provider: str, exc: Exception) -> ProviderError:
    text = str(exc)
    lowered = text.lower()
    if "401" in text or "403" in text or "unauthenticated" in lowered or "api key" in lowered or "permission" in lowered:
        return ProviderError(provider, "auth", text[:200])
    if "429" in text or "resource_exhausted" in lowered or "rate" in lowered or "quota" in lowered:
        return ProviderError(provider, "rate_limit", text[:200])
    if "deadline" in lowered or "timeout" in lowered or "timed out" in lowered:
        return ProviderError(provider, "timeout", text[:200])
    return ProviderError(provider, "upstream", text[:200])
