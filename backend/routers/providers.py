"""Voice provider catalog APIs: models, languages, voices, validation, tests, preview.

Everything served here comes from the database catalog (provider_defs,
provider_models, voice_profiles, supported_languages) — never from hardcoded
frontend arrays. List endpoints return only active rows.

Security:
- API keys are resolved server-side from env: references and never appear in
  any response, log line or error message.
- Connection tests and previews perform REAL provider calls when credentials
  exist and return sanitized failures otherwise — no fake success.
- Mutating/test/preview endpoints require the ``manage_voices`` permission and
  are audit-logged.
"""

from __future__ import annotations

import asyncio
import base64
import time

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    get_current_user,
    is_super_admin,
    require_permission,
    require_tenant_member,
)
from backend.core.provider_catalog import (
    CAPABILITIES,
    find_voice,
    get_model,
    get_provider,
    has_credentials,
    list_models,
    list_providers,
    list_voices,
    model_platform_languages,
    supports_voice_cloning,
    validate_voice_settings,
)
from backend.core.responses import ok
from shared.audio.pcm import pcm_to_wav_bytes
from shared.config import get_settings
from shared.db.mysql import get_db
from shared.errors import ApiError, NotFoundError
from shared.models import User, VoiceBot, VoiceProfile
from shared.providers.base import ProviderError
from shared.providers.tts.elevenlabs_ws import ElevenLabsWebSocketTTSProvider
from shared.providers.tts.sarvam_ws import SarvamWebSocketTTSProvider
from shared.providers.tts.streaming import TTSStreamSettings

router = APIRouter(tags=["Providers"])

_TEST_TIMEOUT_S = 8.0
_PREVIEW_TIMEOUT_S = 15.0
_PREVIEW_MAX_CHARS = 500


def _provider_secret(provider_row) -> str:
    reference = provider_row.secret_ref or f"env:{provider_row.code.upper()}_API_KEY"
    return get_settings().resolve_secret(reference)


def _serialize_provider(row, capability: str) -> dict:
    return {
        "code": row.code,
        "name": row.name,
        "capability": capability,
        "description": row.description,
        "requiresApiKey": row.requires_api_key,
        "hasCredentials": has_credentials(row),
        "supportsCloning": capability == "tts" and supports_voice_cloning(row),
    }


def _serialize_model(row) -> dict:
    return {
        "code": row.code,
        "displayName": row.display_name,
        "provider": row.provider_code,
        "capability": row.capability,
        "languages": row.languages or [],
        "codecs": row.codecs or [],
        "sampleRates": row.sample_rates or [],
        "streaming": row.streaming,
        "paramsSchema": row.params_schema or {},
        "isDefault": row.is_default,
    }


def _serialize_voice(row: VoiceProfile) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "gender": row.gender,
        "provider": row.provider,
        "providerVoiceId": row.provider_voice_id,
        "languages": row.languages or [],
        "modelCodes": row.model_codes or [],
        "locale": row.locale,
        "premium": row.premium,
        "isDefault": row.is_default,
        "status": row.status,
        "providerSettings": row.provider_settings or {},
        "sampleText": row.sample_text,
        "source": row.source or "platform",
    }


# ── catalog reads ────────────────────────────────────────────────────────────

