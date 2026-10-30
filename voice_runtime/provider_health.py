"""Process-wide provider outage detection for the voice runtime.

One vote per call: a call's FIRST provider outcome — the decision warm-up,
a decision, or the reply's first token — is a success or a
:class:`~shared.providers.base.ProviderError`. Three consecutive calls,
across every bot this process serves, whose first outcome is a failure open
an outage: ONE ``ERROR`` log line carrying the provider, the HTTP status and
the message, and a ``provider_outage`` event on the call that tripped it
(status and message included, so the next outage is diagnosable from the
call documents without the journal). The next call whose first outcome
succeeds closes it with a ``provider_recovered`` event.

Only account- or service-level failures count: ``auth`` (401/403),
``rate_limit`` (429, including an exceeded spend limit) and ``upstream``
(5xx, connection failures). A timeout or an invalid request is that call's
own problem and never votes.

Why one vote per call rather than per request: an outage makes every new
call's warm-up fail within a second, so three new calls trip the alert
quickly, while one bad request inside an otherwise healthy call cannot.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from shared.providers.base import ProviderError, error_fields

logger = logging.getLogger(__name__)

OUTAGE_CATEGORIES = frozenset({"auth", "rate_limit", "upstream"})
CONSECUTIVE_CALLS = 3
_VOTES_REMEMBERED = 2000


@dataclass
class _Failure:
    provider: str
    category: str
    status: int | None
    message: str
    stage: str
    at: datetime


class ProviderHealth:
    def __init__(self, *, consecutive_calls: int = CONSECUTIVE_CALLS) -> None:
        self._consecutive_calls = consecutive_calls
        self.reset()

    def reset(self) -> None:
        self._voted: OrderedDict[str, bool] = OrderedDict()
        self._streak: list[_Failure] = []
        self.outage: dict | None = None

    # ── votes ───────────────────────────────────────────────────────────
    def _vote(self, session_id: str, ok: bool) -> bool:
        """True when this is the call's first vote (later outcomes are ignored)."""
        key = str(session_id or "")
        if key in self._voted:
            return False
        self._voted[key] = ok
        while len(self._voted) > _VOTES_REMEMBERED:
            self._voted.popitem(last=False)
        return True

    def succeeded(self, session_id: str) -> dict | None:
        """A call's first provider outcome was a success. Returns the
        ``provider_recovered`` event to record when it closes an outage."""
        if not self._vote(session_id, True):
            return None
        self._streak = []
        outage, self.outage = self.outage, None
        if outage is None:
            return None
        now = datetime.now(timezone.utc)
        since = outage["since"]
        duration = round((now - since).total_seconds(), 1)
        logger.info(
            "provider recovered: %s after %d failed calls over %.0f s (since %s)",
            outage["provider"], outage["failed_calls"], duration, since.isoformat(),
        )
        return {
            "kind": "provider_recovered",
            "provider": outage["provider"],
            "failed_calls": outage["failed_calls"],
            "since": since.isoformat(),
            "duration_s": duration,
        }

    def failed(
        self, session_id: str, error: BaseException | None, *, stage: str,
    ) -> dict | None:
        """A call's first provider outcome was ``error``. Returns the
        ``provider_outage`` event to record when it opens an outage."""
        if not isinstance(error, ProviderError) or error.category not in OUTAGE_CATEGORIES:
            return None
        if not self._vote(session_id, False):
            return None
        failure = _Failure(
            provider=error.provider, category=error.category, status=error.status,
            message=error.message[:200], stage=stage, at=datetime.now(timezone.utc),
        )
        if self.outage is not None:
            self.outage["failed_calls"] += 1
            return None
        self._streak.append(failure)
        if len(self._streak) < self._consecutive_calls:
            return None
        first = self._streak[0]
        self.outage = {
            "provider": failure.provider, "since": first.at,
            "failed_calls": len(self._streak),
        }
        self._streak = []
        logger.error(
            "provider outage: %d consecutive calls failed (%s at %s%s %s: %s) since %s",
            self.outage["failed_calls"], failure.provider, failure.stage,
            f", HTTP {failure.status}" if failure.status else "", failure.category,
            failure.message, first.at.isoformat(),
        )
        return {
            "kind": "provider_outage",
            "provider": failure.provider,
            "category": failure.category,
            "status": failure.status,
            "message": failure.message,
            "stage": failure.stage,
            "calls": self.outage["failed_calls"],
            "since": first.at.isoformat(),
        }


provider_health = ProviderHealth()

__all__ = ["ProviderHealth", "provider_health", "error_fields", "OUTAGE_CATEGORIES"]
