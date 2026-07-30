"""Locale mapping between platform language codes and provider wire codes.

The platform's language master (``supported_languages.code``) uses BCP-47-style
locale codes (``hi-IN``, ``en-US``). Providers differ:

- Sarvam uses locale codes but spells Odia ``od-IN`` (platform: ``or-IN``).
- ElevenLabs uses bare ISO 639-1 codes (``hi``, ``en``).

``provider_models.languages`` stores each model's languages in the provider's
native form. The helpers here translate between the two shapes so language IDs
saved in the database are always platform locale codes.
"""

from __future__ import annotations

# Platform locale → provider wire code (only true renames belong here).
_PLATFORM_TO_PROVIDER: dict[str, dict[str, str]] = {
    "sarvam": {"or-IN": "od-IN"},
}

_PROVIDER_TO_PLATFORM: dict[str, dict[str, str]] = {
    provider: {v: k for k, v in aliases.items()}
    for provider, aliases in _PLATFORM_TO_PROVIDER.items()
}

# Bare ISO 639-1 → full locale, for providers whose wire protocol only accepts
# full locale codes. Sarvam rejects bare codes ("en") with a 422, so both the
# REST and the WebSocket implementation canonicalize through this one table.
_SHORT_TO_LOCALE: dict[str, dict[str, str]] = {
    "sarvam": {
        "en": "en-IN", "hi": "hi-IN", "bn": "bn-IN", "kn": "kn-IN", "ml": "ml-IN",
        "mr": "mr-IN", "od": "or-IN", "or": "or-IN", "pa": "pa-IN", "ta": "ta-IN",
        "te": "te-IN", "gu": "gu-IN",
    },
}

# Locales the Sarvam TTS API accepts (platform form — Odia stays "or-IN" here;
# the wire alias above renames it where the provider expects "od-IN").
SARVAM_SUPPORTED_LOCALES = frozenset({
    "hi-IN", "bn-IN", "kn-IN", "ml-IN", "mr-IN",
    "pa-IN", "raj-IN", "ta-IN", "te-IN",
    "en-IN", "gu-IN", "or-IN",
})


def short_code_to_locale(provider: str, code: str) -> str:
    """Expand a bare ISO 639-1 code to the provider's full locale ("en" →
    "en-IN" for Sarvam). Full locales and unknown codes pass through."""
    if code and "-" not in code:
        return _SHORT_TO_LOCALE.get(provider, {}).get(code.lower(), code)
    return code


def to_provider_language(
    provider: str, platform_code: str, model_languages: list[str] | None = None
) -> str | None:
    """Translate a platform locale into the code the provider expects.

    Bare ISO 639-1 inputs are first expanded to the provider's full locale
    where the provider requires one. When ``model_languages`` is given, the
    result is constrained to that list (exact locale first, then alias, then
    bare ISO 639-1 prefix). Returns None if the model does not support the
    language.
    """
    expanded = short_code_to_locale(provider, platform_code)
    alias = _PLATFORM_TO_PROVIDER.get(provider, {}).get(expanded)
    if not model_languages:
        return alias or expanded
    candidates = [c for c in (expanded, alias, expanded.split("-")[0]) if c]
    for candidate in candidates:
        if candidate in model_languages:
            return candidate
    return None


def matches_model_language(
    provider: str, platform_code: str, model_languages: list[str] | None
) -> bool:
    """True when a provider model supports the given platform locale.

    An empty/None ``model_languages`` list means the model is
    language-agnostic (e.g. LLMs, mock providers).
    """
    if not model_languages:
        return True
    return to_provider_language(provider, platform_code, model_languages) is not None


def to_platform_language(provider: str, provider_code: str) -> str:
    """Translate a provider wire code back into the platform locale form."""
    return _PROVIDER_TO_PLATFORM.get(provider, {}).get(provider_code, provider_code)
