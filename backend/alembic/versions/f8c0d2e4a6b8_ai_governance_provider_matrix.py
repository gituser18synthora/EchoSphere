"""AI Governance: converge provider/model activation to the governed matrix.

Revision ID: f8c0d2e4a6b8
Revises: e5f7a9b1c3d5
Create Date: 2026-07-23

Data-only migration (no schema changes). Active providers per capability:

- llm:       openai
- embedding: openai
- stt:       sarvam
- tts:       sarvam, elevenlabs
- voice:     platform, elevenlabs   (voice catalogs follow their TTS vendor)

Everything else is deactivated — never deleted, IDs stay stable. The "mock"
pseudo-provider keeps its status (dev/test only; the catalog layer excludes it
from production). Platform-seeded AI profiles that referenced now-inactive
providers are re-pointed inside the matrix; operator-created profiles are left
untouched and surface as "inactive selection" in the UI/API instead.

The same rules live in backend/seeds/provider_catalog_seed.py
(reconcile_provider_governance), which re-converges on every bootstrap.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f8c0d2e4a6b8"
down_revision: Union[str, None] = "e5f7a9b1c3d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Platform-seeded AI profile codes (base_seed.AI_PROFILES) — the only profiles
# this migration re-points.
_SEEDED_PROFILES = "('low_cost','balanced','high_accuracy','low_latency','enterprise','custom')"


def upgrade() -> None:
    # ── Providers: deactivate everything outside the matrix ────────────────
    op.execute(
        """
        UPDATE provider_defs SET status='inactive'
        WHERE is_deleted = 0 AND status = 'active' AND (
              (kind = 'stt'       AND code NOT IN ('sarvam', 'mock'))
           OR (kind = 'tts'       AND code NOT IN ('sarvam', 'elevenlabs', 'mock'))
           OR (kind = 'llm'       AND code NOT IN ('openai', 'mock'))
           OR (kind = 'embedding' AND code NOT IN ('openai', 'mock'))
           OR (kind = 'voice'     AND code NOT IN ('platform', 'elevenlabs', 'mock'))
        )
        """
    )
    # ── Providers: the matrix members must be active ────────────────────────
    op.execute(
        """
        UPDATE provider_defs SET status='active'
        WHERE is_deleted = 0 AND status <> 'active' AND (
              (kind = 'stt'       AND code = 'sarvam')
           OR (kind = 'tts'       AND code IN ('sarvam', 'elevenlabs'))
           OR (kind = 'llm'       AND code = 'openai')
           OR (kind = 'embedding' AND code = 'openai')
        )
        """
    )
    # ── Provider models of disallowed providers ────────────────────────────
    op.execute(
        """
        UPDATE provider_models SET status='inactive'
        WHERE is_deleted = 0 AND status = 'active' AND (
              (capability = 'stt'       AND provider_code NOT IN ('sarvam', 'mock'))
           OR (capability = 'tts'       AND provider_code NOT IN ('sarvam', 'elevenlabs', 'mock'))
           OR (capability = 'llm'       AND provider_code NOT IN ('openai', 'mock'))
           OR (capability = 'embedding' AND provider_code NOT IN ('openai', 'mock'))
        )
        """
    )
    # ── Seeded AI profiles: re-point engines outside the matrix ────────────
    op.execute(
        f"""
        UPDATE ai_config_profiles
        SET stt_provider='sarvam', stt_model='saaras:v3'
        WHERE is_deleted = 0 AND code IN {_SEEDED_PROFILES}
          AND stt_provider IS NOT NULL AND stt_provider NOT IN ('sarvam')
        """
    )
    op.execute(
        f"""
        UPDATE ai_config_profiles
        SET tts_provider='sarvam', tts_model='bulbul:v3', default_voice='vp-sv-shubh'
        WHERE is_deleted = 0 AND code IN {_SEEDED_PROFILES}
          AND tts_provider IS NOT NULL AND tts_provider NOT IN ('sarvam', 'elevenlabs')
        """
    )
    op.execute(
        f"""
        UPDATE ai_config_profiles
        SET llm_provider='openai', llm_model='gpt-4o-mini'
        WHERE is_deleted = 0 AND code IN {_SEEDED_PROFILES}
          AND llm_provider IS NOT NULL AND llm_provider NOT IN ('openai')
        """
    )
    op.execute(
        f"""
        UPDATE ai_config_profiles
        SET embedding_provider='openai', embedding_model='text-embedding-3-small',
            embedding_dimension=1536
        WHERE is_deleted = 0 AND code IN {_SEEDED_PROFILES}
          AND embedding_provider IS NOT NULL AND embedding_provider NOT IN ('openai')
        """
    )
    # Seeded fallback stacks that referenced out-of-matrix vendors are cleared
    # (the JSON is free-form; anything mentioning a disallowed provider goes).
    op.execute(
        f"""
        UPDATE ai_config_profiles
        SET fallback_providers = NULL
        WHERE is_deleted = 0 AND code IN {_SEEDED_PROFILES}
          AND fallback_providers IS NOT NULL
          AND (CAST(fallback_providers AS CHAR) LIKE '%anthropic%'
               OR CAST(fallback_providers AS CHAR) LIKE '%google%'
               OR CAST(fallback_providers AS CHAR) LIKE '%azure%'
               OR CAST(fallback_providers AS CHAR) LIKE '%deepgram%'
               OR CAST(fallback_providers AS CHAR) LIKE '%assemblyai%')
        """
    )
    # ── Legacy approved-model registry (AI Governance page) ────────────────
    op.execute(
        """
        UPDATE approved_models SET status='deprecated'
        WHERE is_deleted = 0 AND status <> 'deprecated'
          AND LOWER(COALESCE(provider, '')) NOT IN ('openai', 'sarvam', 'elevenlabs')
        """
    )


def downgrade() -> None:
    # Data reconciliation is intentionally not reversible: the previous
    # activation state is preserved in the audit log, not in this migration.
    pass
