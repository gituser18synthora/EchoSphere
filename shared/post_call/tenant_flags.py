"""Tenant-level switches for post-call intelligence.

Two independent Super Admin controls live on the tenant row:

- ``call_summary_enabled`` — whether the post-call summary / outcome / Next
  Best Action analysis runs at all (gates :func:`enqueue_post_call`).
- ``use_previous_call_summary`` — whether a new call loads the customer's
  latest stored summary into the bot context (gates
  :func:`load_previous_memory`).

Both are resolved server-side from the database at the moment they matter —
never trusted from a client payload — and FAIL CLOSED: an unknown tenant or
a broken lookup behaves like both switches being off, which is exactly the
platform default.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select

from shared.db.mysql import get_sessionmaker
from shared.models import Tenant

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantSummaryFlags:
    call_summary_enabled: bool = False
    use_previous_call_summary: bool = False


DISABLED = TenantSummaryFlags()


def load_tenant_summary_flags_sync(tenant_id: str | None, session=None) -> TenantSummaryFlags:
    """Current switches for one tenant; the platform-default (both off) for a
    missing tenant or a failed lookup. Pass ``session`` to reuse an open one."""
    if not tenant_id:
        return DISABLED
    own_session = session is None
    if own_session:
        session = get_sessionmaker()()
    try:
        row = session.execute(
            select(
                Tenant.call_summary_enabled, Tenant.use_previous_call_summary
            ).where(Tenant.id == tenant_id, Tenant.is_deleted.is_(False))
        ).one_or_none()
        if row is None:
            return DISABLED
        return TenantSummaryFlags(
            call_summary_enabled=bool(row[0]),
            use_previous_call_summary=bool(row[1]),
        )
    except Exception:  # noqa: BLE001 — a broken lookup means "off", never a crash
        logger.warning(
            "tenant summary-flag lookup failed for %s", tenant_id, exc_info=True
        )
        return DISABLED
    finally:
        if own_session:
            session.close()
