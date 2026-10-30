"""Per-caller contact-attempt limits (policy ``contact_limits.per_day``).

Counted in Redis per (tenant, bot-independent caller, policy-timezone day).
The caller number is HASHED before it becomes part of the key — raw phone
numbers never appear in Redis keys or logs. Counting happens at the moment
the platform ACCEPTS a call (the webhook); the check-and-increment is one
atomic INCR so concurrent webhooks cannot both pass a boundary.

Fail-open on a Redis outage (logged loudly): the alternative — refusing all
collections traffic whenever Redis blips — is a bigger operational hazard
than one over-limit attempt, and the authoritative attempt history remains
the conversation ledger.
"""

import hashlib
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from shared.compliance.policy import CompliancePolicySnapshot

logger = logging.getLogger(__name__)

_KEY_TTL_SECONDS = 60 * 60 * 50  # covers any timezone's day + margin


def _caller_digest(caller: str) -> str:
    normalized = "".join(ch for ch in caller if ch.isdigit())[-12:]
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


async def check_and_count_contact(
    policy: CompliancePolicySnapshot,
    *,
    tenant_id: str,
    caller: str | None,
    now: datetime | None = None,
) -> tuple[bool, int]:
    """Atomically count this attempt and return (allowed, attempts_today).

    A policy without ``contact_limits.per_day`` (or a call without a caller
    number) never restricts.
    """
    per_day = int((policy.contact_limits or {}).get("per_day") or 0)
    if per_day <= 0 or not caller:
        return True, 0
    now = now or datetime.now(timezone.utc)
    try:
        local_day = now.astimezone(ZoneInfo(policy.timezone or "UTC")).date()
    except Exception:  # noqa: BLE001 — bad zone already blocks via the window check
        local_day = now.date()
    key = f"ccl:{tenant_id}:{_caller_digest(caller)}:{local_day.isoformat()}"
    try:
        from shared.db.redis import get_redis

        redis = get_redis()
        attempts = int(await redis.incr(key))
        if attempts == 1:
            await redis.expire(key, _KEY_TTL_SECONDS)
        if attempts > per_day:
            return False, attempts
        return True, attempts
    except Exception:  # noqa: BLE001
        logger.error("contact-limit store unavailable; allowing call unchecked")
        return True, 0
