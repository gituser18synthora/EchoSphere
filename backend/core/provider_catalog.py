"""Database-driven provider catalog lookups and configuration validation.

The single source of truth for "which provider/model/language/voice/parameter
combinations are valid" is the DB catalog (provider_defs, provider_models,
voice_profiles, supported_languages). Both the voice-settings save path and
the /providers/validate-config endpoint run through `validate_voice_settings`
so the frontend's provider-specific hiding is never the only gate.

Credentials are handled as references only — this module never returns key
material, it only answers "is a credential configured?".
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from shared.config import get_settings
from shared.models import (
    BotLanguage,
    ProviderDef,
    ProviderModel,
    SupportedLanguage,
    VoiceBotSetting,
    VoiceProfile,
)
from shared.providers.languages import matches_model_language
from shared.turn_detection import validate_noise_gate, validate_turn_detection

CAPABILITIES = ("stt", "tts", "llm", "embedding")

# Providers with a public voice-cloning API. Sarvam offers cloning only inside
# its Studio product (browser recording, no API) as of 2026-07, so it is not
# listed. A provider_defs.config {"voice_cloning": bool} entry overrides this.
_VOICE_CLONING_PROVIDERS = frozenset({"elevenlabs"})


def supports_voice_cloning(provider: ProviderDef) -> bool:
    config = provider.config or {}
    if "voice_cloning" in config:
        return bool(config["voice_cloning"])
    return provider.code in _VOICE_CLONING_PROVIDERS


def _tenant_visibility_filter(tenant_id: str | None):
    """Rows a tenant may see: platform voices plus its own cloned voices."""
    if tenant_id:
        return or_(VoiceProfile.tenant_id.is_(None), VoiceProfile.tenant_id == tenant_id)
    return VoiceProfile.tenant_id.is_(None)


# ── lookups ──────────────────────────────────────────────────────────────────

def get_provider(db: Session, capability: str, code: str) -> ProviderDef | None:
    # The mock pseudo-provider is dev/test only — reject direct submissions in
    # production too, not just hide it from listings.
    if code == "mock" and get_settings().app_env == "production":
        return None
    return db.scalar(
        select(ProviderDef).where(
            ProviderDef.kind == capability,
            ProviderDef.code == code,
            ProviderDef.status == "active",
            ProviderDef.is_deleted.is_(False),
        )
    )


def list_providers(db: Session, capability: str) -> list[ProviderDef]:
    rows = db.scalars(
        select(ProviderDef).where(
            ProviderDef.kind == capability,
            ProviderDef.status == "active",
            ProviderDef.is_deleted.is_(False),
        ).order_by(ProviderDef.sort_order)
    ).all()
    if get_settings().app_env == "production":
        rows = [r for r in rows if r.code != "mock"]
    return rows


def get_model(db: Session, capability: str, provider: str, code: str) -> ProviderModel | None:
    if provider == "mock" and get_settings().app_env == "production":
        return None
    return db.scalar(
        select(ProviderModel).where(
            ProviderModel.capability == capability,
            ProviderModel.provider_code == provider,
            ProviderModel.code == code,
            ProviderModel.status == "active",
            ProviderModel.is_deleted.is_(False),
        )
    )


def list_models(db: Session, capability: str, provider: str) -> list[ProviderModel]:
    return db.scalars(
        select(ProviderModel).where(
            ProviderModel.capability == capability,
            ProviderModel.provider_code == provider,
            ProviderModel.status == "active",
            ProviderModel.is_deleted.is_(False),
        ).order_by(ProviderModel.sort_order)
    ).all()


def model_platform_languages(db: Session, model: ProviderModel) -> list[SupportedLanguage]:
    """Enabled platform languages the given provider model supports."""
    enabled = db.scalars(
        select(SupportedLanguage)
        .where(SupportedLanguage.enabled.is_(True))
        .order_by(SupportedLanguage.sort_order)
    ).all()
    return [
        lang for lang in enabled
        if matches_model_language(model.provider_code, lang.code, model.languages)
    ]


def list_voices(
    db: Session,
    provider: str,
    *,
    model: str | None = None,
    language: str | None = None,
    gender: str | None = None,
    tenant_id: str | None = None,
    include_all_tenants: bool = False,
) -> list[VoiceProfile]:
    stmt = select(VoiceProfile).where(
        VoiceProfile.provider == provider,
        VoiceProfile.status == "active",
        VoiceProfile.is_deleted.is_(False),
    )
    if not include_all_tenants:
        stmt = stmt.where(_tenant_visibility_filter(tenant_id))
    rows = db.scalars(stmt.order_by(VoiceProfile.sort_order, VoiceProfile.name)).all()
    result = []
    for voice in rows:
        if model and voice.model_codes and model not in voice.model_codes:
            continue
        if language and voice.languages and language not in voice.languages:
            continue
        if gender and voice.gender != gender:
            continue
        result.append(voice)
    return result


def find_voice(
    db: Session,
    provider: str,
    voice: str,
    *,
    tenant_id: str | None = None,
    include_all_tenants: bool = False,
) -> VoiceProfile | None:
    """Look an ACTIVE voice up by catalog id or provider wire code.

    Input is trimmed so padded ids validate; wire-code matching is
    case-insensitive via the column collation. Inactive/deleted voices are
    not returned — a disabled speaker must fail validation, not resolve.
    Tenant scoping: unless include_all_tenants, only platform voices and the
    given tenant's own voices resolve — another tenant's clone never does.
    """
    if not voice or not str(voice).strip():
        return None
    voice = str(voice).strip()

    def _visible(row: VoiceProfile) -> bool:
        return include_all_tenants or row.tenant_id in (None, tenant_id or None)

    row = db.get(VoiceProfile, voice)
    if (row is not None and row.provider == provider
            and row.status == "active" and not row.is_deleted and _visible(row)):
        return row
    stmt = select(VoiceProfile).where(
        VoiceProfile.provider == provider,
        VoiceProfile.provider_voice_id == voice,
        VoiceProfile.status == "active",
        VoiceProfile.is_deleted.is_(False),
    )
    if not include_all_tenants:
        stmt = stmt.where(_tenant_visibility_filter(tenant_id))
    return db.scalar(stmt)


def has_credentials(provider: ProviderDef) -> bool:
    if not provider.requires_api_key:
        return True
    reference = provider.secret_ref or f"env:{provider.code.upper()}_API_KEY"
    return bool(get_settings().resolve_secret(reference))


def get_platform_language(db: Session, code: str | None) -> SupportedLanguage | None:
    """Language catalog record for a platform locale code (any enabled state)."""
    if not code or not str(code).strip():
        return None
    return db.scalar(
        select(SupportedLanguage).where(SupportedLanguage.code == str(code).strip())
    )


# ── parameter-schema validation ──────────────────────────────────────────────

def validate_params(schema: dict | None, params: dict | None, *, prefix: str) -> list[str]:
    """Validate provider-specific parameters against the model's schema."""
    errors: list[str] = []
    params = params or {}
    schema = schema or {}
    for key, value in params.items():
        spec = schema.get(key)
        if spec is None:
            errors.append(f"{prefix}: unknown parameter '{key}'.")
            continue
        if value is None:
            continue
        if spec.get("fixed") and value != spec.get("default"):
            errors.append(f"{prefix}: parameter '{key}' is fixed and cannot be changed.")
            continue
        kind = spec.get("type")
        if kind == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"{prefix}: '{key}' must be a number.")
            elif not (spec.get("min", float("-inf")) <= value <= spec.get("max", float("inf"))):
                errors.append(
                    f"{prefix}: '{key}' must be between {spec.get('min')} and {spec.get('max')}."
                )
        elif kind == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"{prefix}: '{key}' must be an integer.")
            elif not (spec.get("min", float("-inf")) <= value <= spec.get("max", float("inf"))):
                errors.append(
                    f"{prefix}: '{key}' must be between {spec.get('min')} and {spec.get('max')}."
                )
        elif kind == "boolean":
            if not isinstance(value, bool):
                errors.append(f"{prefix}: '{key}' must be true or false.")
        elif kind == "enum":
            if value not in (spec.get("values") or []):
                errors.append(
                    f"{prefix}: '{key}' must be one of {', '.join(map(str, spec.get('values') or []))}."
                )
        elif kind == "string":
            if not isinstance(value, str):
                errors.append(f"{prefix}: '{key}' must be a string.")
            elif len(value) > int(spec.get("max_length", 200)):
                errors.append(f"{prefix}: '{key}' is too long.")
        elif kind == "int_list":
            if (not isinstance(value, list)
                    or any(not isinstance(v, int) or isinstance(v, bool) for v in value)):
                errors.append(f"{prefix}: '{key}' must be a list of integers.")
            else:
                if len(value) > int(spec.get("max_items", 16)):
                    errors.append(f"{prefix}: '{key}' has too many entries.")
                lo, hi = spec.get("min", float("-inf")), spec.get("max", float("inf"))
                if any(not (lo <= v <= hi) for v in value):
                    errors.append(
                        f"{prefix}: every '{key}' entry must be between {spec.get('min')} and {spec.get('max')}."
                    )
        elif kind == "string_list":
            if (not isinstance(value, list)
                    or any(not isinstance(v, str) for v in value)):
                errors.append(f"{prefix}: '{key}' must be a list of strings.")
            else:
                if len(value) > int(spec.get("max_items", 16)):
                    errors.append(f"{prefix}: '{key}' has too many entries.")
                if any(len(v) > int(spec.get("max_length", 50)) for v in value):
                    errors.append(f"{prefix}: a '{key}' entry is too long.")
                allowed = spec.get("values")
                if allowed and any(v not in allowed for v in value):
                    errors.append(
                        f"{prefix}: every '{key}' entry must be one of "
                        f"{', '.join(map(str, allowed))}."
                    )
    return errors


