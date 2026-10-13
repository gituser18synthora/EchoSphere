"""Per-turn latency attribution from pipecat's ``UserBotLatencyObserver``.

The brain already records ``turn_latency`` (our own spans: speech, STT final,
endpoint, classify, LLM first token, TTS queue/TTFB, playout) and
``turn_timing``. pipecat >= 1.9 measures the same user-to-bot interval from
the pipeline's point of view and names the parts no service reports — VAD
silence, turn detection, turn-completion holds, sentence aggregation,
function handlers — as ``LatencyBreakdown.contributions`` that sum to the
measured total, plus per-service TTFB. This observer stores that record on
the call as a ``turn_latency_breakdown`` event next to ours, so a slow turn
can be attributed with both views without re-deriving anything.

Nothing here is on the audio path: the observer only watches frame pushes,
and recording failures are swallowed (telemetry must never affect a call).
"""

from __future__ import annotations

import logging
from typing import Any

from pipecat.observers.user_bot_latency_observer import (
    LatencyBreakdown,
    UserBotLatencyObserver,
)

logger = logging.getLogger(__name__)

EVENT_KIND = "turn_latency_breakdown"


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(float(seconds) * 1000.0, 1)


def breakdown_event(breakdown: LatencyBreakdown) -> dict[str, Any]:
    """JSON-safe event payload for one ``LatencyBreakdown``."""
    contributions = [
        {
            "key": c.key,
            "label": c.label,
            "owner": c.owner,
            "owner_kind": str(getattr(c.owner_kind, "value", c.owner_kind)),
            "start_time": c.start_time,
            "ms": _ms(c.duration_secs),
        }
        for c in breakdown.contributions
    ]
    slowest = max(contributions, key=lambda c: c["ms"] or 0.0, default=None)
    payload: dict[str, Any] = {
        "measured_from": (
            str(getattr(breakdown.measured_from, "value", breakdown.measured_from))
            if breakdown.measured_from is not None else None
        ),
        "total_ms": _ms(breakdown.total_secs),
        "user_turn_ms": _ms(breakdown.user_turn_secs),
        "user_turn_start_time": breakdown.user_turn_start_time,
        "contributions": contributions,
        "slowest": slowest["key"] if slowest else None,
        "ttfb": [
            {"processor": t.processor, "model": t.model, "ms": _ms(t.duration_secs)}
            for t in breakdown.ttfb
        ],
    }
    if breakdown.text_aggregation is not None:
        payload["text_aggregation_ms"] = _ms(breakdown.text_aggregation.duration_secs)
    if breakdown.function_calls:
        payload["function_calls"] = [
            {
                "name": getattr(f, "function_name", None) or getattr(f, "name", None),
                "ms": _ms(getattr(f, "duration_secs", None)),
            }
            for f in breakdown.function_calls
        ]
    return payload


class TurnLatencyBreakdownObserver(UserBotLatencyObserver):
    """``UserBotLatencyObserver`` that writes each breakdown onto the call.

    ``recorder`` is anything with ``add_event(kind, **data)`` (the session
    recorder). Pass ``min_contribution_secs=0`` to list every hop; the
    default rolls sub-threshold hops into one pipeline entry, which is what
    Conversation Review wants.
    """

    def __init__(self, recorder, **kwargs) -> None:
        super().__init__(**kwargs)
        self._recorder = recorder
        self.add_event_handler("on_latency_breakdown", self._on_breakdown)

    async def _on_breakdown(self, _observer, breakdown: LatencyBreakdown) -> None:
        try:
            payload = breakdown_event(breakdown)
            self._recorder.add_event(EVENT_KIND, **payload)
            logger.info(
                "turn_latency_breakdown total=%sms from=%s slowest=%s",
                payload["total_ms"], payload["measured_from"], payload["slowest"],
            )
        except Exception:  # noqa: BLE001 — telemetry must never affect a call
            logger.debug("latency breakdown not recorded", exc_info=True)


__all__ = ["EVENT_KIND", "TurnLatencyBreakdownObserver", "breakdown_event"]
