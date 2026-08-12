"""Tenant pronunciation dictionaries (Sarvam bulbul:v3 ``dict_id``).

Sarvam stores the actual word → "speak as" mappings and issues the
``dictionary_id`` that TTS calls (preview and live) pass as ``dict_id``.
Its account API has no names and its list endpoint returns bare ids, so
EchoSphere keeps a tenant-scoped metadata row per dictionary (name,
description, per-language word counts) and proxies every provider call
server-side — API keys are resolved from secret references and never reach
the frontend.

Scoping note: the Sarvam API key is a platform credential, so the account's
dictionary quota (10 dictionaries, 100 words each) is shared across tenants.
Rows are tenant-scoped locally; a tenant only ever sees and selects its own.
"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import require_permission, resolve_tenant_id
from backend.core.provider_catalog import get_provider
from backend.core.responses import ok
from backend.core.softdelete import soft_delete
from shared.config import get_settings
from shared.db.mysql import get_db
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from shared.models import PronunciationDictionary, User
from shared.providers.languages import (
    SARVAM_SUPPORTED_LOCALES,
    to_platform_language,
    to_provider_language,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Pronunciation"])

_SARVAM_DICTIONARY_URL = "https://api.sarvam.ai/text-to-speech/pronunciation-dictionary"
_PROVIDER_TIMEOUT_S = 20.0
# Documented Sarvam account limits (2026-08). The account quota is shared
# across tenants (platform API key), so the local cap exists only to fail
# with a readable message before the provider does.
_MAX_WORDS = 100
_MAX_DICTIONARIES = 10
_MAX_WORD_LEN = 200


class DictionaryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    # {"hi-IN": {"EMI": "ई एम आई"}, "en-IN": {"HDFC": "H D F C"}}
    pronunciations: dict[str, dict[str, str]]


class DictionaryUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    pronunciations: dict[str, dict[str, str]] | None = None


def _serialize(row: PronunciationDictionary) -> dict:
    return {
        "id": row.id,
        "provider": row.provider,
        "dictId": row.provider_dict_id,
        "name": row.name,
        "description": row.description,
        "languageWordCounts": row.language_word_counts or {},
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def _validate_pronunciations(pronunciations: dict) -> dict[str, dict[str, str]]:
    """Validate structure/limits and map platform locales to Sarvam wire codes
    (or-IN → od-IN). Returns the wire-ready mapping."""
    if not pronunciations or not isinstance(pronunciations, dict):
        raise ApiError("At least one pronunciation entry is required.", 422)
    wire: dict[str, dict[str, str]] = {}
    total = 0
    for locale, entries in pronunciations.items():
        if locale not in SARVAM_SUPPORTED_LOCALES:
            raise ApiError(
                f"Language '{locale}' is not supported by Sarvam pronunciation "
                f"dictionaries. Supported: {', '.join(sorted(SARVAM_SUPPORTED_LOCALES))}.",
                422,
            )
        if not isinstance(entries, dict):
            raise ApiError(f"Pronunciations for '{locale}' must be a word → text mapping.", 422)
        clean: dict[str, str] = {}
        for word, spoken in entries.items():
            word = str(word).strip()
            spoken = str(spoken).strip()
            if not word or not spoken:
                raise ApiError(
                    f"Empty word or pronunciation in '{locale}' — every row needs both.", 422,
                )
            if len(word) > _MAX_WORD_LEN or len(spoken) > _MAX_WORD_LEN:
                raise ApiError(
                    f"'{word[:40]}' in '{locale}' is too long (max {_MAX_WORD_LEN} characters).",
                    422,
                )
            clean[word] = spoken
        if not clean:
            continue
        total += len(clean)
        wire[to_provider_language("sarvam", locale) or locale] = clean
    if not wire:
        raise ApiError("At least one pronunciation entry is required.", 422)
    if total > _MAX_WORDS:
        raise ApiError(
            f"A pronunciation dictionary holds at most {_MAX_WORDS} words "
            f"({total} submitted).", 422,
        )
    return wire


def _word_counts(pronunciations: dict) -> dict[str, int]:
    """Display summary keyed by the PLATFORM locale codes the UI knows."""
    counts: dict[str, int] = {}
    for locale, entries in pronunciations.items():
        if isinstance(entries, dict) and entries:
            counts[locale] = len(entries)
    return counts


def _sarvam_key(db: Session) -> str:
    provider_row = get_provider(db, "tts", "sarvam")
    if provider_row is None:
        raise ApiError("The Sarvam provider is not available on this platform.", 422)
    reference = provider_row.secret_ref or "env:SARVAM_API_KEY"
    key = get_settings().resolve_secret(reference)
    if not key:
        raise ApiError(
            "No API key configured for Sarvam — set the referenced environment "
            "variable to manage pronunciation dictionaries.", 422,
        )
    return key


async def _provider_call(
    method: str, url: str, key: str, *, files: dict | None = None
) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=_PROVIDER_TIMEOUT_S) as client:
            response = await client.request(
                method, url, headers={"api-subscription-key": key}, files=files
            )
    except httpx.HTTPError as exc:
        logger.warning("sarvam dictionary API unreachable: %s", exc)
        raise ApiError("Sarvam did not respond — try again shortly.", 502) from exc
    if response.status_code >= 400:
        # Keep the raw body server-side for diagnostics; the client gets a
        # readable summary without header/credential material.
        logger.warning(
            "sarvam dictionary API %s %s -> %s: %s",
            method, url.split("?")[0], response.status_code, response.text[:500],
        )
        detail = ""
        try:
            payload = response.json()
            detail = str(
                payload.get("error", {}).get("message")
                or payload.get("detail") or payload.get("message") or ""
            )[:200]
        except ValueError:
            pass
        suffix = f": {detail}" if detail else "."
        raise ApiError(
            f"Sarvam rejected the dictionary request ({response.status_code}){suffix}", 502,
        )
    return response


def _dictionary_file(wire_pronunciations: dict) -> dict:
    body = json.dumps({"pronunciations": wire_pronunciations}, ensure_ascii=False)
    # Sarvam requires the multipart file part to be application/json —
    # anything else is rejected as an invalid content type.
    return {"file": ("dictionary.json", body.encode("utf-8"), "application/json")}


def _get_row(db: Session, tenant_id: str, dictionary_id: str) -> PronunciationDictionary:
    row = db.scalar(select(PronunciationDictionary).where(
        PronunciationDictionary.id == dictionary_id,
        PronunciationDictionary.tenant_id == tenant_id,
        PronunciationDictionary.is_deleted.is_(False),
    ))
    if row is None:
        raise NotFoundError("Pronunciation dictionary")
    return row


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/pronunciation-dictionaries")
def list_dictionaries(
    tenant_id: str | None = None,
    user: User = Depends(require_permission("manage_voices", "bots.manage")),
    db: Session = Depends(get_db),
):
    effective_tenant = resolve_tenant_id(user, tenant_id)
    rows = db.scalars(
        select(PronunciationDictionary).where(
            PronunciationDictionary.tenant_id == effective_tenant,
            PronunciationDictionary.is_deleted.is_(False),
        ).order_by(PronunciationDictionary.name)
    ).all()
    return ok([_serialize(row) for row in rows])


@router.get("/pronunciation-dictionaries/{dictionary_id}")
async def get_dictionary(
    dictionary_id: str,
    tenant_id: str | None = None,
    user: User = Depends(require_permission("manage_voices", "bots.manage")),
    db: Session = Depends(get_db),
):
    """Metadata plus the LIVE provider mappings (Sarvam is the source of truth)."""
    effective_tenant = resolve_tenant_id(user, tenant_id)
    row = _get_row(db, effective_tenant, dictionary_id)
    key = _sarvam_key(db)
    response = await _provider_call(
        "GET", f"{_SARVAM_DICTIONARY_URL}/{row.provider_dict_id}", key
    )
    live = response.json().get("pronunciations") or {}
    # Wire → platform locale for the UI (od-IN → or-IN).
    pronunciations = {
        (to_platform_language("sarvam", locale) or locale): entries
        for locale, entries in live.items()
        if isinstance(entries, dict) and entries
    }
    return ok({**_serialize(row), "pronunciations": pronunciations})


@router.post("/pronunciation-dictionaries")
async def create_dictionary(
    body: DictionaryRequest,
    request: Request,
    tenant_id: str | None = None,
    user: User = Depends(require_permission("manage_voices", "bots.manage")),
    db: Session = Depends(get_db),
):
    effective_tenant = resolve_tenant_id(user, tenant_id)
    existing = db.scalars(select(PronunciationDictionary).where(
        PronunciationDictionary.tenant_id == effective_tenant,
        PronunciationDictionary.is_deleted.is_(False),
    )).all()
    if len(existing) >= _MAX_DICTIONARIES:
        raise ApiError(
            f"Sarvam allows at most {_MAX_DICTIONARIES} pronunciation dictionaries.", 422,
        )
    if any(row.name.lower() == body.name.strip().lower() for row in existing):
        raise ApiError(f"A dictionary named '{body.name.strip()}' already exists.", 422)

    wire = _validate_pronunciations(body.pronunciations)
    key = _sarvam_key(db)
    response = await _provider_call(
        "POST", _SARVAM_DICTIONARY_URL, key, files=_dictionary_file(wire)
    )
    provider_dict_id = str(response.json().get("dictionary_id") or "").strip()
    if not provider_dict_id:
        raise ApiError("Sarvam did not return a dictionary id.", 502)

    row = PronunciationDictionary(
        id=new_id("pd"),
        tenant_id=effective_tenant,
        provider="sarvam",
        provider_dict_id=provider_dict_id,
        name=body.name.strip(),
        description=(body.description or "").strip() or None,
        language_word_counts=_word_counts(body.pronunciations),
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(row)
    record_audit(
        db, user=user, action="Created pronunciation dictionary",
        entity_type="pronunciation_dictionary", entity_id=row.id,
        target_label=row.name, tenant_id=effective_tenant,
        new_value={"dictId": provider_dict_id, "wordCounts": row.language_word_counts},
        request=request,
    )
    db.commit()
    return ok(_serialize(row))


@router.put("/pronunciation-dictionaries/{dictionary_id}")
async def update_dictionary(
    dictionary_id: str,
    body: DictionaryUpdateRequest,
    request: Request,
    tenant_id: str | None = None,
    user: User = Depends(require_permission("manage_voices", "bots.manage")),
    db: Session = Depends(get_db),
):
    effective_tenant = resolve_tenant_id(user, tenant_id)
    row = _get_row(db, effective_tenant, dictionary_id)
    before = _serialize(row)

    if body.pronunciations is not None:
        wire = _validate_pronunciations(body.pronunciations)
        key = _sarvam_key(db)
        await _provider_call(
            "PUT", f"{_SARVAM_DICTIONARY_URL}?dict_id={row.provider_dict_id}",
            key, files=_dictionary_file(wire),
        )
        row.language_word_counts = _word_counts(body.pronunciations)
    if body.name is not None:
        name = body.name.strip()
        clash = db.scalar(select(PronunciationDictionary).where(
            PronunciationDictionary.tenant_id == effective_tenant,
            PronunciationDictionary.is_deleted.is_(False),
            PronunciationDictionary.id != row.id,
            PronunciationDictionary.name == name,
        ))
        if clash is not None:
            raise ApiError(f"A dictionary named '{name}' already exists.", 422)
        row.name = name
    if body.description is not None:
        row.description = body.description.strip() or None
    row.updated_by = user.id
    record_audit(
        db, user=user, action="Updated pronunciation dictionary",
        entity_type="pronunciation_dictionary", entity_id=row.id,
        target_label=row.name, tenant_id=effective_tenant,
        previous_value=before, new_value=_serialize(row), request=request,
    )
    db.commit()
    return ok(_serialize(row))


@router.delete("/pronunciation-dictionaries/{dictionary_id}")
async def delete_dictionary(
    dictionary_id: str,
    request: Request,
    tenant_id: str | None = None,
    user: User = Depends(require_permission("manage_voices", "bots.manage")),
    db: Session = Depends(get_db),
):
    effective_tenant = resolve_tenant_id(user, tenant_id)
    row = _get_row(db, effective_tenant, dictionary_id)
    key = _sarvam_key(db)
    # Provider first: a 404 there means the dictionary is already gone —
    # proceed with the local delete rather than stranding the row.
    try:
        await _provider_call(
            "DELETE", f"{_SARVAM_DICTIONARY_URL}?dict_id={row.provider_dict_id}", key
        )
        provider_deleted = True
    except ApiError as exc:
        if "404" not in exc.message:
            raise
        provider_deleted = False
    soft_delete(row, user)
    record_audit(
        db, user=user, action="Deleted pronunciation dictionary",
        entity_type="pronunciation_dictionary", entity_id=row.id,
        target_label=row.name, tenant_id=effective_tenant,
        previous_value={"dictId": row.provider_dict_id}, request=request,
    )
    db.commit()
    return ok({"deleted": True, "providerDeleted": provider_deleted})