# ── platform-owned LLM settings ──────────────────────────────────────────────
# These keys configure the EchoSphere orchestration layer (Goal Engine /
# intent pipeline / memory greeting) rather than the LLM provider, so they
# intentionally do not belong to a provider model's ``params_schema`` — the
# same design as the STT ``turn_detection``/``noise_gate`` sections below.
# Safe ranges mirror the runtime clamps in shared.orchestration.goal_engine.

_PLATFORM_LLM_BOOLEAN_KEYS = (
    "goal_engine_enabled",
    "intent_llm_enabled",
    "memory_greeting_enabled",
)
_PLATFORM_LLM_STRING_KEYS = {
    "orchestration_provider": 40,
    "orchestration_model": 80,
}
_PLATFORM_LLM_NUMBER_BOUNDS = {
    "orchestration_timeout_seconds": (0.5, 5.0),
    "intent_timeout_seconds": (0.5, 5.0),
    "memory_greeting_timeout_seconds": (1.0, 10.0),
}
_PLATFORM_LLM_INTEGER_BOUNDS = {
    "orchestration_max_tokens": (64, 340),
    # Provider-independent spoken-output cap. Runtime also derives a native
    # provider token budget from it and enforces the character ceiling.
    "max_output_characters": (120, 2000),
}
PLATFORM_LLM_SETTING_KEYS = frozenset(
    (*_PLATFORM_LLM_BOOLEAN_KEYS, *_PLATFORM_LLM_STRING_KEYS,
     *_PLATFORM_LLM_NUMBER_BOUNDS, *_PLATFORM_LLM_INTEGER_BOUNDS)
)


