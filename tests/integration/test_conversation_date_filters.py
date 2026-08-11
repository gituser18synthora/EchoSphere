"""Date-range filtering of the conversation list against the real database.

The Conversations page fetches one page and relies on the API to apply the
range, so the filter has to hold in SQL: a day boundary must include the calls
that happened at its very edges and exclude the ones just outside it, whether
the client spells the bound as a day or as an instant.

All rows are uniquely suffixed and deleted in teardown; existing data is never
mutated.
"""

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy import text as sa_text

from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_engine, get_sessionmaker

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
TENANT = "tn-001"
BOT = "bot-101"

# Two calls on the same day at its first and last second, plus neighbours a day
# either side: enough to catch an exclusive boundary or an off-by-one day.
CALLS = {
    f"cv_range_{_SUFFIX}_before": datetime(2026, 6, 9, 23, 59, 59),
    f"cv_range_{_SUFFIX}_open": datetime(2026, 6, 10, 0, 0, 0),
    f"cv_range_{_SUFFIX}_close": datetime(2026, 6, 10, 23, 59, 59),
    f"cv_range_{_SUFFIX}_next": datetime(2026, 6, 11, 0, 0, 0),
    f"cv_range_{_SUFFIX}_after": datetime(2026, 6, 12, 12, 0, 0),
}
DURATIONS = {name: 30 + index * 15 for index, name in enumerate(CALLS)}
# One call that never connected: duration 0 must serialize a null per-minute
# rate, not a division error and not a misleading 0.
DURATIONS[f"cv_range_{_SUFFIX}_after"] = 0


def _bearer(email: str) -> dict:
    from shared.models import User

    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        return {
            "Authorization": f"Bearer {create_access_token(
                user_id=user.id, role=user.role.code, tenant_id=user.tenant_id
            )}"
        }
    finally:
        session.close()


@pytest.fixture(scope="module")
def client():
    with get_engine().begin() as conn:
        for row_id, started_at in CALLS.items():
            conn.execute(
                sa_text(
                    "INSERT INTO conversation_sessions "
                    "(id, tenant_id, bot_id, channel, started_at, duration_sec, sentiment, "
                    " contained, cost_usd, language, flagged, status, is_deleted, "
                    " created_at, updated_at) "
                    "VALUES (:id, :t, :b, 'voice', :st, :d, 'neutral', 1, 0, 'hi-IN', 0, "
                    " 'completed', 0, NOW(), NOW())"
                ),
                {"id": row_id, "t": TENANT, "b": BOT, "st": started_at, "d": DURATIONS[row_id]},
            )
    with TestClient(app) as test_client:
        yield test_client
    with get_engine().begin() as conn:
        conn.execute(
            sa_text("DELETE FROM conversation_sessions WHERE id LIKE :pattern"),
            {"pattern": f"cv_range_{_SUFFIX}_%"},
        )


@pytest.fixture(scope="module")
def tenant_admin():
    return _bearer("priya.sharma@meridianhealth.com")


