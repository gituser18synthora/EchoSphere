"""Soft-delete helpers. Hard deletes are blocked while ALLOW_HARD_DELETE=false."""

from datetime import datetime, timezone

from shared.config import get_settings
from shared.errors import HardDeleteBlockedError
from shared.models import User


def guard_hard_delete() -> None:
    """Raise unless hard deletes are explicitly enabled via env."""
    if not get_settings().allow_hard_delete:
        raise HardDeleteBlockedError()


def soft_delete(row, user: User | None, *, keep_status: bool = False) -> None:
    """Flag ``row`` deleted. By default the row's ``status`` also becomes
    ``archived``; pass ``keep_status=True`` for rows whose lifecycle status must
    survive as history (a deleted bot's tombstone keeps saying what it was)."""
    row.is_deleted = True
    row.deleted_at = datetime.now(timezone.utc)
    row.deleted_by = user.id if user else None
    if keep_status:
        return
    if hasattr(row, "status") and getattr(row, "status", None) not in ("archived",):
        row.status = "archived"


def not_deleted(model):
    """Filter expression excluding soft-deleted rows."""
    return model.is_deleted.is_(False)