def validate_llm_settings(
    schema: dict | None, params: dict | None, *, prefix: str = "LLM"
) -> list[str]:
    """Validate provider parameters plus platform-owned orchestration settings.

    The orchestration keys control the EchoSphere decision layer, not the LLM
    provider, so they are validated against the platform contract here and
    removed before the remaining settings go through the strict
    provider-schema validation.
    """
    params = params or {}
    errors: list[str] = []
    for key in _PLATFORM_LLM_BOOLEAN_KEYS:
        if key in params and not isinstance(params[key], bool):
            errors.append(f"{prefix}: '{key}' must be true or false.")
    for key, max_length in _PLATFORM_LLM_STRING_KEYS.items():
        value = params.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(f"{prefix}: '{key}' must be a string.")
        elif len(value) > max_length:
            errors.append(f"{prefix}: '{key}' is too long.")
    for key, (low, high) in _PLATFORM_LLM_NUMBER_BOUNDS.items():
        value = params.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{prefix}: '{key}' must be a number.")
        elif not low <= float(value) <= high:
            errors.append(f"{prefix}: '{key}' must be between {low:g} and {high:g}.")
    for key, (low, high) in _PLATFORM_LLM_INTEGER_BOUNDS.items():
        value = params.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{prefix}: '{key}' must be an integer.")
        elif not low <= value <= high:
            errors.append(f"{prefix}: '{key}' must be between {low} and {high}.")
    provider_params = {
        key: value
        for key, value in params.items()
        if key not in PLATFORM_LLM_SETTING_KEYS
    }
    errors.extend(validate_params(schema, provider_params, prefix=prefix))
    return errors


