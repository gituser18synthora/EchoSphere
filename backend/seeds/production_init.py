"""Explicit, idempotent production initialisation.

Production never seeds at API startup (``Settings.startup_seed_enabled``).
Instead ``python -m backend.cli init-production`` runs, in order:

1. the MySQL and PostgreSQL schema migrations,
2. the production profile of the base seed (genuine master data only),
3. creation of the single initial super admin — the password comes from
   ``SUPERADMIN_PASSWORD`` if set, otherwise from the CLI (stdin/prompt), so it
   never has to stay in the environment once the account exists,
4. a verification pass that lists every development/demo row that must not
   exist in production.

Every step is safe to repeat: re-running creates nothing new.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.db.mysql import get_sessionmaker
from shared.models import (
    ApprovedModel,
    Country,
    Currency,
    Guardrail,
    GuardrailProfile,
    Industry,
    Integration,
    Permission,
    Plan,
    PlatformTemplate,
    ProviderDef,
    ProviderModel,
    ProviderPricing,
    Role,
    SupportedLanguage,
    Tenant,
    User,
    VoiceBot,
    VoiceProfile,
)

logger = logging.getLogger("backend.production_init")


class ProductionInitError(RuntimeError):
    """Initialisation cannot continue safely (usage error, not a crash)."""


@dataclass
class InitResult:
    created: dict = field(default_factory=dict)
    superadmin: str = ""
    summary: dict = field(default_factory=dict)
    violations: dict = field(default_factory=dict)


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


def non_production_rows(db: Session) -> dict[str, int]:
    """Rows that only the development seed or the demo dataset create.

    Returns ``{description: count}`` for every category that is non-empty.
    Sample plans are deliberately not listed: operators create real plans
    through the master-data UI and their codes cannot be told apart.
    """
    from backend.seeds.base_seed import VOICES
    from backend.seeds.demo_seed import BOTS, TENANTS, USERS

    sample_voice_ids = [row[0] for row in VOICES]
    checks = {
        "mock provider definitions":
            select(func.count()).select_from(ProviderDef).where(ProviderDef.code == "mock"),
        "mock provider models":
            select(func.count()).select_from(ProviderModel)
            .where(ProviderModel.provider_code == "mock"),
        "sample 'platform' voices":
            select(func.count()).select_from(VoiceProfile)
            .where(VoiceProfile.id.in_(sample_voice_ids)),
        "platform template rows":
            select(func.count()).select_from(PlatformTemplate),
        "integration catalog rows":
            select(func.count()).select_from(Integration),
        "approved-model registry rows":
            select(func.count()).select_from(ApprovedModel),
        "demo tenants":
            select(func.count()).select_from(Tenant)
            .where(Tenant.id.in_([row[0] for row in TENANTS])),
        "demo users":
            select(func.count()).select_from(User)
            .where(User.id.in_([row[0] for row in USERS])),
        "demo bots":
            select(func.count()).select_from(VoiceBot)
            .where(VoiceBot.id.in_([row[0] for row in BOTS])),
    }
    found = {name: _count(db, stmt) for name, stmt in checks.items()}
    return {name: count for name, count in found.items() if count}


def summary(db: Session) -> dict[str, int]:
    """Row counts an operator checks after initialisation."""
    super_admin = db.scalar(select(Role).where(Role.code == "super_admin"))
    voice_rows = db.execute(
        select(VoiceProfile.provider, func.count()).group_by(VoiceProfile.provider)
    ).all()
    counts = {
        "roles": _count(db, select(func.count()).select_from(Role)),
        "permissions": _count(db, select(func.count()).select_from(Permission)),
        "industries": _count(db, select(func.count()).select_from(Industry)),
        "countries": _count(db, select(func.count()).select_from(Country)),
        "languages": _count(db, select(func.count()).select_from(SupportedLanguage)),
        "providers (active)": _count(
            db, select(func.count()).select_from(ProviderDef)
            .where(ProviderDef.status == "active", ProviderDef.is_deleted.is_(False))),
        "provider models": _count(db, select(func.count()).select_from(ProviderModel)),
        "guardrails": _count(db, select(func.count()).select_from(Guardrail)),
        "guardrail profiles": _count(db, select(func.count()).select_from(GuardrailProfile)),
        "currencies": _count(db, select(func.count()).select_from(Currency)),
        "provider pricing rows": _count(db, select(func.count()).select_from(ProviderPricing)),
        "plans": _count(db, select(func.count()).select_from(Plan)),
        "tenants": _count(db, select(func.count()).select_from(Tenant)),
        "bots": _count(db, select(func.count()).select_from(VoiceBot)),
        "users": _count(db, select(func.count()).select_from(User)),
        "super admins": _count(
            db, select(func.count()).select_from(User)
            .where(User.role_id == (super_admin.id if super_admin else ""),
                   User.is_deleted.is_(False))),
    }
    for provider, count in sorted(voice_rows):
        counts[f"voices ({provider})"] = int(count)
    return counts


def verify_production(db: Session) -> tuple[dict[str, int], dict[str, int]]:
    """``(summary, violations)`` for a read-only audit of any database."""
    return summary(db), non_production_rows(db)


def init_production(
    *,
    password_provider: Callable[[], str | None],
    skip_migrations: bool = False,
    skip_pg_migrate: bool = False,
) -> InitResult:
    """Run the full, idempotent production initialisation (see module doc)."""
    from shared.config import get_settings

    from backend.seeds.base_seed import ensure_super_admin, run_base_seed

    settings = get_settings()
    if not settings.is_production:
        raise ProductionInitError(
            f"init-production requires APP_ENV=production (current: {settings.app_env!r})."
        )

    if not skip_migrations:
        from backend.cli import cmd_migrate, cmd_pg_migrate

        cmd_migrate()
        if not skip_pg_migrate:
            cmd_pg_migrate()

    result = InitResult()
    result.created = run_base_seed(production=True)

    db = get_sessionmaker()()
    try:
        role = db.scalar(select(Role).where(Role.code == "super_admin"))
        existing = _count(
            db, select(func.count()).select_from(User)
            .where(User.role_id == role.id, User.is_deleted.is_(False)),
        )
        if result.created.get("users"):
            # Created by the seed step from SUPERADMIN_PASSWORD in the environment.
            result.superadmin = f"created {settings.superadmin_email.lower()} (from SUPERADMIN_PASSWORD)"
        elif existing:
            result.superadmin = f"present ({existing} account(s)); nothing created"
        else:
            password = password_provider()
            if not password:
                raise ProductionInitError(
                    "No super admin exists and no initial password was supplied — "
                    "pass it on stdin (--password-stdin), answer the prompt, or set "
                    "SUPERADMIN_PASSWORD for this one run."
                )
            created = ensure_super_admin(
                db, role=role, email=settings.superadmin_email,
                name=settings.superadmin_name, password=password,
                allow_additional=False,
            )
            db.commit()
            result.created["users"] = result.created.get("users", 0) + created
            result.superadmin = (
                f"created {settings.superadmin_email.lower()}" if created
                else "present; nothing created"
            )
        result.summary, result.violations = verify_production(db)
    finally:
        db.close()
    return result


def format_report(result: InitResult) -> str:
    created = {k: v for k, v in result.created.items() if v}
    lines = [
        f"Base seed (production profile): {created or 'nothing new'}",
        f"Super admin: {result.superadmin}",
        "Database summary:",
    ]
    lines += [f"  {name:<28} {count}" for name, count in result.summary.items()]
    if result.violations:
        lines.append("DEVELOPMENT/DEMO ROWS PRESENT — this database is not production-clean:")
        lines += [f"  {name:<28} {count}" for name, count in result.violations.items()]
    else:
        lines.append("Verification: no development or demo rows present.")
    return "\n".join(lines)
