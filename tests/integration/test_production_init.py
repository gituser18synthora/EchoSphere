"""Production initialisation on an EMPTY database.

Uses the ``disposable_mysql_database`` fixture (tests/conftest.py): a brand-new
schema that Settings and the process-wide engine point at for the duration of
the test and that is dropped afterwards. The fixture also snapshots the shared
development database and fails the test if it changed. The CLI is exercised
as a real subprocess with the disposable schema passed explicitly.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from shared.config import get_settings
from shared.db.mysql import get_sessionmaker
from shared.models import (
    ProviderDef,
    ProviderModel,
    Role,
    SystemSetting,
    Tenant,
    User,
    VoiceProfile,
)

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]


def _set_env(monkeypatch, **values: str) -> None:
    """Override environment variables and drop the cached Settings."""
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def _migrate_mysql(database: str) -> None:
    assert get_settings().mysql_database == database
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(REPO / "backend/alembic.ini")), "head")


def _session(database: str):
    """The process-wide sessionmaker — must already point at the disposable schema."""
    session = get_sessionmaker()()
    assert session.get_bind().url.database == database
    return session


class TestProductionProfileOnEmptyDatabase:
    def test_production_seed_creates_only_master_data_and_one_admin(
        self, disposable_mysql_database, monkeypatch,
    ):
        database = disposable_mysql_database
        _migrate_mysql(database)
        _set_env(monkeypatch, APP_ENV="production", SUPERADMIN_EMAIL="owner@example.test",
                 SUPERADMIN_NAME="Owner", SUPERADMIN_PASSWORD="Initial-Pass-1")

        from backend.seeds.base_seed import (
            COUNTRIES, CURRENCIES, GUARDRAILS, GUARDRAIL_PROFILES, INDUSTRIES,
            LANGUAGES, TEMPLATES, run_base_seed,
        )
        from backend.seeds.production_init import non_production_rows, summary

        db = _session(database)
        try:
            created = run_base_seed(db=db)  # profile follows APP_ENV
            for group in ("plans", "voices", "models", "integrations", "templates"):
                assert created[group] == 0, (group, created)
            assert created["users"] == 1
            assert non_production_rows(db) == {}

            counts = summary(db)
            assert counts["tenants"] == 0 and counts["bots"] == 0 and counts["plans"] == 0
            assert counts["users"] == 1 and counts["super admins"] == 1
            assert counts["roles"] == 3 and counts["industries"] == len(INDUSTRIES)
            assert counts["languages"] == len(LANGUAGES)
            assert counts["countries"] >= len(COUNTRIES)
            assert counts["guardrails"] >= len(GUARDRAILS)
            assert counts["guardrail profiles"] >= len(GUARDRAIL_PROFILES)
            assert counts["currencies"] == len(CURRENCIES)
            assert counts["provider pricing rows"] > 0
            for provider in ("sarvam", "elevenlabs", "deepgram"):
                assert counts.get(f"voices ({provider})", 0) > 0, provider
            assert "voices (platform)" not in counts

            assert db.scalar(select(func.count()).select_from(ProviderDef)
                             .where(ProviderDef.code == "mock")) == 0
            assert db.scalar(select(func.count()).select_from(ProviderModel)
                             .where(ProviderModel.provider_code == "mock")) == 0
            active = {(p.kind, p.code) for p in db.scalars(
                select(ProviderDef).where(ProviderDef.status == "active"))}
            assert {("stt", "sarvam"), ("tts", "elevenlabs"), ("llm", "openai"),
                    ("embedding", "openai")} <= active
            assert not any(code == "mock" for _, code in active)

            setting = db.scalar(select(SystemSetting)
                                .where(SystemSetting.key == "voice.default_provider"))
            assert setting.value["provider"] == "sarvam"
            assert setting.value["voice_id"] == "vp-sv-shubh"
            assert db.get(VoiceProfile, setting.value["voice_id"]) is not None

            user = db.scalar(select(User))
            assert user.email == "owner@example.test"
            assert db.get(Role, user.role_id).code == "super_admin"

            # Idempotent: a second run creates and changes nothing.
            assert not any(run_base_seed(db=db).values())

            # Production: a changed SUPERADMIN_EMAIL never adds a second owner.
            _set_env(monkeypatch, SUPERADMIN_EMAIL="second@example.test")
            assert run_base_seed(db=db)["users"] == 0
            assert db.scalar(select(func.count()).select_from(User)) == 1

            # The development profile is unchanged: on the same schema it still
            # adds its catalog samples, mock providers and the extra admin.
            dev = run_base_seed(db=db, production=False)
            assert dev["plans"] == 3 and dev["voices"] == 8 and dev["models"] == 5
            assert dev["integrations"] == 6 and dev["templates"] == len(TEMPLATES)
            assert dev["providers"] == 4 and dev["provider_models"] == 4  # mock rows
            assert dev["users"] == 1
            assert {
                "mock provider definitions", "mock provider models",
                "sample 'platform' voices", "platform template rows",
                "integration catalog rows", "approved-model registry rows",
            } <= set(non_production_rows(db))
        finally:
            db.close()


class TestDemoSeedIsolation:
    def test_demo_seed_runs_only_on_the_disposable_schema(
        self, disposable_mysql_database, monkeypatch,
    ):
        """The demo dataset lands in the disposable schema; the conftest guard
        refuses any other target. Covers the exact failure that once wrote six
        demo invoices into the shared development database."""
        database = disposable_mysql_database
        _migrate_mysql(database)
        _set_env(monkeypatch, APP_ENV="development", SUPERADMIN_EMAIL="dev@example.test",
                 SUPERADMIN_PASSWORD="Dev-Pass-1")
        from backend.cli import cmd_seed

        cmd_seed(demo=True)  # base seed + demo dataset, both through get_sessionmaker()

        db = _session(database)
        try:
            assert db.get(Tenant, "tn-001") is not None
            assert db.scalar(select(func.count()).select_from(Tenant)) >= 9
        finally:
            db.close()

    def test_demo_seed_refuses_a_non_disposable_target(self):
        from backend.seeds.demo_seed import run_demo_seed

        with pytest.raises(AssertionError, match="shared database"):
            run_demo_seed()  # would resolve to the shared development database


class TestInitProductionCli:
    def _run(self, args, env, stdin=""):
        return subprocess.run(
            [sys.executable, "-m", "backend.cli", *args], cwd=REPO, env=env,
            input=stdin, capture_output=True, text=True, timeout=900,
        )

    def test_init_production_end_to_end(self, disposable_mysql_database):
        database = disposable_mysql_database
        env = {
            **os.environ,
            "APP_ENV": "production",
            "MYSQL_DATABASE": database,
            "SUPERADMIN_EMAIL": "owner@example.test",
            "SUPERADMIN_NAME": "Owner",
            "SUPERADMIN_PASSWORD": "",  # never in the environment
            "ENABLE_DATABASE_SEED": "true",
        }
        init = ["init-production", "--password-stdin", "--skip-pg-migrate"]

        first = self._run(init, env, stdin="Initial-Pass-1\n")
        assert first.returncode == 0, first.stdout + first.stderr
        assert "created owner@example.test" in first.stdout
        assert "no development or demo rows present" in first.stdout

        second = self._run(init, env, stdin="")  # no password needed any more
        assert second.returncode == 0, second.stdout + second.stderr
        assert "nothing new" in second.stdout
        assert "present (1 account(s))" in second.stdout

        demo = self._run(["seed", "--demo"], env)
        assert demo.returncode != 0
        assert "refused" in demo.stdout + demo.stderr

        verify = self._run(["verify-production"], env)
        assert verify.returncode == 0, verify.stdout + verify.stderr
        assert "no development or demo rows present" in verify.stdout

        wrong_env = self._run(init, {**env, "APP_ENV": "development"}, stdin="x\n")
        assert wrong_env.returncode != 0
        assert "requires APP_ENV=production" in wrong_env.stdout + wrong_env.stderr

        db = _session(database)
        try:
            assert db.scalar(select(func.count()).select_from(User)) == 1
            assert db.scalar(select(func.count()).select_from(Tenant)) == 0
            assert db.scalar(select(func.count()).select_from(ProviderDef)
                             .where(ProviderDef.code == "mock")) == 0
        finally:
            db.close()
