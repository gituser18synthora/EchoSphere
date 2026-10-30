"""Deterministic calling-window checks.

The decision is pure and clock-injectable: callers pass ``now`` (an aware
UTC datetime) in tests. Evaluation happens in the POLICY's IANA timezone via
``zoneinfo``, so daylight-saving transitions are handled by the timezone
database, not by hand-written offsets. A window may span midnight
(``start > end``); it belongs to the day it STARTS on.

The LLM plays no part here — an out-of-window call is refused before any
media connects.
"""

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from shared.compliance.policy import CompliancePolicySnapshot


@dataclass(frozen=True)
class CallWindowDecision:
    allowed: bool
    reason: str = ""
    policy_code: str | None = None
    policy_version: int | None = None
    local_time: str | None = None


def _parse_hhmm(value: str) -> time | None:
    try:
        hour, minute = str(value).split(":", 1)
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return None


def _window_matches(window: dict, local: datetime) -> bool:
    start = _parse_hhmm(window.get("start", ""))
    end = _parse_hhmm(window.get("end", ""))
    if start is None or end is None:
        return False  # malformed window never grants permission
    days = window.get("days")
    now_t = local.time()
    weekday = local.weekday()  # Monday = 0
    if start <= end:
        in_days = days is None or weekday in days
        return in_days and start <= now_t < end
    # Overnight window (e.g. 21:00–02:00): the segment after midnight belongs
    # to the day the window STARTED on.
    starts_today = (days is None or weekday in days) and now_t >= start
    prev_weekday = (weekday - 1) % 7
    started_yesterday = (days is None or prev_weekday in days) and now_t < end
    return starts_today or started_yesterday


def check_calling_window(
    policy: CompliancePolicySnapshot, now: datetime | None = None
) -> CallWindowDecision:
    """Whether a call is inside one of the policy's permitted windows.

    A policy without configured windows never restricts. ``now`` must be
    timezone-aware; naive datetimes are treated as UTC.
    """
    windows = policy.calling_windows or ()
    if not windows:
        return CallWindowDecision(allowed=True)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        local = now.astimezone(ZoneInfo(policy.timezone or "UTC"))
    except Exception:  # noqa: BLE001 — unknown zone: fail CLOSED for a
        # window-restricted policy; a typo must not silently allow 3 a.m. calls.
        return CallWindowDecision(
            allowed=False,
            reason=f"policy timezone '{policy.timezone}' is invalid",
            policy_code=policy.code, policy_version=policy.version,
        )
    for window in windows:
        if isinstance(window, dict) and _window_matches(window, local):
            return CallWindowDecision(
                allowed=True, policy_code=policy.code,
                policy_version=policy.version,
                local_time=local.strftime("%Y-%m-%d %H:%M %Z"),
            )
    return CallWindowDecision(
        allowed=False,
        reason="outside the permitted calling window",
        policy_code=policy.code, policy_version=policy.version,
        local_time=local.strftime("%Y-%m-%d %H:%M %Z"),
    )
