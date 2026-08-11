"""Date-range query bound parsing shared by list endpoints and exports.

The contract these tests pin down: bounds are inclusive, arrive as either a
bare day or an instant, and always come back as naive UTC — which is how
`conversation_sessions.started_at` is stored, so a comparison can never mix an
aware and a naive datetime.
"""

from datetime import datetime

import pytest

from backend.core.date_filters import parse_date_range, parse_range_bound
from shared.errors import ApiError


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_bounds_do_not_filter(value):
    assert parse_range_bound(value, field="dateFrom") is None


def test_bare_day_covers_the_whole_day():
    assert parse_range_bound("2026-08-10", field="dateFrom") == datetime(2026, 8, 10, 0, 0, 0)
    end = parse_range_bound("2026-08-10", field="dateTo", end_of_day=True)
    assert end == datetime(2026, 8, 10, 23, 59, 59, 999999)


def test_instants_are_normalised_to_naive_utc():
    # The UI sends the viewer's local midnight as an instant; IST is +05:30, so
    # the stored-UTC bound is the previous evening — not 00:00 UTC.
    ist_midnight = parse_range_bound("2026-08-10T00:00:00+05:30", field="dateFrom")
    assert ist_midnight == datetime(2026, 8, 9, 18, 30, 0)
    assert ist_midnight.tzinfo is None
    assert parse_range_bound("2026-08-10T04:30:00Z", field="dateFrom") == datetime(2026, 8, 10, 4, 30)
    assert parse_range_bound("2026-08-10T04:30:00", field="dateFrom") == datetime(2026, 8, 10, 4, 30)


def test_a_day_bound_is_not_shortened_by_an_end_of_day_instant():
    """An explicit instant is used as given — only bare days get expanded."""
    assert parse_range_bound(
        "2026-08-10T23:59:59.999000+00:00", field="dateTo", end_of_day=True
    ) == datetime(2026, 8, 10, 23, 59, 59, 999000)


@pytest.mark.parametrize("value", ["yesterday", "10-08-2026", "2026-13-01", "2026-08-10T99:00:00"])
def test_unparseable_values_are_rejected_with_the_field(value):
    with pytest.raises(ApiError) as excinfo:
        parse_range_bound(value, field="dateFrom")
    assert excinfo.value.status_code == 422
    assert excinfo.value.errors[0]["field"] == "dateFrom"


def test_single_day_range_is_valid_and_inclusive():
    start, end = parse_date_range("2026-08-10", "2026-08-10")
    assert start == datetime(2026, 8, 10, 0, 0, 0)
    assert end == datetime(2026, 8, 10, 23, 59, 59, 999999)
    assert start < end


def test_inverted_range_is_rejected():
    with pytest.raises(ApiError) as excinfo:
        parse_date_range("2026-08-11", "2026-08-10")
    assert excinfo.value.status_code == 422
    assert excinfo.value.errors[0]["field"] == "dateFrom"


def test_open_ended_ranges_are_allowed():
    assert parse_date_range("2026-08-10", None)[1] is None
    assert parse_date_range(None, "2026-08-10")[0] is None
    assert parse_date_range(None, None) == (None, None)
