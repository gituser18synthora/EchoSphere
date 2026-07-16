"""Management CLI.

Usage:
  python -m backend.cli migrate            # apply MySQL Alembic migrations
  python -m backend.cli pg-migrate         # apply PostgreSQL (knowledge plane) migrations
  python -m backend.cli seed               # idempotent base seed
  python -m backend.cli seed --demo        # + development demo dataset (opt-in)
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
    from backend.seeds.base_seed import run_base_seed

    created = run_base_seed()
    print(f"Base seed: {({k: v for k, v in created.items() if v}) or 'nothing new'}")
    if demo:
        from backend.seeds.demo_seed import run_demo_seed

        created = run_demo_seed()
        print(f"Demo seed: {created or 'nothing new (already present)'}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="backend.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("pg-migrate")
    seed = sub.add_parser("seed")
    seed.add_argument("--demo", action="store_true", help="also load the development demo dataset")
    args = parser.parse_args()

    if args.cmd == "migrate":
        cmd_migrate()
    elif args.cmd == "pg-migrate":
        cmd_pg_migrate()
    elif args.cmd == "seed":
        cmd_seed(demo=args.demo)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
