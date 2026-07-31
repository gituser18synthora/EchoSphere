"""Eleven v3 model languages become platform-catalog-derived locale codes.

Revision ID: b3e5a7c9d1f3
Revises: a7c9e1b3d5f7
Create Date: 2026-07-30

The eleven_v3 provider_models row previously stored the provider's raw
70+-language ISO list. The supported_languages table is the platform's source
of truth for selectable languages, so the row is converted to the platform
locale codes (en-US, en-IN, hi-IN, …) whose canonical base code (iso_code,
falling back to the locale prefix) is officially supported by Eleven v3
(verified against elevenlabs.io/docs, 2026-07-30 — no Odia). Languages
without a catalog record are dropped from the row; enable/disable state keeps
being applied at read time.

Guarded conversion: only a row whose languages still EXACTLY equal the legacy
bare-ISO list is rewritten (provably unedited); operator-managed rows are
left untouched. No-op when the languages table is empty (fresh databases —
the bootstrap seed derives the list itself). Data-only, no schema changes.

Rollback restores the legacy bare-ISO list when the row still carries a
catalog-derived list.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3e5a7c9d1f3"
down_revision: Union[str, None] = "a7c9e1b3d5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Official Eleven v3 base ISO codes (elevenlabs.io/docs, 2026-07-30).
_ELEVEN_V3_ISO_CODES = frozenset({
    "af", "ar", "hy", "as", "az", "be", "bn", "bs", "bg", "ca", "ceb", "ny",
    "hr", "cs", "da", "nl", "en", "et", "fil", "fi", "fr", "gl", "ka", "de",
    "el", "gu", "ha", "he", "hi", "hu", "is", "id", "ga", "it", "ja", "jv",
    "kn", "kk", "ky", "ko", "lv", "ln", "lt", "lb", "mk", "ms", "ml", "zh",
    "cmn", "mr", "ne", "no", "ps", "fa", "pl", "pt", "pa", "ro", "ru", "sr",
    "sd", "sk", "sl", "so", "es", "sw", "sv", "ta", "te", "th", "tr", "uk",
    "ur", "vi", "cy",
})

# Exact pre-conversion row shape (what a7c9e1b3d5f7 / the old seed wrote).
_LEGACY_BARE_CODES = [
    "af", "ar", "hy", "as", "az", "be", "bn", "bs", "bg", "ca", "ceb", "ny",
    "hr", "cs", "da", "nl", "en", "et", "fil", "fi", "fr", "gl", "ka", "de",
    "el", "gu", "ha", "he", "hi", "hu", "is", "id", "ga", "it", "ja", "jv",
    "kn", "kk", "ky", "ko", "lv", "ln", "lt", "lb", "mk", "ms", "ml", "zh",
    "mr", "ne", "no", "ps", "fa", "pl", "pt", "pa", "ro", "ru", "sr", "sd",
    "sk", "sl", "so", "es", "sw", "sv", "ta", "te", "th", "tr", "uk", "ur",
    "vi", "cy",
]


def _load_row(bind):
    return bind.execute(
        sa.text(
            "SELECT id, languages FROM provider_models WHERE "
            "provider_code = 'elevenlabs' AND capability = 'tts' "
            "AND code = 'eleven_v3'"
        )
    ).first()


def _parse(raw) -> list:
    if isinstance(raw, list):
        return raw
    return json.loads(raw or "[]")


def _catalog_locales(bind) -> list[str]:
    rows = bind.execute(
        sa.text(
            "SELECT code, iso_code FROM supported_languages "
            "ORDER BY sort_order, code"
        )
    ).all()
    locales: list[str] = []
    for code, iso in rows:
        code = (code or "").strip()
        if not code or code in locales:
            continue
        base = (iso or code.split("-")[0]).strip().lower()
        if base in _ELEVEN_V3_ISO_CODES:
            locales.append(code)
    return locales


def upgrade() -> None:
    bind = op.get_bind()
    row = _load_row(bind)
    if row is None:
        return
    if _parse(row.languages) != _LEGACY_BARE_CODES:
        return  # operator-managed (or already converted) — leave untouched
    locales = _catalog_locales(bind)
    if not locales:
        return  # fresh database: the bootstrap seed derives the list itself
    bind.execute(
        sa.text("UPDATE provider_models SET languages = :languages WHERE id = :id"),
        {"languages": json.dumps(locales), "id": row.id},
    )


def downgrade() -> None:
    bind = op.get_bind()
    row = _load_row(bind)
    if row is None:
        return
    current = _parse(row.languages)
    # Restore only a still-catalog-derived list; keep operator-edited rows.
    if current and set(current) <= set(_catalog_locales(bind)):
        bind.execute(
            sa.text("UPDATE provider_models SET languages = :languages WHERE id = :id"),
            {"languages": json.dumps(_LEGACY_BARE_CODES), "id": row.id},
        )