def validate_stt_settings(
    schema: dict | None, params: dict | None, *, prefix: str = "STT"
) -> list[str]:
    """Validate provider parameters plus platform-owned turn timing.

    ``turn_detection`` and ``noise_gate`` control the EchoSphere pipeline rather
    than the STT provider, so they intentionally do not belong to a provider
    model's ``params_schema``.  Validate them against the shared runtime
    contract and keep the remaining settings under the normal strict provider
    validation.
    """
    params = params or {}
    errors = validate_turn_detection(
        params.get("turn_detection"), prefix=f"{prefix} turn detection"
    )
    errors.extend(
        validate_noise_gate(params.get("noise_gate"), prefix=f"{prefix} noise gate")
    )
    provider_params = {
        key: value
        for key, value in params.items()
        if key not in ("turn_detection", "noise_gate")
    }
    errors.extend(validate_params(schema, provider_params, prefix=prefix))
    return errors


# ── full voice-settings validation ───────────────────────────────────────────

def _validate_engine(
    db: Session,
    *,
    provider: str | None,
    model: str | None,
    voice: str | None,
    language: str | None,
    params: dict | None,
    prefix: str,
    errors: list[str],
    warnings: list[str],
    require_voice: bool = False,
    tenant_id: str | None = None,
    require_streaming: bool = False,
) -> ProviderModel | None:
    """Validate one TTS engine selection (provider+model+voice+language+params)."""
    if not provider:
        return None
    provider_row = get_provider(db, "tts", provider)
    if provider_row is None:
        errors.append(f"{prefix}: TTS provider '{provider}' is not available.")
        return None
    if not has_credentials(provider_row):
        warnings.append(
            f"{prefix}: credentials for '{provider}' are not configured — "
            "voice sessions will fail until the API key is set."
        )
    model_row = None
    if model:
        model_row = get_model(db, "tts", provider, model)
        if model_row is None:
            errors.append(f"{prefix}: model '{model}' does not belong to provider '{provider}'.")
        elif require_streaming and not model_row.streaming:
            # Fallback and per-language engines exist only inside the realtime
            # streaming router — a model without realtime streaming support
            # (e.g. ElevenLabs eleven_v3) can never serve them.
            errors.append(
                f"{prefix}: model '{model}' does not support realtime streaming — "
                "choose a streaming model (e.g. eleven_flash_v2_5)."
            )
    elif require_voice:
        errors.append(f"{prefix}: a model is required.")
    if voice:
        voice_row = find_voice(db, provider, voice, tenant_id=tenant_id)
        if voice_row is None:
            errors.append(f"{prefix}: voice '{voice}' does not belong to provider '{provider}'.")
        elif model and voice_row.model_codes and model not in voice_row.model_codes:
            errors.append(f"{prefix}: voice '{voice_row.name}' does not support model '{model}'.")
        elif (language and voice_row.languages
              and language not in voice_row.languages):
            errors.append(
                f"{prefix}: voice '{voice_row.name}' does not support language '{language}'."
            )
    elif require_voice:
        errors.append(f"{prefix}: a voice is required.")
    if language:
        # The languages table is the source of truth: the locale must be a
        # catalog record. A record an admin disabled AFTER it was configured
        # stays usable (bot languages are enabled-checked when added), but is
        # flagged so the operator sees the legacy state.
        lang_row = get_platform_language(db, language)
        if lang_row is None:
            errors.append(
                f"{prefix}: language '{language}' is not in the platform language catalog."
            )
        elif not lang_row.enabled:
            warnings.append(
                f"{prefix}: language '{language}' is disabled on the platform — "
                "the existing configuration keeps working, but re-enable the "
                "language or remove this selection."
            )
    if model_row is not None:
        if language and not matches_model_language(provider, language, model_row.languages):
            errors.append(
                f"{prefix}: language '{language}' is not supported by {provider}/{model}."
            )
        errors.extend(validate_params(model_row.params_schema, params, prefix=prefix))
    return model_row


