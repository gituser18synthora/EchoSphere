"""Management CLI.

Usage:
  python -m backend.cli migrate            # apply MySQL Alembic migrations
  python -m backend.cli pg-migrate         # apply PostgreSQL (knowledge plane) migrations
  python -m backend.cli seed               # idempotent base seed (profile follows APP_ENV)
  python -m backend.cli seed --demo        # + development demo dataset (refused in production)
  python -m backend.cli init-production    # explicit one-shot production initialisation
  python -m backend.cli verify-production  # read-only: report development/demo rows
  python -m backend.cli knowledge-verify   # cross-check sources/chunks/embeddings/indexes
  python -m backend.cli knowledge-reindex --kb <kb_id>   # safe re-index of one KB
"""

import argparse
import os
import sys


def cmd_migrate() -> None:
    from alembic import command
    from alembic.config import Config

    ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alembic.ini")
    command.upgrade(Config(ini), "head")
    print("Migrations applied (alembic upgrade head).")


def cmd_pg_migrate() -> None:
    from alembic import command
    from alembic.config import Config

    ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alembic_pg.ini")
    command.upgrade(Config(ini), "head")
    print("PostgreSQL knowledge-plane migrations applied.")


def cmd_seed(demo: bool) -> None:
    from shared.config import get_settings

    from backend.seeds.base_seed import run_base_seed

    if demo and get_settings().is_production:
        sys.exit(
            "seed --demo is refused when APP_ENV=production: the demo dataset is "
            "development-only."
        )
    created = run_base_seed()
    print(f"Base seed: {({k: v for k, v in created.items() if v}) or 'nothing new'}")
    if demo:
        from backend.seeds.demo_seed import run_demo_seed

        created = run_demo_seed()
        print(f"Demo seed: {created or 'nothing new (already present)'}")


def cmd_init_production(args) -> None:
    from shared.config import get_settings

    from backend.seeds.production_init import (
        ProductionInitError,
        format_report,
        init_production,
    )

    settings = get_settings()

    def password_provider() -> str | None:
        if args.password_stdin:
            return sys.stdin.readline().rstrip("\r\n")
        if settings.superadmin_password:
            return settings.superadmin_password
        if sys.stdin.isatty():
            import getpass

            return getpass.getpass(
                f"Initial password for super admin {settings.superadmin_email}: "
            )
        return None

    try:
        result = init_production(
            password_provider=password_provider,
            skip_migrations=args.skip_migrations,
            skip_pg_migrate=args.skip_pg_migrate,
        )
    except ProductionInitError as exc:
        sys.exit(f"init-production: {exc}")
    print(format_report(result))
    if result.violations:
        sys.exit(1)


def cmd_verify_production() -> None:
    from shared.config import get_settings
    from shared.db.mysql import get_sessionmaker

    from backend.seeds.production_init import InitResult, format_report, verify_production

    db = get_sessionmaker()()
    try:
        summary, violations = verify_production(db)
    finally:
        db.close()
    result = InitResult(summary=summary, violations=violations,
                        superadmin=f"(verify only, APP_ENV={get_settings().app_env})")
    print(format_report(result))
    if violations:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="backend.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("pg-migrate")
    seed = sub.add_parser("seed")
    seed.add_argument("--demo", action="store_true", help="also load the development demo dataset")
    init_prod = sub.add_parser(
        "init-production",
        help="migrate + production base seed + initial super admin + verification "
             "(requires APP_ENV=production; idempotent)",
    )
    init_prod.add_argument("--password-stdin", action="store_true",
                           help="read the initial super-admin password from stdin")
    init_prod.add_argument("--skip-migrations", action="store_true",
                           help="do not run the MySQL/PostgreSQL migrations")
    init_prod.add_argument("--skip-pg-migrate", action="store_true",
                           help="run the MySQL migrations only (knowledge plane migrated separately)")
    sub.add_parser("verify-production",
                   help="read-only: row summary plus any development/demo rows present")
    sub.add_parser("knowledge-verify", help="cross-check sources, chunks, embeddings and indexes")
    reindex = sub.add_parser("knowledge-reindex", help="queue a safe re-index of a knowledge base")
    reindex.add_argument("--kb", required=True, help="knowledge base (source) id")
    args = parser.parse_args()

    if args.cmd == "migrate":
        cmd_migrate()
    elif args.cmd == "pg-migrate":
        cmd_pg_migrate()
    elif args.cmd == "seed":
        cmd_seed(demo=args.demo)
    elif args.cmd == "init-production":
        cmd_init_production(args)
    elif args.cmd == "verify-production":
        cmd_verify_production()
    elif args.cmd == "knowledge-verify":
        from backend.knowledge_verify import run_verify

        sys.exit(1 if run_verify() else 0)
    elif args.cmd == "knowledge-reindex":
        from backend.knowledge_verify import run_reindex

        run_reindex(args.kb)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
