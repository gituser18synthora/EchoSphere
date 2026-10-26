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


def _is_reasoning_chat_model(model: str) -> bool:
    """Models whose Chat Completions contract uses reasoning-era controls."""
    normalized = (model or "").lower()
    return normalized.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))


def _is_original_gpt5(model: str) -> bool:
    normalized = (model or "").lower()
    return normalized == "gpt-5" or normalized.startswith(
        ("gpt-5-2025", "gpt-5-mini", "gpt-5-nano")
    )


#: Families whose lowest reasoning effort is ``none`` — sampling controls
#: (temperature) are accepted again at that effort. Probed against the live
#: API on 2026-10-23: gpt-5.1, 5.2, 5.4(-mini/-nano), 5.5, 5.6-{luna,terra,sol},
#: gpt-6-luna and gpt-6-sol accept ``none`` (+ temperature=0); the original
#: gpt-5 family's floor is ``minimal``; gpt-6.1-sol and the o-series start at
#: ``low`` (left at their model default here).
_NONE_EFFORT_FAMILIES = ("gpt-5.1", "gpt-5.2", "gpt-5.4", "gpt-5.5", "gpt-5.6", "gpt-6-")
#: Accepted values of the per-engine ``reasoning_effort`` option.
REASONING_EFFORT_OPTIONS = frozenset({"lowest", "none", "minimal", "low", "medium", "high"})


def lowest_reasoning_effort(model: str) -> str | None:
    """The lowest ``reasoning_effort`` the model accepts, or None when its
    floor is ``low`` or it is not a reasoning-era model (use its default)."""
    normalized = (model or "").lower()
    if _is_original_gpt5(normalized):
        return "minimal"
    if normalized.startswith(_NONE_EFFORT_FAMILIES):
        return "none"
    return None


def _chat_generation_params(
    model: str, *, temperature: float, max_tokens: int,
    reasoning_effort: str | None = None,
) -> dict:
    """Build model-compatible Chat Completions generation parameters.

    GPT-5/GPT-6/reasoning models reject the legacy ``max_tokens`` field. The
    original GPT-5 family supports ``minimal`` reasoning, which is the right
    latency profile for live voice. Later generations are left at their model
    default unless the engine asks for an effort: ``reasoning_effort="lowest"``
    resolves to the lowest value the family accepts (:func:`lowest_reasoning_effort`),
    any other value is sent as given. At effort ``none`` the sampling
    temperature is accepted again and is sent, so a T=0 classification call
    stays deterministic on the newer models too.
    """
    if not _is_reasoning_chat_model(model):
        return {"temperature": temperature, "max_tokens": max_tokens}

    params: dict = {"max_completion_tokens": max_tokens}
    effort = reasoning_effort
    if effort == "lowest":
        effort = lowest_reasoning_effort(model)
    elif effort is None and _is_original_gpt5(model):
        effort = "minimal"
    if effort:
        params["reasoning_effort"] = effort
    if effort == "none":
        params["temperature"] = temperature
    return params


def _response_meta(response, requested_model: str) -> dict:
    """Provider-reported facts of one completion: the tier it actually ran on
    (a "fast" request reports "priority" on older models and "default" when
    downgraded), the cached share of the prompt and the resolved model."""
    usage = getattr(response, "usage", None)
    details = getattr(usage, "prompt_tokens_details", None) if usage is not None else None
    return {
        "service_tier": getattr(response, "service_tier", None),
        "cached_tokens": int(getattr(details, "cached_tokens", 0) or 0),
        "model": getattr(response, "model", None) or requested_model,
    }


class OpenAILLM(LLMProvider):
    name = "openai-llm"
    supports_json_output = True

    def __init__(self, config: ProviderConfig) -> None:
        settings = get_settings()
        key = settings.resolve_secret(
            config.api_key_reference or settings.llm_api_key_reference
        )
        if not key:
            raise ProviderError(self.name, "auth", "Missing API key reference")
        # max_retries=0: the SDK's default 2 hidden retries stack under the
        # callers' own bounded retries and blow the voice latency budget —
        # retry policy is owned by the brain/API layers, never the SDK.
        self._client = AsyncOpenAI(
            api_key=key, timeout=config.timeout_seconds, max_retries=0
        )
        self._model = config.model or settings.llm_model or "gpt-4o-mini"
        # Optional per-engine request options (ProviderConfig.extra):
        # - "service_tier": e.g. "fast" (alias "priority") for the hard-deadline
        #   Goal Engine decision call (measured 2026-10-07 on the Zepto MDND
        #   prompt: full decision 1.70 s → 1.50 s p50);
        # - "prompt_cache_key": routes requests that share a prefix to the
        #   same prompt cache (one key per bot);
        # - "reasoning_effort": see _chat_generation_params ("lowest" = the
        #   lowest value the model family accepts).
        extra = dict(config.extra or {})
        self._request_extra = {
            k: str(v) for k, v in extra.items()
            if k in ("service_tier", "prompt_cache_key") and v
        }
        effort = str(extra.get("reasoning_effort") or "").strip().lower()
        self._reasoning_effort = effort if effort in REASONING_EFFORT_OPTIONS else None
        # What the most recent call actually ran on (provider report), for
        # the call's metrics: tier, cached prompt tokens, model. Providers
        # are cached per config and shared by a bot's calls: callers read it
        # right after their own call, and per-call tallies live in the call.
        self.last_response_meta: dict | None = None

    def request_options(self) -> dict:
        """The request options every call of this engine sends: tier,
        prompt-cache key and the reasoning effort as resolved for the model
        ("lowest" is sent as "none" to gpt-6-luna). Observability only."""
        options = dict(getattr(self, "_request_extra", {}))
        resolved = _chat_generation_params(
            self._model, temperature=0.0, max_tokens=1,
            reasoning_effort=getattr(self, "_reasoning_effort", None),
        )
        if "reasoning_effort" in resolved:
            options["reasoning_effort"] = resolved["reasoning_effort"]
        return options

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
        response_format: dict | None = None,
    ) -> LLMResult:
        kwargs: dict = {}
        if response_format is not None:
            kwargs["response_format"] = response_format
        if tools:
            kwargs["tools"] = tools
        kwargs.update(_chat_generation_params(
            self._model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=getattr(self, "_reasoning_effort", None),
        ))
        kwargs.update(getattr(self, "_request_extra", {}))
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=self._build_messages(messages, system),
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(self.name, "upstream", str(exc)[:200]) from exc
        self.last_response_meta = _response_meta(response, self._model)
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
            cached_tokens=int((self.last_response_meta or {}).get("cached_tokens") or 0),
            service_tier=(self.last_response_meta or {}).get("service_tier"),
            model=(self.last_response_meta or {}).get("model"),
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
        kwargs = _chat_generation_params(
            self._model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=getattr(self, "_reasoning_effort", None),
        )
        kwargs.update(getattr(self, "_request_extra", {}))
        if tools:
            kwargs["tools"] = tools
        try:
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=self._build_messages(messages, system),
                stream=True,
                # Final chunk carries provider-reported token usage — the
                # billing source of truth for the streaming voice path.
                stream_options={"include_usage": True},
                **kwargs,
            )
            tier = model = None
            async for chunk in stream:
                tier = getattr(chunk, "service_tier", None) or tier
                model = getattr(chunk, "model", None) or model
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
            # A completed stream (a reply cut by barge-in never gets here).
            self.last_response_meta = {
                "service_tier": tier,
                "cached_tokens": self.last_stream_usage.cached_tokens if self.last_stream_usage else 0,
                "model": model or self._model,
            }
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(self.name, "upstream", str(exc)[:200]) from exc
