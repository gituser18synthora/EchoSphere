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


def to_provider_language(
    provider: str, platform_code: str, model_languages: list[str] | None = None
) -> str | None:
    """Translate a platform locale into the code the provider expects.

    When ``model_languages`` is given, the result is constrained to that list
    (exact locale first, then alias, then bare ISO 639-1 prefix). Returns None
    if the model does not support the language.
    """
    alias = _PLATFORM_TO_PROVIDER.get(provider, {}).get(platform_code)
    if not model_languages:
        return alias or platform_code
    candidates = [c for c in (platform_code, alias, platform_code.split("-")[0]) if c]
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