def validate_voice_settings(
    db: Session, bot, payload: dict
) -> tuple[list[str], list[str]]:
    """Validate a complete voice-settings payload against the DB catalog.

    Returns (errors, warnings). ``payload`` uses snake_case keys matching
    VoiceBotSetting columns. Frontend field-hiding is advisory only — this is
    the enforcement point.
    """
    errors: list[str] = []
    warnings: list[str] = []
    # Voice lookups are scoped to the bot's tenant: platform voices plus the
    # tenant's own clones. Another tenant's cloned voice must fail validation.
    tenant_id = getattr(bot, "tenant_id", None)
    # Persisted settings distinguish a NEW language selection (rejected when
    # the language is missing/disabled) from an unchanged legacy one
    # (preserved with a warning).
    persisted = db.scalar(
        select(VoiceBotSetting).where(VoiceBotSetting.bot_id == bot.id)
    )

    bot_languages = set(db.scalars(
        select(BotLanguage.language_code).where(BotLanguage.bot_id == bot.id)
    ).all())
    lang_map = payload.get("language_voice_map") or {}
    default_locale = lang_map.get("default")

    # ── STT ──
    stt_provider = payload.get("stt_provider")
    if stt_provider:
        provider_row = get_provider(db, "stt", stt_provider)
        if provider_row is None:
            errors.append(f"STT provider '{stt_provider}' is not available.")
        else:
            if not has_credentials(provider_row):
                warnings.append(
                    f"STT: credentials for '{stt_provider}' are not configured — "
                    "voice sessions will fail until the API key is set."
                )
            model = payload.get("stt_model")
            model_row = None
            if model:
                model_row = get_model(db, "stt", stt_provider, model)
                if model_row is None:
                    errors.append(
                        f"STT model '{model}' does not belong to provider '{stt_provider}'."
                    )
            stt_language = payload.get("stt_language")
            if stt_language and stt_language != "unknown":
                # Catalog check: the language must exist and be active. An
                # unchanged persisted value is grandfathered (warning) so a
                # later-deactivated language never bricks an existing config;
                # a NEW selection of a missing/disabled language is rejected.
                lang_row = get_platform_language(db, stt_language)
                unchanged = (
                    persisted is not None
                    and (persisted.stt_language or "") == stt_language
                )
                if lang_row is None:
                    message = (
                        f"STT language '{stt_language}' is not in the platform "
                        "language catalog."
                    )
                    if unchanged:
                        warnings.append(message + " Existing configuration preserved.")
                    else:
                        errors.append(message)
                elif not lang_row.enabled:
                    message = f"STT language '{stt_language}' is disabled on the platform."
                    if unchanged:
                        warnings.append(
                            message + " The existing configuration keeps working; "
                            "re-enable the language or pick an active one."
                        )
                    else:
                        errors.append(message + " Choose an active language.")
            if stt_language and model_row is not None:
                if (stt_language != "unknown"
                        and not matches_model_language(
                            stt_provider, stt_language, model_row.languages)):
                    errors.append(
                        f"STT language '{stt_language}' is not supported by "
                        f"{stt_provider}/{model}."
                    )
            if model_row is not None:
                errors.extend(validate_stt_settings(
                    model_row.params_schema, payload.get("stt_settings"), prefix="STT"
                ))

    # ── LLM ──
    llm_provider = payload.get("llm_provider")
    if llm_provider:
        provider_row = get_provider(db, "llm", llm_provider)
        if provider_row is None:
            errors.append(f"LLM provider '{llm_provider}' is not available.")
        else:
            if not has_credentials(provider_row):
                warnings.append(
                    f"LLM: credentials for '{llm_provider}' are not configured — "
                    "voice sessions will fail until the API key is set."
                )
            model = payload.get("llm_model")
            model_row = None
            if model:
                model_row = get_model(db, "llm", llm_provider, model)
                if model_row is None:
                    errors.append(
                        f"LLM model '{model}' does not belong to provider '{llm_provider}'."
                    )
            if model_row is not None:
                errors.extend(validate_llm_settings(
                    model_row.params_schema, payload.get("llm_settings"), prefix="LLM"
                ))

    # ── Orchestration engine (platform-owned llm_settings keys) ──
    # The Goal Engine's dedicated decision model must exist in the governed
    # catalog like any other engine. The runtime additionally degrades to the
    # conversation LLM if governance changes later — but a NEW selection of an
    # unknown/inactive engine is rejected at save time.
    llm_settings_blob = payload.get("llm_settings") or {}
    orch_provider = llm_settings_blob.get("orchestration_provider")
    if isinstance(orch_provider, str) and orch_provider.strip():
        orch_provider = orch_provider.strip()
        orch_row = get_provider(db, "llm", orch_provider)
        if orch_row is None:
            errors.append(
                f"LLM: orchestration provider '{orch_provider}' is not available."
            )
        else:
            orch_model = llm_settings_blob.get("orchestration_model")
            if isinstance(orch_model, str) and orch_model.strip():
                if get_model(db, "llm", orch_provider, orch_model.strip()) is None:
                    errors.append(
                        f"LLM: orchestration model '{orch_model.strip()}' does not "
                        f"belong to provider '{orch_provider}'."
                    )

    # ── TTS (default engine) ──
    tts_provider = payload.get("tts_provider")
    tts_model_row = None
    if tts_provider:
        tts_model_row = _validate_engine(
            db,
            provider=tts_provider,
            model=payload.get("tts_model"),
            voice=payload.get("tts_voice"),
            language=default_locale if default_locale in bot_languages else None,
            params=payload.get("tts_settings"),
            prefix="TTS",
            errors=errors,
            warnings=warnings,
            tenant_id=tenant_id,
        )

    # ── Fallback engine ──
    fallback_provider = payload.get("fallback_provider")
    if fallback_provider:
        if fallback_provider == (tts_provider or ""):
            errors.append("Fallback TTS provider must differ from the primary provider.")
        _validate_engine(
            db,
            provider=fallback_provider,
            model=payload.get("fallback_model"),
            voice=payload.get("fallback_voice"),
            language=None,
            params=None,
            prefix="Fallback TTS",
            errors=errors,
            warnings=warnings,
            tenant_id=tenant_id,
            require_streaming=True,
        )

    # A non-streaming default engine (ElevenLabs eleven_v3) runs the segmented
    # REST pipeline, which has no fallback and no per-language switching —
    # surface that those configured features will not apply on live calls.
    if tts_model_row is not None and not tts_model_row.streaming:
        lang_overrides = [k for k in (payload.get("language_voice_map") or {}) if k != "default"]
        unavailable = []
        if fallback_provider:
            unavailable.append("the fallback engine")
        if lang_overrides:
            unavailable.append("per-language voice overrides")
        if unavailable:
            warnings.append(
                f"TTS: model '{tts_model_row.code}' does not stream in realtime — "
                f"live calls synthesize each reply over REST and "
                f"{' and '.join(unavailable)} will not apply."
            )

    # ── Per-language voice map ──
    if lang_map:
        if default_locale and bot_languages and default_locale not in bot_languages:
            errors.append(
                f"Default language '{default_locale}' is not one of the bot's languages."
            )
        for locale, entry in lang_map.items():
            if locale == "default":
                continue
            if bot_languages and locale not in bot_languages:
                errors.append(f"Language mapping '{locale}' is not one of the bot's languages.")
            if isinstance(entry, dict):
                _validate_engine(
                    db,
                    provider=entry.get("provider") or tts_provider,
                    model=entry.get("model") or payload.get("tts_model"),
                    voice=entry.get("voice"),
                    language=locale,
                    params=entry.get("params"),
                    prefix=f"Voice mapping [{locale}]",
                    errors=errors,
                    warnings=warnings,
                    tenant_id=tenant_id,
                    require_streaming=bool(entry.get("model")),
                )
            elif isinstance(entry, str):
                voice_row = db.get(VoiceProfile, entry)
                if (voice_row is None or voice_row.is_deleted
                        or voice_row.tenant_id not in (None, tenant_id)):
                    errors.append(f"Voice mapping [{locale}]: unknown voice profile '{entry}'.")
                elif voice_row.languages and locale not in voice_row.languages:
                    errors.append(
                        f"Voice mapping [{locale}]: voice '{voice_row.name}' does not "
                        f"support this language."
                    )
            else:
                errors.append(f"Voice mapping [{locale}]: invalid entry.")

    # ── Audio / transport settings ──
    audio = payload.get("audio_settings") or {}
    for transport, spec in audio.items():
        if transport not in ("browser", "telephony"):
            errors.append(f"Audio settings: unknown transport '{transport}'.")
            continue
        if not isinstance(spec, dict):
            errors.append(f"Audio settings [{transport}]: invalid entry.")
            continue
        codec = spec.get("codec")
        rate = spec.get("sampleRate")
        allowed_codecs = (
            {"linear16", "pcm"} if transport == "browser" else {"mulaw", "ulaw", "alaw", "linear16"}
        )
        if codec and codec not in allowed_codecs:
            errors.append(f"Audio settings [{transport}]: codec '{codec}' is not supported.")
        allowed_rates = {8000, 16000, 22050, 24000} if transport == "browser" else {8000}
        if rate and rate not in allowed_rates:
            errors.append(f"Audio settings [{transport}]: sample rate {rate} is not supported.")

    return errors, warnings
