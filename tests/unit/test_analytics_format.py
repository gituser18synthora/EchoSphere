"""mm:ss rendering and the talk-minutes ÷ calls average behind the duration KPIs."""

import pytest

from backend.routers.analytics import _avg_call_sec, _mmss


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "—"),
        (0, "00:00"),
        (9, "00:09"),
        (69, "01:09"),
        (59.6, "01:00"),  # rounds to whole seconds before splitting
        (600, "10:00"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3661, "1:01:01"),
        (-5, "00:00"),  # never a negative clock
    ],
)
def test_mmss(seconds, expected):
    assert _mmss(seconds) == expected


def test_avg_call_sec_is_ratio_of_sums():
    assert _avg_call_sec(20.0, 20) == 60
    assert _avg_call_sec(115000.0, 100000) == 69
    assert _avg_call_sec(0.5, 1) == 30


def test_avg_call_sec_has_no_value_without_calls():
    assert _avg_call_sec(0.0, 0) is None
    assert _avg_call_sec(12.0, 0) is None
