"""Runtime schema feature detection.

Some features ship their code before their Alembic migration has been
applied to every environment (migrations need operator approval). Code that
touches such a column/table asks here first and degrades gracefully when
the schema is not there yet. Results are cached per process; call
:func:`reset` after running migrations in-process (tests).
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect

from shared.db.mysql import get_engine

logger = logging.getLogger(__name__)
_cache: dict[tuple[str, str | None], bool] = {}


def table_exists(table: str) -> bool:
    key = (table, None)
    if key not in _cache:
        try:
            _cache[key] = bool(inspect(get_engine()).has_table(table))
        except Exception:  # noqa: BLE001 — never fail a request over introspection
            logger.exception("schema introspection failed for table %s", table)
            return False
    return _cache[key]


def column_exists(table: str, column: str) -> bool:
    key = (table, column)
    if key not in _cache:
        try:
            if not table_exists(table):
                _cache[key] = False
            else:
                names = {c["name"] for c in inspect(get_engine()).get_columns(table)}
                _cache[key] = column in names
        except Exception:  # noqa: BLE001
            logger.exception("schema introspection failed for %s.%s", table, column)
            return False
    return _cache[key]


def reset() -> None:
    _cache.clear()