@router.get("/providers/catalog")
def providers_catalog(
    capability: str | None = None,
    user: User = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    capabilities = [capability] if capability in CAPABILITIES else list(CAPABILITIES)
    result = {
        cap: [_serialize_provider(r, cap) for r in list_providers(db, cap)]
        for cap in capabilities
    }
    return ok(result)


@router.get("/providers/{capability}/{code}/models")
def provider_models(
    capability: str,
    code: str,
    user: User = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    if capability not in CAPABILITIES:
        raise ApiError("Unknown capability.", 422)
    if get_provider(db, capability, code) is None:
        raise NotFoundError("Provider")
    return ok([_serialize_model(m) for m in list_models(db, capability, code)])


@router.get("/providers/{capability}/{code}/models/{model}/languages")
def provider_model_languages(
    capability: str,
    code: str,
    model: str,
    user: User = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    if capability not in CAPABILITIES:
        raise ApiError("Unknown capability.", 422)
    row = get_model(db, capability, code, model)
    if row is None:
        raise NotFoundError("Provider model")
    languages = [
        {"code": lang.code, "name": lang.name, "nativeName": lang.native_name}
        for lang in model_platform_languages(db, row)
    ]
    supports_auto = bool(row.languages) and "unknown" in (row.languages or [])
    return ok({"languages": languages, "supportsAutoDetect": supports_auto,
               "languageAgnostic": not row.languages})


@router.get("/providers/tts/{code}/voices")
def provider_voices(
    code: str,
    model: str | None = None,
    language: str | None = None,
    gender: str | None = None,
    user: User = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    if get_provider(db, "tts", code) is None:
        raise NotFoundError("Provider")
    return ok([
        _serialize_voice(v)
        for v in list_voices(
            db, code, model=model, language=language, gender=gender,
            tenant_id=user.tenant_id,
            include_all_tenants=is_super_admin(user),
        )
    ])


# ── validation ───────────────────────────────────────────────────────────────

class ValidateConfigRequest(BaseModel):
    bot_id: str = Field(alias="botId")
    config: dict

    model_config = {"populate_by_name": True}


_CONFIG_KEYS = {
    "sttProvider": "stt_provider", "sttModel": "stt_model",
    "sttLanguage": "stt_language", "sttSettings": "stt_settings",
    "llmProvider": "llm_provider", "llmModel": "llm_model",
    "llmSettings": "llm_settings",
    "ttsProvider": "tts_provider", "ttsModel": "tts_model",
    "ttsVoice": "tts_voice", "ttsSettings": "tts_settings",
    "languageVoiceMap": "language_voice_map",
    "fallbackProvider": "fallback_provider", "fallbackModel": "fallback_model",
    "fallbackVoice": "fallback_voice", "audioSettings": "audio_settings",
}


def snake_config(config: dict) -> dict:
    return {snake: config[camel] for camel, snake in _CONFIG_KEYS.items() if camel in config}


@router.post("/providers/validate-config")
def validate_config(
    body: ValidateConfigRequest,
    user: User = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    from backend.core.deps import assert_tenant_access

    bot = db.get(VoiceBot, body.bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("Bot")
    assert_tenant_access(user, bot.tenant_id)
    errors, warnings = validate_voice_settings(db, bot, snake_config(body.config))
    return ok({"valid": not errors, "errors": errors, "warnings": warnings})


# ── connection tests ─────────────────────────────────────────────────────────

class ProviderTestRequest(BaseModel):
    capability: str
    provider: str
    model: str | None = None
    voice: str | None = None
    language: str | None = None


async def _ws_handshake_test(url: str, headers: dict) -> None:
    from websockets.asyncio.client import connect as websocket_connect

    ws = await asyncio.wait_for(
        websocket_connect(url, additional_headers=headers), timeout=_TEST_TIMEOUT_S
    )
    await ws.close()


async def _run_provider_test(db: Session, body: ProviderTestRequest) -> dict:
    provider_row = get_provider(db, body.capability, body.provider)
    if provider_row is None:
        raise NotFoundError("Provider")
    if body.model and get_model(db, body.capability, body.provider, body.model) is None:
        raise ApiError(f"Model '{body.model}' does not belong to '{body.provider}'.", 422)

    if body.provider == "mock":
        return {"ok": True, "message": "Mock provider — no external call."}

    key = _provider_secret(provider_row)
    if not key:
        return {
            "ok": False,
            "error": "credentials_missing",
            "message": f"No API key configured for {provider_row.name}. "
                       f"Set the referenced environment variable and restart.",
        }

    started = time.perf_counter()
    try:
        if body.provider == "sarvam" and body.capability == "stt":
            model = body.model or "saarika:v2.5"
            await _ws_handshake_test(
                f"wss://api.sarvam.ai/speech-to-text/ws?model={model}",
                {"api-subscription-key": key},
            )
        elif body.provider == "sarvam" and body.capability == "tts":
            model = body.model or "bulbul:v3"
            await _ws_handshake_test(
                f"wss://api.sarvam.ai/text-to-speech/ws?model={model}",
                {"api-subscription-key": key},
            )
        elif body.provider == "elevenlabs":
            async with httpx.AsyncClient(timeout=_TEST_TIMEOUT_S) as client:
                params = {"page_size": 1}
                if body.voice:
                    voice_row = find_voice(db, "elevenlabs", body.voice)
                    wire_id = voice_row.provider_voice_id if voice_row else body.voice
                    params = {"voice_ids": wire_id}
                response = await client.get(
                    "https://api.elevenlabs.io/v2/voices",
                    headers={"xi-api-key": key}, params=params,
                )
                if response.status_code in (401, 403):
                    return {"ok": False, "error": "auth",
                            "message": "ElevenLabs rejected the API key."}
                response.raise_for_status()
                if body.voice and not response.json().get("voices"):
                    return {"ok": False, "error": "voice_unavailable",
                            "message": "The selected voice is not available on this account."}
        elif body.provider == "openai":
            async with httpx.AsyncClient(timeout=_TEST_TIMEOUT_S) as client:
                url = "https://api.openai.com/v1/models"
                if body.model:
                    url += f"/{body.model}"
                response = await client.get(url, headers={"Authorization": f"Bearer {key}"})
                if response.status_code in (401, 403):
                    return {"ok": False, "error": "auth",
                            "message": "OpenAI rejected the API key."}
                if response.status_code == 404:
                    return {"ok": False, "error": "invalid_model",
                            "message": f"Model '{body.model}' is not available on this account."}
                response.raise_for_status()
        else:
            return {"ok": False, "error": "unsupported",
                    "message": f"No connection test implemented for '{body.provider}'."}
    except (TimeoutError, asyncio.TimeoutError):
        return {"ok": False, "error": "timeout",
                "message": f"{provider_row.name} did not respond within {_TEST_TIMEOUT_S:.0f}s."}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "error": "upstream",
                "message": f"{provider_row.name} returned HTTP {exc.response.status_code}."}
    except Exception as exc:  # noqa: BLE001 — sanitize everything else
        detail = type(exc).__name__
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            return {"ok": False, "error": "auth",
                    "message": f"{provider_row.name} rejected the API key."}
        return {"ok": False, "error": "connection",
                "message": f"Could not reach {provider_row.name} ({detail})."}

    return {"ok": True, "latencyMs": round((time.perf_counter() - started) * 1000, 1)}


@router.post("/providers/test")
async def provider_test(
    body: ProviderTestRequest,
    request: Request,
    user: User = Depends(require_permission("manage_voices", "bots.manage")),
    db: Session = Depends(get_db),
):
    if body.capability not in CAPABILITIES:
        raise ApiError("Unknown capability.", 422)
    result = await _run_provider_test(db, body)
    record_audit(
        db, user=user, action="Tested provider connection", entity_type="provider",
        entity_id=f"{body.capability}:{body.provider}",
        target_label=f"{body.provider} ({body.capability})",
        tenant_id=user.tenant_id,
        new_value={"ok": result.get("ok"), "model": body.model, "error": result.get("error")},
        request=request,
    )
    db.commit()
    return ok(result)


# ── voice preview ────────────────────────────────────────────────────────────

class PreviewRequest(BaseModel):
    provider: str
    model: str
    voice: str
    language: str
    text: str = Field(min_length=1, max_length=_PREVIEW_MAX_CHARS)
    params: dict = Field(default_factory=dict)


async def _collect_preview_audio(provider_client, text: str) -> tuple[bytes, float | None]:
    """Stream a preview generation and return (pcm, time-to-first-audio-ms)."""
    started = time.perf_counter()
    await provider_client.connect()
    await provider_client.synthesize_stream(text, generation_id="preview")
    await provider_client.flush("preview")
    await provider_client.finish("preview")

    chunks: list[bytes] = []
    ttfa_ms: float | None = None
    deadline = started + _PREVIEW_TIMEOUT_S
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise ProviderError(provider_client.name, "timeout", "Preview timed out")
        try:
            event = await asyncio.wait_for(provider_client.events.get(), timeout=remaining)
        except (TimeoutError, asyncio.TimeoutError):
            raise ProviderError(provider_client.name, "timeout", "Preview timed out") from None
        if event.kind == "audio":
            if ttfa_ms is None:
                ttfa_ms = (time.perf_counter() - started) * 1000
            chunks.append(event.audio)
        elif event.kind == "final":
            break
        elif event.kind == "error" and event.error is not None:
            raise event.error
    return b"".join(chunks), ttfa_ms


@router.post("/providers/tts-preview")
async def tts_preview(
    body: PreviewRequest,
    request: Request,
    user: User = Depends(require_permission("manage_voices", "bots.manage")),
    db: Session = Depends(get_db),
):
    provider_row = get_provider(db, "tts", body.provider)
    if provider_row is None:
        raise NotFoundError("Provider")
    model_row = get_model(db, "tts", body.provider, body.model)
    if model_row is None:
        raise ApiError(f"Model '{body.model}' does not belong to '{body.provider}'.", 422)

    voice_row = find_voice(
        db, body.provider, body.voice,
        tenant_id=user.tenant_id,
        include_all_tenants=is_super_admin(user),
    )
    if voice_row is None:
        # Raw wire codes pass through, but a value that maps to some OTHER
        # tenant's cloned voice must not — 404, without leaking existence.
        hidden = find_voice(db, body.provider, body.voice, include_all_tenants=True)
        if hidden is not None and hidden.tenant_id:
            raise NotFoundError("Voice")
    wire_voice = voice_row.provider_voice_id or voice_row.name if voice_row else body.voice
    started = time.perf_counter()

    if body.provider == "mock":
        from shared.providers.base import ProviderConfig
        from shared.providers.tts.mock import MockTTS

        result = await MockTTS(ProviderConfig(provider="mock")).synthesize(
            body.text, voice=wire_voice, language=body.language
        )
        pcm, sample_rate, ttfa_ms = result.audio, result.sample_rate, 1.0
    else:
        key = _provider_secret(provider_row)
        if not key:
            raise ApiError(
                f"No API key configured for {provider_row.name} — set the referenced "
                "environment variable to enable previews.", 422,
            )
        sample_rate = 24000 if 24000 in (model_row.sample_rates or [24000]) else 16000
        stream_settings = TTSStreamSettings(
            provider=body.provider,
            model=body.model,
            voice=wire_voice,
            language=body.language,
            sample_rate=sample_rate,
            codec="linear16" if body.provider == "sarvam" else "pcm",
            params=body.params,
            api_key=key,
            timeout_seconds=_TEST_TIMEOUT_S,
        )
        client_cls = (
            SarvamWebSocketTTSProvider if body.provider == "sarvam"
            else ElevenLabsWebSocketTTSProvider if body.provider == "elevenlabs"
            else None
        )
        if client_cls is None:
            raise ApiError(f"Preview is not supported for provider '{body.provider}'.", 422)
        client = client_cls(stream_settings)
        try:
            pcm, ttfa_ms = await _collect_preview_audio(client, body.text)
        except ProviderError as exc:
            raise ApiError(f"{provider_row.name} preview failed: {exc.category}.", 502) from exc
        finally:
            await client.close()

    total_ms = (time.perf_counter() - started) * 1000
    record_audit(
        db, user=user, action="Generated voice preview", entity_type="provider",
        entity_id=f"tts:{body.provider}", target_label=f"{body.provider}/{body.voice}",
        tenant_id=user.tenant_id,
        new_value={"model": body.model, "voice": body.voice, "language": body.language,
                   "textChars": len(body.text)},
        request=request,
    )
    if body.provider != "mock" and user.tenant_id:
        # Previews synthesize real audio — billable characters for the tenant.
        # Platform-admin previews (no tenant) are internal and not tenant-billed.
        from shared.billing.metering import record_usage_event

        record_usage_event(
            db,
            tenant_id=user.tenant_id,
            capability="tts",
            provider_code=body.provider,
            model_code=body.model,
            voice_code=body.voice,
            characters=len(body.text),
            usage_metadata={"kind": "tts_preview"},
            commit=False,
        )
    db.commit()
    if not pcm:
        raise ApiError("The provider returned no audio.", 502)
    wav = pcm_to_wav_bytes(pcm, sample_rate=sample_rate)
    return ok({
        "audioBase64": base64.b64encode(wav).decode("ascii"),
        "mimeType": "audio/wav",
        "sampleRate": sample_rate,
        "ttfaMs": round(ttfa_ms or 0.0, 1),
        "totalMs": round(total_ms, 1),
        "provider": body.provider,
        "voice": body.voice,
    })


# ── ElevenLabs account voice sync ────────────────────────────────────────────

@router.post("/providers/elevenlabs/sync-voices")
async def sync_elevenlabs_voices(
    request: Request,
    user: User = Depends(require_permission("manage_voices", "manage_master_data")),
    db: Session = Depends(get_db),
):
    """Verify catalog voices against the connected ElevenLabs account.

    Marks configured voices unavailable when the account no longer has them
    (never deletes, never overwrites display names) and reports account voices
    that are not yet in the catalog.
    """
    provider_row = get_provider(db, "tts", "elevenlabs")
    if provider_row is None:
        raise NotFoundError("Provider")
    key = _provider_secret(provider_row)
    if not key:
        raise ApiError("No ElevenLabs API key configured.", 422)

    account_voices: dict[str, str] = {}
    next_page: str | None = None
    async with httpx.AsyncClient(timeout=_TEST_TIMEOUT_S) as client:
        while True:
            params = {"page_size": 100}
            if next_page:
                params["next_page_token"] = next_page
            response = await client.get(
                "https://api.elevenlabs.io/v2/voices",
                headers={"xi-api-key": key}, params=params,
            )
            if response.status_code in (401, 403):
                raise ApiError("ElevenLabs rejected the API key.", 502)
            response.raise_for_status()
            payload = response.json()
            for voice in payload.get("voices", []):
                if voice.get("voice_id"):
                    account_voices[voice["voice_id"]] = voice.get("name") or ""
            next_page = payload.get("next_page_token")
            if not payload.get("has_more") or not next_page:
                break

    rows = db.scalars(
        select(VoiceProfile).where(
            VoiceProfile.provider == "elevenlabs",
            VoiceProfile.is_deleted.is_(False),
        )
    ).all()
    marked_unavailable, restored = [], []
    for row in rows:
        if row.provider_voice_id and row.provider_voice_id not in account_voices:
            if row.status != "unavailable":
                row.status = "unavailable"
                marked_unavailable.append(row.name)
        elif row.status == "unavailable":
            row.status = "active"
            restored.append(row.name)
    known_ids = {row.provider_voice_id for row in rows}
    unlisted = [
        {"voiceId": voice_id, "name": name}
        for voice_id, name in account_voices.items() if voice_id not in known_ids
    ]
    record_audit(
        db, user=user, action="Synced ElevenLabs voices", entity_type="provider",
        entity_id="tts:elevenlabs", target_label="ElevenLabs voice sync",
        tenant_id=user.tenant_id,
        new_value={"unavailable": marked_unavailable, "restored": restored,
                   "accountOnly": len(unlisted)},
        request=request,
    )
    db.commit()
    return ok({
        "accountVoices": len(account_voices),
        "markedUnavailable": marked_unavailable,
        "restored": restored,
        "notInCatalog": unlisted,
    })
