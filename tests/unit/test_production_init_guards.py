"""Production-safety guards around database initialisation (no database).

Covers: no startup seeding in production, SUPERADMIN_PASSWORD only required
where startup seeding runs, the demo dataset refused in production before any
database access, and a tripwire that no migration inserts business or demo
rows (which would bypass the production seed profile).
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import backend.main as main_mod  # imported in development mode (validates settings)
import backend.seeds.base_seed as seed_mod

REPO = Path(__file__).resolve().parents[2]


def _set_env(monkeypatch, **values: str) -> None:
    """Override environment variables and drop the cached Settings."""
    from shared.config import get_settings

    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_settings():
    from shared.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_database_access(monkeypatch):
    """These are unit tests: any attempt to open a MySQL session fails fast.

    (A cached-settings mistake once let a test here reach the shared
    development database.) The seed modules import get_sessionmaker by name,
    so the guard is installed at every call site, not just in shared.db.mysql.
    """
    import shared.db.mysql as mysql_db
    import backend.seeds.base_seed as base_seed
    import backend.seeds.demo_seed as demo_seed
    import backend.seeds.production_init as production_init

    def forbidden(*args, **kwargs):
        raise AssertionError("unit guard tests must not open a database connection")

    for module in (mysql_db, base_seed, demo_seed, production_init):
        monkeypatch.setattr(module, "get_sessionmaker", forbidden)
    monkeypatch.setattr(mysql_db, "get_engine", forbidden)

# Tables only application code or operators may populate.
_BUSINESS_TABLES = {
    "users", "tenants", "voice_bots", "plans", "subscriptions", "invoices",
    "platform_templates", "integrations", "approved_models", "phone_numbers",
    "sip_trunks", "usage_records", "conversation_sessions", "tenant_settings",
    "knowledge_sources", "channel_configs", "api_connections",
}
_DEMO_LITERALS = (r"vp-0[1-8]\b", r"usr-demo-", r"\btn-00\d\b", r"\bbot-10\d\b", r"Demo@")


def _migration_files() -> list[Path]:
    return sorted((REPO / "backend/alembic/versions").glob("*.py")) + sorted(
        (REPO / "backend/alembic_pg/versions").glob("*.py")
    )


class TestMigrationsCannotBypassProductionProfile:
    def test_no_migration_upgrade_inserts_business_or_demo_rows(self):
        offenders: list[str] = []
        for path in _migration_files():
            upgrade = path.read_text().split("def downgrade")[0]
            for table in re.findall(r"INSERT\s+INTO\s+`?(\w+)`?", upgrade, re.I):
                if table in _BUSINESS_TABLES:
                    offenders.append(f"{path.name}: INSERT INTO {table}")
            for var in re.findall(r"bulk_insert\(\s*(\w+)", upgrade):
                match = re.search(
                    rf"\b{var}\s*=\s*(?:op\.create_table|sa\.table|table)\(\s*[\"'](\w+)[\"']",
                    upgrade,
                )
                assert match, f"{path.name}: cannot resolve bulk_insert target '{var}'"
                if match.group(1) in _BUSINESS_TABLES:
                    offenders.append(f"{path.name}: bulk_insert into {match.group(1)}")
            for literal in _DEMO_LITERALS:
                if re.search(literal, upgrade):
                    offenders.append(f"{path.name}: demo/sample literal {literal!r}")
        assert offenders == [], "\n".join(offenders)


class TestSettingsGuards:
    def test_startup_seed_never_runs_in_production(self):
        from shared.config import Settings

        assert Settings(app_env="production", enable_database_seed=True).startup_seed_enabled is False
        assert Settings(app_env="development", enable_database_seed=True).startup_seed_enabled is True
        assert Settings(app_env="development", enable_database_seed=False).startup_seed_enabled is False

    def test_production_api_does_not_require_superadmin_password(self, monkeypatch):
        from shared.config import ConfigurationError, validate_settings

        _set_env(monkeypatch, APP_ENV="production", ENABLE_DATABASE_SEED="true",
                 SUPERADMIN_PASSWORD="")
        try:
            validate_settings("api")
        except ConfigurationError as exc:  # other production checks may fire locally
            assert "SUPERADMIN_PASSWORD" not in str(exc)

    def test_development_api_still_requires_superadmin_password(self, monkeypatch):
        from shared.config import ConfigurationError, validate_settings

        _set_env(monkeypatch, APP_ENV="development", ENABLE_DATABASE_SEED="true",
                 SUPERADMIN_PASSWORD="")
        with pytest.raises(ConfigurationError, match="SUPERADMIN_PASSWORD"):
            validate_settings("api")


class TestDemoSeedGuard:
    def test_demo_seed_refused_in_production_before_any_database_access(self, monkeypatch):
        from backend.seeds.demo_seed import run_demo_seed

        _set_env(monkeypatch, APP_ENV="production")

        class Untouchable:
            def __getattr__(self, name):
                raise AssertionError(f"database session touched via .{name}")

        with pytest.raises(RuntimeError, match="refused"):
            run_demo_seed(db=Untouchable())

    def test_cli_seed_demo_exits_in_production(self, monkeypatch):
        from backend.cli import cmd_seed

        _set_env(monkeypatch, APP_ENV="production")
        with pytest.raises(SystemExit) as exc:
            cmd_seed(demo=True)
        assert "refused" in str(exc.value)


class TestStartupSeedGuard:
    @pytest.fixture()
    def quiet_lifespan(self, monkeypatch):
        _set_env(monkeypatch, ENABLE_DATABASE_SEED="true", AUTO_RUN_MIGRATIONS="false",
                 INGESTION_WORKER_EMBEDDED="false")
        monkeypatch.setattr(main_mod.Mongo, "connect", AsyncMock())
        monkeypatch.setattr(main_mod.Mongo, "disconnect", AsyncMock())
        monkeypatch.setattr(main_mod, "create_indexes", AsyncMock())
        calls: list[dict] = []
        monkeypatch.setattr(seed_mod, "run_base_seed", lambda *a, **kw: calls.append(kw))
        return calls

    async def test_lifespan_skips_seed_in_production(self, quiet_lifespan, monkeypatch):
        _set_env(monkeypatch, APP_ENV="production")
        async with main_mod.lifespan(main_mod.app):
            pass
        assert quiet_lifespan == []

    async def test_lifespan_seeds_in_development(self, quiet_lifespan, monkeypatch):
        _set_env(monkeypatch, APP_ENV="development")
        async with main_mod.lifespan(main_mod.app):
            pass
        assert len(quiet_lifespan) == 1
