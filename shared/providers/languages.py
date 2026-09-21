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
        "te": "te-IN", "gu": "gu-IN", "ur": "ur-IN",
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


# ── EchoSphere language scope ────────────────────────────────────────────────
# Everything below covers ONLY the languages this platform has enabled in
# ``supported_languages`` — verified 2026-09-21 against the live DB, and all
# nine are Indian:
#
#   en-IN  hi-IN  mr-IN  te-IN  ta-IN  ml-IN  pa-IN  gu-IN  ur-IN
#
# This is deliberately NOT a vendor language catalog. A provider's other
# languages are not modelled here: what each provider model accepts overall
# already lives in ``provider_models.languages`` (the DB catalog), and a
# language only earns an entry below once the platform enables it AND the
# combination has been verified against the vendor's live API.

#: Platform locales Sarvam STT pins the recognizer to. Sarvam accepts more
#: languages than this; the set is the pre-existing TTS locale set plus Urdu,
#: which Sarvam transcribes but cannot speak — the one enabled language whose
#: STT and TTS support differ.
SARVAM_STT_SUPPORTED_LOCALES = SARVAM_SUPPORTED_LOCALES | {"ur-IN"}

# ElevenLabs ``language_code`` values for EchoSphere languages, on the models
# that accept the parameter at all (Flash/Turbo v2.5 — Eleven v3 takes no
# language_code). Verified 2026-09-21 against
# ``GET https://api.elevenlabs.io/v1/models`` plus a live probe of both the
# REST endpoint and the multi-stream WebSocket.
#
# The six enabled languages that are absent — mr-IN, te-IN, ml-IN, pa-IN,
# gu-IN, ur-IN — are NOT in the v2.5 language set. They get no code at all:
# ElevenLabs answers an unsupported value with HTTP 400
# ``{"status":"unsupported_language"}`` and, on the socket, a
# ``{"error":"unsupported_language","code":1008}`` frame with no audio
# whatsoever, so a guessed code turns the bot mute.
_ELEVENLABS_V2_5_CODES: dict[str, str] = {
    "en-IN": "en",
    "hi-IN": "hi",
    "ta-IN": "ta",
}
#: The same three, as bare codes, for callers that hold the short form.
_ELEVENLABS_V2_5_BARE = frozenset(_ELEVENLABS_V2_5_CODES.values())

#: The platform's enabled languages (``supported_languages.enabled = 1``).
ECHOSPHERE_LOCALES = frozenset({
    "en-IN", "hi-IN", "mr-IN", "te-IN", "ta-IN",
    "ml-IN", "pa-IN", "gu-IN", "ur-IN",
})

# Which of OUR languages each catalogued ElevenLabs model can actually speak,
# from ``GET https://api.elevenlabs.io/v1/models`` (2026-09-21). Only the nine
# are listed — the vendor's other languages are not modelled here.
#
# Eleven v3 speaks all nine but takes no ``language_code`` parameter, so its
# support is expressed by choosing the model, not by a wire code.
_ELEVENLABS_MODEL_LOCALES: dict[str, frozenset[str]] = {
    "eleven_flash_v2_5": frozenset(_ELEVENLABS_V2_5_CODES),
    "eleven_turbo_v2_5": frozenset(_ELEVENLABS_V2_5_CODES),
    "eleven_v3": ECHOSPHERE_LOCALES,
}

#: ElevenLabs models that accept the ``language_code`` enforcement parameter.
ELEVENLABS_LANGUAGE_ENFORCING_MODELS = frozenset({
    "eleven_flash_v2_5", "eleven_turbo_v2_5",
})


def _canonical_locale(code: str | None) -> str:
    """BCP-47 casing for a platform code ("HI-in"/"hi_in" → "hi-IN").

    Locales reach the providers from stored config, API payloads and live
    detection labels; matching a provider's enum must not depend on how they
    were typed.
    """
    parts = (code or "").strip().replace("_", "-").split("-")
    if not parts[0]:
        return ""
    return "-".join(
        [parts[0].lower()]
        + [part.upper() if len(part) == 2 else part.lower() for part in parts[1:] if part]
    )


def elevenlabs_supports_language(
    model: str | None, platform_code: str | None
) -> bool | None:
    """Can this ElevenLabs model speak this EchoSphere language?

    ``True``/``False`` for one of the platform's nine languages on a
    catalogued model. ``None`` means the combination is outside what this
    module models — an unknown model, or a locale the platform has not
    enabled — and callers must NOT read that as a rejection.
    """
    locale = _canonical_locale(platform_code)
    speaks = _ELEVENLABS_MODEL_LOCALES.get((model or "").strip())
    if speaks is None or locale not in ECHOSPHERE_LOCALES:
        return None
    return locale in speaks


def elevenlabs_models_speaking(platform_code: str | None) -> list[str]:
    """Catalogued ElevenLabs models that can speak this EchoSphere language —
    what an "unsupported language" error should point the operator at."""
    locale = _canonical_locale(platform_code)
    return [
        model for model, speaks in _ELEVENLABS_MODEL_LOCALES.items()
        if locale in speaks
    ]


def elevenlabs_language_code(model: str | None, platform_code: str | None) -> str | None:
    """The ``language_code`` to send to ElevenLabs, or None to omit it.

    None means the model takes no ``language_code`` at all (Eleven v3) or no
    language is configured. Callers must check
    :func:`elevenlabs_supports_language` FIRST: a model that cannot speak the
    language has to be refused, never sent a guessed code — ElevenLabs answers
    an unsupported value with HTTP 400 / a 1008 ``unsupported_language`` frame
    and synthesizes nothing.
    """
    if (model or "").strip() not in ELEVENLABS_LANGUAGE_ENFORCING_MODELS:
        return None
    locale = _canonical_locale(platform_code)
    if locale in _ELEVENLABS_V2_5_CODES:
        return _ELEVENLABS_V2_5_CODES[locale]
    if locale in _ELEVENLABS_V2_5_BARE:
        return locale
    if elevenlabs_supports_language(model, locale) is False:
        # Belt and braces: a language this model provably cannot speak never
        # gets a code, even if a caller skipped the support check.
        return None
    # Outside the platform's own languages this module holds no capability
    # data, so the pre-existing behaviour stands: send the bare subtag and let
    # ElevenLabs be the judge.
    return locale.split("-")[0] or None


def sarvam_stt_language_code(language: str | None) -> str:
    """Wire ``language_code`` for Sarvam STT ("hi-IN"/"hi" → "hi-IN"); blank,
    "auto" and languages the recognizer cannot pin request auto-detect."""
    locale = short_code_to_locale("sarvam", _canonical_locale(language))
    if not locale or locale.lower() in ("auto", "unknown"):
        return "unknown"
    # Accept the provider's own spelling back ("od-IN" → "or-IN"): detected
    # labels are fed straight back in on the language-rescue path.
    locale = to_platform_language("sarvam", locale)
    if locale not in SARVAM_STT_SUPPORTED_LOCALES:
        return "unknown"
    return to_provider_language("sarvam", locale) or "unknown"
