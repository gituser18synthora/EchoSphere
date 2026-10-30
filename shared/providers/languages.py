"""Locale mapping between platform language codes and provider wire codes.

The platform's language master (``supported_languages.code``) uses BCP-47-style
locale codes (``hi-IN``, ``en-US``). Providers differ:

- Sarvam uses locale codes but spells Odia ``od-IN`` (platform: ``or-IN``).
- ElevenLabs uses bare ISO 639-1 codes (``hi``, ``en``).
- Deepgram TTS has no language parameter at all: the language is part of
  the voice model id (``aura-2-thalia-en``), so a locale only selects
  which voices exist — nothing locale-shaped is ever sent to Deepgram.

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
    # Same 74-language model as eleven_v3, tuned for realtime dialogue
    # (GET /v1/models, 2026-09-22: both list all nine of our languages).
    # SYNTHESIS is verified for hi/mr/ur/te/ml over the Text-to-Dialogue
    # socket; per-language PRONUNCIATION is not yet signed off.
    "eleven_v3_conversational": ECHOSPHERE_LOCALES,
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


#: ElevenLabs models that stream in realtime (either WebSocket endpoint).
#: ``eleven_v3`` is NOT one of them: it is REST-only, so it can be a bot's
#: DEFAULT engine (every reply then synthesizes over the segmented REST path)
#: or drive a preview, but it cannot serve a per-language override or a
#: fallback inside the streaming router — those are rejected by voice-settings
#: validation ("does not support realtime streaming").
#: ``eleven_v3_conversational`` CAN: it streams over the Text-to-Dialogue
#: socket and is valid everywhere a streaming model is required.
ELEVENLABS_STREAMING_MODELS = frozenset({
    "eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_v3_conversational",
})

#: ElevenLabs models served by the Text-to-Dialogue WebSocket rather than the
#: text-to-speech one. They stream in realtime, but over a different endpoint
#: and wire protocol — see shared/providers/tts/elevenlabs_v3_ws.py. The
#: text-to-speech socket answers these model ids with an HTTP 400 handshake
#: rejection, so routing is per MODEL, not per provider.
ELEVENLABS_DIALOGUE_MODELS = frozenset({
    "eleven_v3_conversational",
})


def _elevenlabs_unsupported_message(
    model: str | None, platform_code: str | None
) -> str:
    """Why this ElevenLabs model cannot speak the language, and what instead.

    The alternative has to say WHERE it can be selected, not just name it:
    Eleven v3 speaks every EchoSphere language but has no realtime streaming,
    so "choose eleven_v3" alone walks the operator into the per-language
    override being rejected by the next validation step.

    It lives here so the provider-neutral dispatcher below can reach it
    without the callers re-deriving it from :func:`elevenlabs_models_speaking`.
    """
    alternatives = [
        m for m in elevenlabs_models_speaking(platform_code) if m != model
    ]
    streaming = [m for m in alternatives if m in ELEVENLABS_STREAMING_MODELS]
    rest_only = [m for m in alternatives if m not in ELEVENLABS_STREAMING_MODELS]
    base = (
        f"ElevenLabs model '{model}' does not support language "
        f"'{platform_code}'."
    )
    if streaming:
        return f"{base} Use {' or '.join(streaming)} for this language."
    if rest_only:
        return (
            f"{base} Only {' or '.join(rest_only)} speaks it, and that model has "
            "no realtime streaming — select it as the bot's DEFAULT TTS model "
            "(every reply then synthesizes over REST), or map this language to "
            "a streaming provider in the per-language voice settings."
        )
    return f"{base} No configured ElevenLabs model speaks it."


# ── Deepgram TTS (Aura / Aura-2) ─────────────────────────────────────────────
# Verified 2026-09-21 against developers.deepgram.com/docs/tts-models.
#
# Deepgram TTS has NO ``language`` request parameter: the language is baked
# into the voice model id, whose suffix is the language tag
# (``aura-2-thalia-en`` → ``en``, ``aura-2-estrella-es`` → ``es``). The
# platform locale is therefore never sent to Deepgram in any form — it only
# decides which voices are selectable. Mapping a locale here means "which
# Deepgram language tag do our voices for this locale carry", not "what do we
# put in a language field".
#
# Deepgram's published TTS language list is short and closed:
#
#   Aura-2 : en, es, de, nl, fr, it, ja
#   Aura   : en only
#
# NO Indian language is on it — not Hindi, Tamil, Telugu, Malayalam, Marathi,
# Gujarati, Punjabi or Urdu — and the India regional endpoint
# (api.in.deepgram.com) does not change that: it is a data-residency host
# running the same models, so it must never be read as Indic support. A
# locale absent from the table below is genuinely unsupported, and the
# adapters refuse it rather than sending a guess. Deepgram would otherwise
# read Devanagari with an English voice and return confident gibberish — it
# has no language field to reject, so the refusal has to happen on our side.
#
# ``en-IN`` IS mapped, because Deepgram genuinely speaks English — but only
# with American, British, Australian, Irish and Filipino accents. There is no
# Indian-English voice; a bot that needs one belongs on another provider.
_DEEPGRAM_TTS_LOCALE_TAGS: dict[str, str] = {
    # English — accent is American/British/Australian, never Indian.
    "en-IN": "en", "en-US": "en", "en-GB": "en", "en-AU": "en", "en-IE": "en",
    "en-PH": "en",
    "es-US": "es", "es-MX": "es", "es-ES": "es", "es-419": "es",
    "de-DE": "de",
    "nl-NL": "nl", "nl-BE": "nl",
    "fr-FR": "fr", "fr-CA": "fr",
    "it-IT": "it",
    "ja-JP": "ja",
}

#: Language tags each catalogued Deepgram TTS model (Aura generation) speaks.
#: The platform's model codes are the Aura *families*; an individual voice
#: (``aura-2-thalia-en``) is the wire ``model`` query parameter.
_DEEPGRAM_TTS_MODEL_TAGS: dict[str, frozenset[str]] = {
    "aura-2": frozenset({"en", "es", "de", "nl", "fr", "it", "ja"}),
    "aura": frozenset({"en"}),
}

#: Deepgram TTS models available on the realtime ``/v1/speak`` WebSocket.
#: Both Aura generations stream; the set exists so the router and the preview
#: can ask the same question they ask of every other provider.
DEEPGRAM_STREAMING_MODELS = frozenset(_DEEPGRAM_TTS_MODEL_TAGS)


def deepgram_tts_language_tag(platform_code: str | None) -> str | None:
    """Deepgram language tag for a platform locale, or None if it has none.

    None means no Deepgram voice exists for that language at all — the caller
    must refuse, never fall back to an English voice speaking foreign text.
    """
    return _DEEPGRAM_TTS_LOCALE_TAGS.get(_canonical_locale(platform_code))


def deepgram_voice_language_tag(voice_model: str | None) -> str | None:
    """Language tag carried by a Deepgram voice id (``aura-2-thalia-en`` →
    ``en``). None when the id does not look like an Aura voice."""
    parts = (voice_model or "").strip().lower().split("-")
    if len(parts) < 3 or parts[0] != "aura":
        return None
    return parts[-1] or None


def deepgram_voice_model_family(voice_model: str | None) -> str | None:
    """Aura family a voice id belongs to (``aura-2-thalia-en`` → ``aura-2``,
    ``aura-asteria-en`` → ``aura``).

    Used to check that the selected catalog model and the selected voice are
    the same generation before either reaches the wire.
    """
    parts = (voice_model or "").strip().lower().split("-")
    if len(parts) < 3 or parts[0] != "aura":
        return None
    return "aura-2" if parts[1] == "2" else "aura"


def deepgram_supports_language(
    model: str | None, platform_code: str | None
) -> bool | None:
    """Can this Deepgram TTS model speak this language?

    Unlike the ElevenLabs equivalent this is authoritative for every locale,
    because Deepgram's TTS language list is closed and published: a locale
    with no entry in the tag table has no Deepgram voice, full stop. ``None``
    is returned only for an unknown model code, where this module holds no
    capability data and callers must not read the answer as a rejection.
    """
    speaks = _DEEPGRAM_TTS_MODEL_TAGS.get((model or "").strip())
    if speaks is None:
        return None
    tag = deepgram_tts_language_tag(platform_code)
    return tag is not None and tag in speaks


def deepgram_models_speaking(platform_code: str | None) -> list[str]:
    """Catalogued Deepgram TTS models that can speak this language."""
    tag = deepgram_tts_language_tag(platform_code)
    if tag is None:
        return []
    return [m for m, tags in _DEEPGRAM_TTS_MODEL_TAGS.items() if tag in tags]


def deepgram_unsupported_language_message(
    model: str | None, platform_code: str | None
) -> str:
    """Why this Deepgram model cannot speak the language, and what to do."""
    alternatives = [m for m in deepgram_models_speaking(platform_code) if m != model]
    base = f"Deepgram model '{model}' does not support language '{platform_code}'."
    if alternatives:
        return f"{base} Use {' or '.join(alternatives)} for this language."
    return (
        f"{base} Deepgram text-to-speech speaks only English, Spanish, German, "
        "Dutch, French, Italian and Japanese — it has no voice for this "
        "language, and the India endpoint (api.in.deepgram.com) is a "
        "data-residency host, not extra language support. Map this language "
        "to another provider in the per-language voice settings."
    )


# ── provider-neutral TTS capability dispatch ─────────────────────────────────
# Callers that validate an arbitrary engine (the preview API, the adapters'
# shared guards) ask these two instead of branching per provider, so adding a
# provider means adding a table above — not another ``if provider == …``
# somewhere else in the codebase.

_TTS_LANGUAGE_SUPPORT: dict[str, tuple] = {
    "elevenlabs": (elevenlabs_supports_language, _elevenlabs_unsupported_message),
    "deepgram": (deepgram_supports_language, deepgram_unsupported_language_message),
}


def tts_supports_language(
    provider: str | None, model: str | None, platform_code: str | None
) -> bool | None:
    """Can this provider/model speak this language?

    ``None`` means "not modelled here" — the caller must NOT read that as a
    rejection; the DB catalog (``provider_models.languages``) remains the
    general gate. Providers with no entry (Sarvam, mock) always answer None.
    """
    entry = _TTS_LANGUAGE_SUPPORT.get((provider or "").strip().lower())
    if entry is None:
        return None
    return entry[0](model, platform_code)


def tts_unsupported_language_message(
    provider: str | None, model: str | None, platform_code: str | None
) -> str:
    """Operator-facing reason a provider/model cannot speak a language."""
    entry = _TTS_LANGUAGE_SUPPORT.get((provider or "").strip().lower())
    if entry is None:
        return f"{provider}/{model} does not support language '{platform_code}'."
    return entry[1](model, platform_code)