def _ids(client, tenant_admin, **params) -> set[str]:
    """Ids of the seeded calls the API returns for these filters."""
    response = client.get(
        f"{API}/conversations",
        params={"pageSize": 200, "search": f"cv_range_{_SUFFIX}", **params},
        headers=tenant_admin,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    return {item["id"] for item in body["data"]}


def test_no_range_returns_every_call(client, tenant_admin):
    assert _ids(client, tenant_admin) == set(CALLS)


def test_single_day_includes_both_edges_and_nothing_else(client, tenant_admin):
    assert _ids(client, tenant_admin, dateFrom="2026-06-10", dateTo="2026-06-10") == {
        f"cv_range_{_SUFFIX}_open",
        f"cv_range_{_SUFFIX}_close",
    }


def test_multi_day_range_is_inclusive_of_the_last_day(client, tenant_admin):
    assert _ids(client, tenant_admin, dateFrom="2026-06-10", dateTo="2026-06-11") == {
        f"cv_range_{_SUFFIX}_open",
        f"cv_range_{_SUFFIX}_close",
        f"cv_range_{_SUFFIX}_next",
    }


def test_open_ended_bounds(client, tenant_admin):
    assert _ids(client, tenant_admin, dateFrom="2026-06-11") == {
        f"cv_range_{_SUFFIX}_next",
        f"cv_range_{_SUFFIX}_after",
    }
    assert _ids(client, tenant_admin, dateTo="2026-06-09") == {f"cv_range_{_SUFFIX}_before"}


def test_instant_bounds_select_the_sending_timezone_s_day(client, tenant_admin):
    """What the UI actually sends: local day boundaries as UTC instants.

    For a viewer at +05:30 the 10th starts at 18:30 UTC on the 9th, so the call
    stored at 2026-06-09 23:59:59 UTC belongs to their 10th — the row set has to
    follow the offset rather than the calendar date in the string.
    """
    assert _ids(
        client, tenant_admin,
        dateFrom="2026-06-10T00:00:00+05:30",
        dateTo="2026-06-10T23:59:59.999+05:30",
    ) == {
        f"cv_range_{_SUFFIX}_before",
        f"cv_range_{_SUFFIX}_open",
    }


def test_duration_is_served_from_the_stored_call_length(client, tenant_admin):
    """The list's Duration column reads this field; it is never recomputed."""
    response = client.get(
        f"{API}/conversations",
        params={"pageSize": 200, "search": f"cv_range_{_SUFFIX}"},
        headers=tenant_admin,
    )
    rows = {item["id"]: item for item in response.json()["data"]}
    for row_id, expected in DURATIONS.items():
        assert rows[row_id]["durationSec"] == expected
        # startedAt is labelled UTC so the client can place it in local time.
        assert rows[row_id]["startedAt"].endswith("Z")
        # The per-minute rate is derived by the backend from the stored total
        # and this same duration; zero-length calls get null, never a rate.
        per_minute = rows[row_id]["costPerMinuteUsd"]
        if expected == 0:
            assert per_minute is None
        else:
            assert per_minute == pytest.approx(
                rows[row_id]["costUsd"] * 60 / expected
            )


def test_other_filters_still_compose_with_a_range(client, tenant_admin):
    assert _ids(
        client, tenant_admin, dateFrom="2026-06-10", dateTo="2026-06-11", botId=BOT,
    ) == {
        f"cv_range_{_SUFFIX}_open",
        f"cv_range_{_SUFFIX}_close",
        f"cv_range_{_SUFFIX}_next",
    }
    assert _ids(
        client, tenant_admin, dateFrom="2026-06-10", dateTo="2026-06-11", flagged="true",
    ) == set()


@pytest.mark.parametrize(
    "params",
    [
        {"dateFrom": "not-a-date"},
        {"dateTo": "10/06/2026"},
        {"dateFrom": "2026-06-11", "dateTo": "2026-06-10"},
    ],
)
def test_unusable_ranges_are_rejected_rather_than_ignored(client, tenant_admin, params):
    response = client.get(f"{API}/conversations", params=params, headers=tenant_admin)
    assert response.status_code == 422, response.text
    assert response.json()["success"] is False


def test_export_applies_the_same_range(client, tenant_admin):
    response = client.get(
        f"{API}/exports/conversations",
        params={"format": "csv", "search": f"cv_range_{_SUFFIX}",
                "dateFrom": "2026-06-10", "dateTo": "2026-06-10"},
        headers=tenant_admin,
    )
    assert response.status_code == 200, response.text
    body = response.content.decode("utf-8-sig")
    assert f"cv_range_{_SUFFIX}_open" in body
    assert f"cv_range_{_SUFFIX}_close" in body
    assert f"cv_range_{_SUFFIX}_next" not in body

    assert client.get(
        f"{API}/exports/conversations",
        params={"format": "csv", "dateFrom": "nonsense"},
        headers=tenant_admin,
    ).status_code == 422
