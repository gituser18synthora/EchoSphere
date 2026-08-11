"""Shared date-range query filtering for list endpoints and exports.

Timestamps are stored as naive UTC (see ``backend.serializers.iso``, which
labels them ``Z``), so a range bound has to arrive as an *instant* to select the
same rows the client renders. The UI therefore sends the viewer's local day
boundaries already converted to UTC; a bare ``YYYY-MM-DD`` is also accepted for
direct API/curl use and is read as that whole day in UTC.

Both bounds are inclusive: ``dateFrom=2026-08-10&dateTo=2026-08-10`` returns
every call that happened on the 10th.
"""

from datetime import date, datetime, time, timezone

from shared.errors import ApiError

_HINT = "Use YYYY-MM-DD or an ISO-8601 timestamp."


def parse_range_bound(
    value: str | None, *, field: str, end_of_day: bool = False
) -> datetime | None:
    """One range bound as a naive-UTC datetime, or None when unset.

    A date-only value expands to the start of that day, or to its last
    microsecond when ``end_of_day`` — so an upper bound spelled as a date
    covers the whole day instead of cutting it off at midnight.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None

    try:
        parsed_date = date.fromisoformat(raw)
    except ValueError:
        parsed_date = None
    if parsed_date is not None:
        return datetime.combine(parsed_date, time.max if end_of_day else time.min)

    try:
        # `fromisoformat` only learned to read a trailing Z in 3.11; normalising
        # keeps this working on either interpreter.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise ApiError(
            f"'{value}' is not a valid date.",
            422,
            errors=[{"field": field, "message": _HINT}],
        ) from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_date_range(
    date_from: str | None,
    date_to: str | None,
    *,
    from_field: str = "dateFrom",
    to_field: str = "dateTo",
) -> tuple[datetime | None, datetime | None]:
    """Validated ``(start, end)`` bounds, both inclusive and naive UTC."""
    start = parse_range_bound(date_from, field=from_field)
    end = parse_range_bound(date_to, field=to_field, end_of_day=True)
    if start is not None and end is not None and start > end:
        raise ApiError(
            "The start of the date range is after its end.",
            422,
            errors=[{"field": from_field, "message": f"Must be on or before {to_field}."}],
        )
    return start, end
