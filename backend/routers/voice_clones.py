"""Tenant voice cloning (ElevenLabs Instant Voice Cloning).

Cloned voices are tenant-owned rows in voice_profiles (tenant_id set,
source="cloned"); the shared platform catalog keeps tenant_id NULL. Every
provider call happens here, server-side — API keys are resolved from secret
references and never reach the frontend. Source audio (uploaded files or
in-browser recordings) is persisted under VOICE_CLONE_AUDIO_DIR with one
voice_clone_audio row per sample, so tenants can replay exactly what the
clone was built from via GET /voice-clones/{id}/audio/{audio_id}. Clones
created before source retention simply have no rows — the API reports an
empty sourceAudio list, never an error.

Once created, a cloned voice is an ordinary catalog voice: bot selection,
validation, preview (/providers/tts-preview) and the TTS runtime resolve it
through the same paths as platform voices, so character-based TTS metering
and pricing snapshots apply unchanged. ElevenLabs does not charge per clone
creation — IVC is plan-gated with a custom-voice-slot quota — so creation
records an audit entry, not a usage event.
"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from backend.core import clone_audio
from backend.core.audit import record_audit
from backend.core.deps import (
    is_super_admin,
    require_permission,
    require_tenant_member,
    resolve_tenant_id,
)
from backend.core.provider_catalog import (
    get_provider,
    has_credentials,
    list_models,
    list_providers,
    supports_voice_cloning,
)
from backend.core.responses import ok
from backend.core.softdelete import soft_delete
from backend.serializers import serialize_clone_audio, serialize_voice
from shared.config import get_settings
from shared.db.mysql import get_db
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from shared.models import User, VoiceBot, VoiceBotSetting, VoiceCloneAudio, VoiceProfile

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Voice Clones"])

_CLONE_TIMEOUT_S = 60.0
_DELETE_TIMEOUT_S = 15.0
_MAX_FILES = 10
_MAX_FILE_MB = 10
_MAX_TOTAL_MB = 30
_MAX_NAME_LEN = 100
_MAX_DESCRIPTION_LEN = 500

# Sample duration constraints (seconds). The minimum/maximum are enforced
# server-side whenever the stored bytes can actually be probed (ffprobe, or
# stdlib wave for .wav); client-declared durations are advisory metadata only.
# The recommended window drives the in-browser recorder guidance.
_MIN_SAMPLE_SEC = 5
_MIN_SAMPLE_TOLERANCE_SEC = 0.5  # container rounding vs wall-clock time
_MAX_SAMPLE_SEC = 1800
_REC_RECOMMENDED_MIN_SEC = 30
_REC_RECOMMENDED_MAX_SEC = 40
_REC_MAX_SEC = 300

_ELEVENLABS_BASE = "https://api.elevenlabs.io"

# Sample formats accepted for cloning uploads (validated by extension AND
# magic bytes where the container has a stable signature).
_AUDIO_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "webm": "audio/webm",
    "aac": "audio/aac",
}

# Provider-specific clone options the frontend renders dynamically — the
# generic UI never hardcodes ElevenLabs fields.
_CLONE_FIELDS: dict[str, list[dict]] = {
    "elevenlabs": [
        {
            "name": "description",
            "type": "string",
            "label": "Description",
            "help": "Shown in the voice list; also stored on the ElevenLabs voice.",
            "maxLength": _MAX_DESCRIPTION_LEN,
            "optional": True,
        },
        {
            "name": "removeBackgroundNoise",
            "type": "boolean",
            "label": "Remove background noise",
            "help": "Runs ElevenLabs audio isolation on the samples before cloning.",
            "default": False,
            "optional": True,
        },
    ],
}

_NO_CLONING_REASON = {
    "sarvam": (
        "Sarvam offers voice cloning only inside Sarvam Studio (in-browser "
        "recording, beta) — its public API has no voice-cloning endpoint."
    ),
}


def _provider_secret(provider_row) -> str:
    reference = provider_row.secret_ref or f"env:{provider_row.code.upper()}_API_KEY"
    return get_settings().resolve_secret(reference)


# ── sample validation ────────────────────────────────────────────────────────

def _file_extension(name: str | None) -> str:
    if not name or "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def _looks_like_audio(ext: str, data: bytes) -> bool:
    """Best-effort magic-byte check for containers with stable signatures."""
    head = data[:16]
    if ext == "wav":
        return head.startswith(b"RIFF") and data[8:12] == b"WAVE"
    if ext == "flac":
        return head.startswith(b"fLaC")
    if ext in ("ogg", "opus"):
        return head.startswith(b"OggS")
    if ext == "webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    if ext == "m4a":
        return data[4:8] == b"ftyp"
    if ext == "mp3":
        return head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    return True  # aac (raw ADTS) has no single reliable signature


async def _read_samples(files: list[UploadFile]) -> list[tuple[str, bytes, str]]:
    """Validate uploads and return (filename, content, mime) tuples."""

    def _reject(message: str) -> ApiError:
        return ApiError(message, 422, errors=[{"field": "files", "message": message}])

    if not files:
        raise _reject("Upload at least one audio sample.")
    if len(files) > _MAX_FILES:
        raise _reject(f"At most {_MAX_FILES} audio samples are allowed.")
    samples: list[tuple[str, bytes, str]] = []
    total = 0
    for upload in files:
        name = upload.filename or "sample"
        ext = _file_extension(name)
        if ext not in _AUDIO_TYPES:
            raise _reject(
                f"'{name}': unsupported file type — allowed: "
                + ", ".join(sorted(set(_AUDIO_TYPES))) + "."
            )
        data = await upload.read()
        if not data:
            raise _reject(f"'{name}' is empty.")
        if len(data) > _MAX_FILE_MB * 1024 * 1024:
            raise _reject(f"'{name}' exceeds the {_MAX_FILE_MB} MB per-file limit.")
        if not _looks_like_audio(ext, data):
            raise _reject(f"'{name}' does not look like a valid .{ext} audio file.")
        total += len(data)
        if total > _MAX_TOTAL_MB * 1024 * 1024:
            raise _reject(f"Combined samples exceed the {_MAX_TOTAL_MB} MB limit.")
        samples.append((name, data, _AUDIO_TYPES[ext]))
    return samples


def _parse_samples_meta(raw: str | None, count: int) -> list[dict]:
    """Client-declared per-file provenance (JSON array aligned with the files
    order): sourceType ("live_recording" | "file_upload") and durationSec.
    Advisory only — lenient parsing, defaults for anything malformed."""
    entries: list = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                entries = parsed
        except ValueError:
            entries = []
    out: list[dict] = []
    for index in range(count):
        item = entries[index] if index < len(entries) else None
        if not isinstance(item, dict):
            item = {}
        source = item.get("sourceType")
        if source not in ("live_recording", "file_upload"):
            source = "file_upload"
        try:
            duration = float(item.get("durationSec"))
        except (TypeError, ValueError):
            duration = None
        if duration is not None and not 0 < duration <= 36000:
            duration = None
        out.append({"sourceType": source, "durationSec": duration})
    return out


# ── ElevenLabs REST calls (management plane; synthesis stays in the runtime) ─

def _elevenlabs_error(response: httpx.Response) -> ApiError:
    """Map a provider failure to a sanitized API error (no keys, no internals)."""
    detail: dict = {}
    try:
        raw = response.json().get("detail")
        if isinstance(raw, dict):
            detail = raw
        elif isinstance(raw, str):
            detail = {"message": raw}
    except ValueError:
        pass
    status = str(detail.get("status") or "")
    message = str(detail.get("message") or "")[:300]
    if response.status_code in (401, 403):
        if status == "missing_permissions":
            # Valid but scoped key (e.g. TTS-only) — actionable, not a mystery.
            return ApiError(
                "The configured ElevenLabs API key does not have voice-cloning "
                "permissions — enable 'create_instant_voice_clone' (and voice "
                "read/write) on the key in the ElevenLabs dashboard.", 422,
            )
        return ApiError("ElevenLabs rejected the configured API key.", 502)
    if status == "voice_limit_reached":
        return ApiError(
            "The connected ElevenLabs account has reached its custom-voice "
            "limit. Delete an unused clone or upgrade the ElevenLabs plan.", 422,
        )
    if status == "can_not_use_instant_voice_cloning":
        return ApiError(
            "The connected ElevenLabs plan does not include Instant Voice "
            "Cloning (Starter or higher is required).", 422,
        )
    if response.status_code in (400, 422) and message:
        return ApiError(f"ElevenLabs rejected the request: {message}", 422)
    return ApiError("ElevenLabs voice cloning failed — please try again.", 502)


async def _elevenlabs_create_voice(
    api_key: str,
    *,
    name: str,
    samples: list[tuple[str, bytes, str]],
    description: str | None,
    remove_background_noise: bool,
) -> dict:
    """POST /v1/voices/add — returns {"voice_id", "requires_verification"}."""
    data = {"name": name, "remove_background_noise": "true" if remove_background_noise else "false"}
    if description:
        data["description"] = description
    files = [("files", (fname, content, mime)) for fname, content, mime in samples]
    try:
        async with httpx.AsyncClient(timeout=_CLONE_TIMEOUT_S) as client:
            response = await client.post(
                f"{_ELEVENLABS_BASE}/v1/voices/add",
                headers={"xi-api-key": api_key}, data=data, files=files,
            )
    except httpx.HTTPError:
        raise ApiError("Could not reach ElevenLabs — please try again.", 502) from None
    if response.status_code >= 400:
        raise _elevenlabs_error(response)
    payload = response.json()
    if not payload.get("voice_id"):
        raise ApiError("ElevenLabs returned no voice id.", 502)
    return payload


async def _elevenlabs_delete_voice(api_key: str, voice_id: str) -> bool:
    """DELETE /v1/voices/{id}. True when deleted or already gone."""
    try:
        async with httpx.AsyncClient(timeout=_DELETE_TIMEOUT_S) as client:
            response = await client.delete(
                f"{_ELEVENLABS_BASE}/v1/voices/{voice_id}",
                headers={"xi-api-key": api_key},
            )
    except httpx.HTTPError:
        return False
    return response.status_code < 400 or response.status_code == 404


# ── mock provider (dev/test pseudo-provider, never listed in production) ─────

async def _mock_create_voice(
    api_key: str,
    *,
    name: str,
    samples: list[tuple[str, bytes, str]],
    description: str | None,
    remove_background_noise: bool,
) -> dict:
    """Simulated clone for the dev pseudo-provider — no external calls."""
    return {"voice_id": new_id("mockclone"), "requires_verification": False}


async def _mock_delete_voice(api_key: str, voice_id: str) -> bool:
    return True


# ── provider dispatch ────────────────────────────────────────────────────────
# Whether a provider supports cloning is DB-config driven
# (provider_catalog.supports_voice_cloning); this registry maps each
# cloning-capable provider to its management-plane implementation. Attribute
# names, not references — resolved late so tests can monkeypatch the module
# functions. A provider whose config enables cloning without an entry here is
# rejected explicitly instead of being routed to another vendor's API.

_CLONE_BACKENDS: dict[str, tuple[str, str]] = {
    "elevenlabs": ("_elevenlabs_create_voice", "_elevenlabs_delete_voice"),
    "mock": ("_mock_create_voice", "_mock_delete_voice"),
}


def _clone_backend(provider_code: str):
    names = _CLONE_BACKENDS.get(provider_code)
    if names is None:
        return None
    return globals()[names[0]], globals()[names[1]]


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_clone_checked(db: Session, voice_id: str, user: User) -> VoiceProfile:
    row = db.get(VoiceProfile, voice_id)
    if row is None or row.is_deleted or row.source != "cloned" or not row.tenant_id:
        raise NotFoundError("Voice")
    if not is_super_admin(user) and row.tenant_id != user.tenant_id:
        # 404, not 403 — never confirm another tenant's clone exists.
        raise NotFoundError("Voice")
    return row


def _clone_usage(db: Session, row: VoiceProfile) -> int:
    """How many bots reference this voice (FK, engine columns or voice map)."""
    refs = [row.id]
    if row.provider_voice_id:
        refs.append(row.provider_voice_id)
    bots = db.scalar(
        select(func.count()).select_from(VoiceBot).where(
            VoiceBot.voice_id == row.id, VoiceBot.is_deleted.is_(False)
        )
    ) or 0
    settings = db.scalar(
        select(func.count()).select_from(VoiceBotSetting).where(or_(
            VoiceBotSetting.voice_id == row.id,
            VoiceBotSetting.tts_voice.in_(refs),
            VoiceBotSetting.fallback_voice.in_(refs),
            cast(VoiceBotSetting.language_voice_map, String).like(f"%{row.id}%"),
        ))
    ) or 0
    return bots + settings


def _clone_audio_rows(db: Session, voice_id: str) -> list[VoiceCloneAudio]:
    return list(db.scalars(
        select(VoiceCloneAudio).where(
            VoiceCloneAudio.voice_id == voice_id,
            VoiceCloneAudio.is_deleted.is_(False),
        ).order_by(VoiceCloneAudio.created_at, VoiceCloneAudio.id)
    ))


def _serialize_clone(db: Session, row: VoiceProfile) -> dict:
    data = serialize_voice(row, usage=_clone_usage(db, row))
    # Empty for clones created before source retention — the UI shows a
    # "source audio unavailable" note for those instead of failing.
    data["sourceAudio"] = [
        serialize_clone_audio(a) for a in _clone_audio_rows(db, row.id)
    ]
    return data


def _invalidate_configs() -> None:
    from shared.bot_config import invalidate_all_bot_configs_sync

    invalidate_all_bot_configs_sync()


# ── config / capability ──────────────────────────────────────────────────────

@router.get("/voice-clones/config")
def voice_clone_config(
    user: User = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """Cloning capability per TTS provider + upload constraints (single source
    of truth for the frontend — mirrors /knowledge/upload-config)."""
    providers = []
    for row in list_providers(db, "tts"):
        cloneable = supports_voice_cloning(row)
        providers.append({
            "code": row.code,
            "name": row.name,
            "supportsCloning": cloneable,
            "hasCredentials": has_credentials(row),
            "cloneParams": _CLONE_FIELDS.get(row.code, []) if cloneable else [],
            "reason": None if cloneable else _NO_CLONING_REASON.get(
                row.code, "This provider does not offer a voice-cloning API."
            ),
        })
    return ok({
        "providers": providers,
        "allowedExtensions": sorted(set(_AUDIO_TYPES)),
        "accept": ",".join("." + e for e in sorted(set(_AUDIO_TYPES))),
        "maxFiles": _MAX_FILES,
        "maxFileMb": _MAX_FILE_MB,
        "maxTotalMb": _MAX_TOTAL_MB,
        "recording": {
            "minSeconds": _MIN_SAMPLE_SEC,
            "recommendedMinSeconds": _REC_RECOMMENDED_MIN_SEC,
            "recommendedMaxSeconds": _REC_RECOMMENDED_MAX_SEC,
            "maxSeconds": _REC_MAX_SEC,
        },
    })


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.get("/voice-clones")
def list_voice_clones(
    request_tenant_id: str | None = Query(None, alias="tenantId"),
    include_inactive: bool = Query(True, alias="includeInactive"),
    user: User = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(user, request_tenant_id)
    stmt = select(VoiceProfile).where(
        VoiceProfile.tenant_id == tenant_id,
        VoiceProfile.source == "cloned",
        VoiceProfile.is_deleted.is_(False),
    )
    if not include_inactive:
        stmt = stmt.where(VoiceProfile.status == "active")
    rows = db.scalars(stmt.order_by(VoiceProfile.created_at.desc())).all()
    return ok([_serialize_clone(db, row) for row in rows])


@router.get("/voice-clones/{voice_id}")
def get_voice_clone(
    voice_id: str,
    user: User = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    row = _get_clone_checked(db, voice_id, user)
    return ok(_serialize_clone(db, row))


@router.get("/voice-clones/{voice_id}/audio/{audio_id}")
def get_voice_clone_audio(
    voice_id: str,
    audio_id: str,
    download: bool = Query(False),
    user: User = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """Stream a stored source-audio sample. Same visibility rules as the clone
    itself (owning tenant, or super admin) — 404 for anything else, never a
    403 that would confirm existence. Storage paths are resolved server-side
    only; clients never pass paths."""
    row = _get_clone_checked(db, voice_id, user)
    audio = db.get(VoiceCloneAudio, audio_id)
    if (
        audio is None
        or audio.is_deleted
        or audio.voice_id != row.id
        or audio.tenant_id != row.tenant_id
    ):
        raise NotFoundError("Audio sample")
    full = clone_audio.resolve_sample_path(audio.storage_path)
    if full is None:
        raise NotFoundError("Audio file")
    kwargs: dict = {"media_type": audio.mime_type or "application/octet-stream"}
    if download:
        # Server-generated name — the original filename never reaches headers.
        kwargs["filename"] = f"voice-source-{audio.id}{full.suffix}"
    return FileResponse(full, **kwargs)


@router.post("/voice-clones", status_code=201)
async def create_voice_clone(
    request: Request,
    provider: str = Form(...),
    name: str = Form(...),
    description: str | None = Form(None),
    gender: str | None = Form(None),
    remove_background_noise: bool = Form(False, alias="removeBackgroundNoise"),
    request_tenant_id: str | None = Form(None, alias="tenantId"),
    samples_meta: str | None = Form(None, alias="samplesMeta"),
    files: list[UploadFile] = File(...),
    user: User = Depends(require_permission("manage_voices")),
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(user, request_tenant_id)

    name = (name or "").strip()
    if not name or len(name) > _MAX_NAME_LEN:
        raise ApiError(
            f"Voice name is required (at most {_MAX_NAME_LEN} characters).", 422,
            errors=[{"field": "name", "message": "Voice name is required."}],
        )
    description = (description or "").strip() or None
    if description and len(description) > _MAX_DESCRIPTION_LEN:
        raise ApiError(
            f"Description must be at most {_MAX_DESCRIPTION_LEN} characters.", 422,
            errors=[{"field": "description", "message": "Description is too long."}],
        )
    if gender and gender not in ("male", "female", "neutral"):
        raise ApiError("Gender must be male, female or neutral.", 422,
                       errors=[{"field": "gender", "message": "Invalid gender."}])

    provider_row = get_provider(db, "tts", provider)
    if provider_row is None:
        raise ApiError(f"TTS provider '{provider}' is not available.", 422,
                       errors=[{"field": "provider", "message": "Unknown provider."}])
    if not supports_voice_cloning(provider_row):
        raise ApiError(
            _NO_CLONING_REASON.get(
                provider_row.code,
                f"{provider_row.name} does not offer a voice-cloning API.",
            ), 422,
            errors=[{"field": "provider",
                     "message": f"{provider_row.name} does not support voice cloning."}],
        )
    backend = _clone_backend(provider_row.code)
    if backend is None:
        raise ApiError(
            f"Voice cloning is enabled for {provider_row.name} in its provider "
            "configuration, but no cloning integration is implemented for it.", 422,
            errors=[{"field": "provider",
                     "message": "No cloning integration for this provider."}],
        )
    create_voice, delete_voice = backend
    api_key = _provider_secret(provider_row)
    if provider_row.requires_api_key and not api_key:
        raise ApiError(
            f"No API key configured for {provider_row.name} — set the referenced "
            "environment variable to enable voice cloning.", 422,
        )

    duplicate = db.scalar(
        select(VoiceProfile).where(
            VoiceProfile.tenant_id == tenant_id,
            VoiceProfile.provider == provider_row.code,
            VoiceProfile.source == "cloned",
            VoiceProfile.is_deleted.is_(False),
            func.lower(VoiceProfile.name) == name.lower(),
        )
    )
    if duplicate is not None:
        raise ApiError(
            f"A cloned voice named '{name}' already exists.", 422,
            errors=[{"field": "name", "message": "A cloned voice with this name already exists."}],
        )

    samples = await _read_samples(files)
    meta = _parse_samples_meta(samples_meta, len(samples))

    # Persist the source audio BEFORE the provider call: durations are
    # validated from the stored bytes, and a provider rejection just removes
    # the files again. The rows are committed together with the voice row.
    voice_id = new_id("vp")
    stored: list[dict] = []

    def _discard_stored() -> None:
        for item in stored:
            clone_audio.delete_sample(item["storagePath"])

    try:
        for (fname, content, mime), sample_meta in zip(samples, meta):
            sample_id = new_id("vca")
            rel_path = clone_audio.save_sample(
                tenant_id, voice_id, sample_id, _file_extension(fname), content
            )
            stored.append({
                "id": sample_id,
                "storagePath": rel_path,
                "fileName": fname,
                "mime": mime,
                "sizeBytes": len(content),
                "sourceType": sample_meta["sourceType"],
                "durationSec": sample_meta["durationSec"],
            })
    except Exception:
        _discard_stored()
        logger.exception("voice clone source-audio storage failed (tenant %s)", tenant_id)
        raise ApiError(
            "The audio samples could not be stored — please try again.", 500
        ) from None

    for item in stored:
        full = clone_audio.resolve_sample_path(item["storagePath"])
        probed = clone_audio.probe_duration_sec(full) if full else None
        if probed is None and full is not None:
            # MediaRecorder webm/ogg blobs carry no duration header — remux in
            # place (stream copy) so the stored file probes and seeks properly.
            probed = clone_audio.normalize_duration_metadata(full)
            if probed is not None:
                item["sizeBytes"] = full.stat().st_size
        if probed is None:
            continue  # container carries no duration → keep the client value
        item["durationSec"] = probed
        if probed < _MIN_SAMPLE_SEC - _MIN_SAMPLE_TOLERANCE_SEC:
            _discard_stored()
            raise ApiError(
                f"'{item['fileName']}' is only {probed:.1f}s of audio — samples "
                f"must be at least {_MIN_SAMPLE_SEC} seconds long "
                f"({_REC_RECOMMENDED_MIN_SEC}–{_REC_RECOMMENDED_MAX_SEC}s recommended).",
                422,
                errors=[{"field": "files",
                         "message": f"'{item['fileName']}' is shorter than {_MIN_SAMPLE_SEC} seconds."}],
            )
        if probed > _MAX_SAMPLE_SEC:
            _discard_stored()
            raise ApiError(
                f"'{item['fileName']}' is over {_MAX_SAMPLE_SEC // 60} minutes long — "
                "trim it to the clearest section before cloning.",
                422,
                errors=[{"field": "files",
                         "message": f"'{item['fileName']}' is too long."}],
            )

    try:
        created = await create_voice(
            api_key, name=name, samples=samples,
            description=description, remove_background_noise=remove_background_noise,
        )
    except Exception:
        # Provider rejected the clone — nothing to keep.
        _discard_stored()
        raise
    provider_voice_id = created["voice_id"]

    row = VoiceProfile(
        id=voice_id,
        tenant_id=tenant_id,
        source="cloned",
        name=name,
        gender=gender or "neutral",
        languages=[],  # ElevenLabs clones are multilingual → language-agnostic
        description=description,
        provider=provider_row.code,
        provider_voice_id=provider_voice_id,
        model_codes=[m.code for m in list_models(db, "tts", provider_row.code)],
        status="active",
        clone_metadata={
            "kind": "instant",
            "requiresVerification": bool(created.get("requires_verification")),
            "removeBackgroundNoise": remove_background_noise,
            "samples": [
                {
                    "fileName": item["fileName"],
                    "sizeBytes": item["sizeBytes"],
                    "sourceType": item["sourceType"],
                    "durationSec": item["durationSec"],
                    "audioId": item["id"],
                }
                for item in stored
            ],
        },
        created_by=user.id,
        updated_by=user.id,
    )
    audio_rows = [
        VoiceCloneAudio(
            id=item["id"],
            tenant_id=tenant_id,
            voice_id=voice_id,
            original_filename=item["fileName"][:255],
            storage_path=item["storagePath"],
            mime_type=item["mime"],
            size_bytes=item["sizeBytes"],
            duration_sec=item["durationSec"],
            source_type=item["sourceType"],
            provider=provider_row.code,
            provider_voice_id=provider_voice_id,
            status="stored",
            created_by=user.id,
            updated_by=user.id,
        )
        for item in stored
    ]
    try:
        db.add(row)
        # No relationship() on these models — flush the voice row first so the
        # audio rows' voice_id FK has its parent when they are inserted.
        db.flush()
        db.add_all(audio_rows)
        record_audit(
            db, user=user, action="voice.clone.create", entity_type="voice_profile",
            entity_id=row.id, target_label=name, tenant_id=tenant_id,
            new_value={"provider": provider_row.code, "providerVoiceId": provider_voice_id,
                       "samples": len(samples), "sourceAudioStored": len(stored)},
            request=request,
        )
        db.commit()
    except Exception:
        # Provider voice exists but persistence failed — remove the provider
        # voice so the account is not left with an orphan slot, and drop the
        # stored files that no row will ever reference.
        db.rollback()
        _discard_stored()
        removed = await delete_voice(api_key, provider_voice_id)
        if not removed:
            logger.error(
                "orphaned %s voice %s (tenant %s): local persistence failed "
                "and provider cleanup also failed",
                provider_row.code, provider_voice_id, tenant_id,
            )
        raise ApiError("The cloned voice could not be saved — please try again.", 500) from None

    return ok(_serialize_clone(db, row))


class CloneUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=_MAX_NAME_LEN)
    description: str | None = Field(None, max_length=_MAX_DESCRIPTION_LEN)
    gender: str | None = None
    locale: str | None = None
    sample_text: str | None = Field(None, alias="sampleText", max_length=500)

    model_config = {"populate_by_name": True}


@router.patch("/voice-clones/{voice_id}")
def update_voice_clone(
    voice_id: str,
    body: CloneUpdateRequest,
    request: Request,
    user: User = Depends(require_permission("manage_voices")),
    db: Session = Depends(get_db),
):
    """Local metadata only — the provider voice itself is never renamed here."""
    row = _get_clone_checked(db, voice_id, user)
    before = serialize_voice(row)
    if body.gender is not None and body.gender not in ("male", "female", "neutral"):
        raise ApiError("Gender must be male, female or neutral.", 422,
                       errors=[{"field": "gender", "message": "Invalid gender."}])
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise ApiError("Voice name is required.", 422,
                           errors=[{"field": "name", "message": "Voice name is required."}])
        duplicate = db.scalar(
            select(VoiceProfile).where(
                VoiceProfile.tenant_id == row.tenant_id,
                VoiceProfile.provider == row.provider,
                VoiceProfile.source == "cloned",
                VoiceProfile.is_deleted.is_(False),
                VoiceProfile.id != row.id,
                func.lower(VoiceProfile.name) == name.lower(),
            )
        )
        if duplicate is not None:
            raise ApiError(
                f"A cloned voice named '{name}' already exists.", 422,
                errors=[{"field": "name",
                         "message": "A cloned voice with this name already exists."}],
            )
        row.name = name
    if body.description is not None:
        row.description = body.description.strip() or None
    if body.gender is not None:
        row.gender = body.gender
    if body.locale is not None:
        row.locale = body.locale.strip() or None
    if body.sample_text is not None:
        row.sample_text = body.sample_text.strip() or None
    row.updated_by = user.id
    record_audit(
        db, user=user, action="voice.clone.update", entity_type="voice_profile",
        entity_id=row.id, target_label=row.name, tenant_id=row.tenant_id,
        previous_value=before, new_value=serialize_voice(row), request=request,
    )
    db.commit()
    _invalidate_configs()
    return ok(_serialize_clone(db, row))


class CloneStatusRequest(BaseModel):
    status: str


@router.post("/voice-clones/{voice_id}/status")
def set_voice_clone_status(
    voice_id: str,
    body: CloneStatusRequest,
    request: Request,
    user: User = Depends(require_permission("manage_voices")),
    db: Session = Depends(get_db),
):
    row = _get_clone_checked(db, voice_id, user)
    if body.status not in ("active", "inactive", "archived"):
        raise ApiError("Status must be active, inactive or archived.", 422)
    previous = row.status
    row.status = body.status
    row.updated_by = user.id
    record_audit(
        db, user=user, action="voice.clone.status", entity_type="voice_profile",
        entity_id=row.id, target_label=row.name, tenant_id=row.tenant_id,
        previous_value={"status": previous}, new_value={"status": row.status},
        request=request,
    )
    db.commit()
    # A deactivated voice must stop resolving in cached runtime configs.
    _invalidate_configs()
    return ok(_serialize_clone(db, row))


@router.delete("/voice-clones/{voice_id}")
async def delete_voice_clone(
    voice_id: str,
    request: Request,
    user: User = Depends(require_permission("manage_voices")),
    db: Session = Depends(get_db),
):
    """Delete the provider voice FIRST, then soft-delete the local record —
    the local row is the only pointer we have to the provider clone."""
    row = _get_clone_checked(db, voice_id, user)
    usage = _clone_usage(db, row)
    if usage:
        raise ApiError(
            f"'{row.name}' is used by {usage} bot configuration(s). "
            "Deactivate or archive it instead, or unassign it first.", 409,
        )
    provider_row = get_provider(db, "tts", row.provider or "")
    provider_deleted = False
    if row.provider_voice_id:
        backend = _clone_backend(provider_row.code) if provider_row is not None else None
        if provider_row is None or backend is None:
            raise ApiError(
                "Cannot delete the provider voice — no cloning integration is "
                f"available for {row.provider}. Archive the voice instead.", 422,
            )
        api_key = _provider_secret(provider_row)
        if provider_row.requires_api_key and not api_key:
            raise ApiError(
                "Cannot delete the provider voice — no API key is configured "
                f"for {row.provider}. Archive the voice instead.", 422,
            )
        provider_deleted = await backend[1](api_key, row.provider_voice_id)
        if not provider_deleted:
            raise ApiError(
                f"{provider_row.name} did not confirm the voice deletion — the "
                "local record was kept. Please try again.", 502,
            )
    audio_rows = _clone_audio_rows(db, row.id)
    soft_delete(row, user)
    for audio in audio_rows:
        soft_delete(audio, user)
        audio.status = "deleted"
    record_audit(
        db, user=user, action="voice.clone.delete", entity_type="voice_profile",
        entity_id=row.id, target_label=row.name, tenant_id=row.tenant_id,
        previous_value={"providerVoiceId": row.provider_voice_id,
                        "providerDeleted": provider_deleted,
                        "sourceAudioRemoved": len(audio_rows)},
        request=request,
    )
    db.commit()
    # Files go only after the rows are durably marked deleted (best-effort —
    # a leftover file without a live row is unreachable via the API anyway).
    for audio in audio_rows:
        clone_audio.delete_sample(audio.storage_path)
    _invalidate_configs()
    return ok({"deleted": True, "providerDeleted": provider_deleted})
