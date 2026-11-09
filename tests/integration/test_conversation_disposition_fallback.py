"""The conversations list, detail, CSV export and txt document show a bot's
structured-summary ``disposition`` field when the runtime stored none
(shared.post_call.dispositions). The runtime's own value still wins, rows
without a final memory stay empty, and other bots are unaffected."""

import csv
import io
import json
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy import text as sa_text

from backend.core.security import create_access_token
from backend.main import app
from backend.reports.operational import CONVERSATIONS_EXPORT
from shared.db.mysql import get_engine, get_sessionmaker

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
TENANT = "tn-001"
BOT = "bot-101"
PREFIX = f"cv_disp_{_SUFFIX}"
LABEL = "Connected – Service Required (Re-dispatch)"
LONG_LABEL = "Connected – Issue Resolved (Either by customer or local Tech.)"

# id suffix → (stored session disposition, memory row (status, structured disposition) or None)
CASES = {
    "structured": (None, ("completed", LABEL)),
    "long": (None, ("completed", LONG_LABEL)),
    "runtime": ("promise_to_pay", ("completed", LABEL)),
    "failed_fallback": (None, ("failed", LABEL)),
    "queued": (None, ("queued", LABEL)),
    "nofield": (None, ("completed", None)),
    "nomemory": (None, None),
}


def _cid(name: str) -> str:
    return f"{PREFIX}_{name}"


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
        for index, (name, (stored, memory)) in enumerate(CASES.items()):
            conn.execute(
                sa_text(
                    "INSERT INTO conversation_sessions "
                    "(id, tenant_id, bot_id, channel, started_at, duration_sec, sentiment, "
                    " contained, cost_usd, language, flagged, status, disposition, is_deleted, "
                    " created_at, updated_at) "
                    "VALUES (:id, :t, :b, 'voice', :st, 60, 'neutral', 1, 0, 'hi-IN', 0, "
                    " 'completed', :disp, 0, NOW(), NOW())"
                ),
                {"id": _cid(name), "t": TENANT, "b": BOT,
                 "st": datetime(2026, 6, 10, 10, index, 0), "disp": stored},
            )
            if memory is None:
                continue
            status, structured = memory
            fields = {"identity_confirmed": "Yes"}
            if structured is not None:
                fields["disposition"] = structured
            conn.execute(
                sa_text(
                    "INSERT INTO conversation_memories "
                    "(id, tenant_id, conversation_id, session_id, bot_id, channel, status, "
                    " attempts, max_attempts, memory, follow_up_required, created_at, updated_at) "
                    "VALUES (:id, :t, :cid, :sid, :b, 'voice', :status, 1, 3, :memory, 0, NOW(), NOW())"
                ),
                {"id": f"cm_{_SUFFIX}_{name}"[:24], "t": TENANT, "cid": _cid(name),
                 "sid": f"vs_{_SUFFIX}_{name}", "b": BOT, "status": status,
                 "memory": json.dumps({"structured_fields": fields,
                                       "structured_field_sources": {"disposition": "workflow"}})},
            )
    with TestClient(app) as test_client:
        yield test_client
    with get_engine().begin() as conn:
        conn.execute(sa_text("DELETE FROM conversation_memories WHERE conversation_id LIKE :p"),
                     {"p": f"{PREFIX}_%"})
        conn.execute(sa_text("DELETE FROM conversation_sessions WHERE id LIKE :p"),
                     {"p": f"{PREFIX}_%"})


@pytest.fixture(scope="module")
def tenant_admin():
    return _bearer("priya.sharma@meridianhealth.com")


EXPECTED = {
    "structured": LABEL,
    "long": LONG_LABEL,
    "runtime": "promise_to_pay",
    "failed_fallback": LABEL,
    "queued": None,
    "nofield": None,
    "nomemory": None,
}


def test_list_shows_the_effective_disposition(client, tenant_admin):
    response = client.get(
        f"{API}/conversations", params={"pageSize": 100, "search": PREFIX}, headers=tenant_admin,
    )
    assert response.status_code == 200, response.text
    by_id = {item["id"]: item["disposition"] for item in response.json()["data"]}
    assert set(by_id) == {_cid(name) for name in CASES}
    for name, expected in EXPECTED.items():
        assert by_id[_cid(name)] == expected, name


@pytest.mark.parametrize("name", list(CASES))
def test_detail_shows_the_effective_disposition(client, tenant_admin, name):
    response = client.get(f"{API}/conversations/{_cid(name)}", headers=tenant_admin)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["disposition"] == EXPECTED[name]


def test_csv_export_appends_the_disposition_column(client, tenant_admin):
    response = client.get(
        f"{API}/exports/conversations", params={"format": "csv", "search": PREFIX},
        headers=tenant_admin,
    )
    assert response.status_code == 200, response.text
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    headers = [column.header for column in CONVERSATIONS_EXPORT.columns]
    assert rows[0] == headers
    assert headers[-1] == "Disposition"  # appended, so positional readers are unaffected
    assert headers.index("Flagged") == 14
    by_id = {row[0]: row[-1] for row in rows[1:]}
    assert set(by_id) == {_cid(name) for name in CASES}
    for name, expected in EXPECTED.items():
        assert by_id[_cid(name)] == (expected or ""), name


def test_txt_document_carries_the_structured_disposition(client, tenant_admin):
    response = client.get(
        f"{API}/conversations/{_cid('structured')}/transcript/export",
        params={"format": "txt"}, headers=tenant_admin,
    )
    assert response.status_code == 200, response.text
    assert _detail_line(response.text, "Disposition") == LABEL
    # A runtime code still renders as words in the header; the structured
    # label only appears where it always did, in the summary's field list.
    runtime = client.get(
        f"{API}/conversations/{_cid('runtime')}/transcript/export",
        params={"format": "txt"}, headers=tenant_admin,
    )
    assert runtime.status_code == 200
    assert _detail_line(runtime.text, "Disposition") == "Promise to pay"
    # No disposition header line at all when neither source has one.
    none = client.get(
        f"{API}/conversations/{_cid('nomemory')}/transcript/export",
        params={"format": "txt"}, headers=tenant_admin,
    )
    assert none.status_code == 200 and _detail_line(none.text, "Disposition") is None


def _detail_line(text: str, label: str) -> str | None:
    """Value of a ``Label : value`` header line of the txt document (None when absent)."""
    for line in text.splitlines():
        if line.startswith(label) and " : " in line:
            return line.split(" : ", 1)[1].strip()
    return None
