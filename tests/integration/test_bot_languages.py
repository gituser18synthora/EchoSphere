"""VoiceBot ↔ language mapping: DB-driven catalog, create/update validation
(order-preserving dedupe, unknown/disabled rejection) and persistence of
bot_languages rows.

Runs against the live app + local databases. Every bot and master-data row
created here is removed in teardown — demo data is never mutated.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]

_bot_ids: list[str] = []


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client
    for bot_id in _bot_ids:
        _purge_bot(bot_id)


def _bearer(email: str) -> dict:
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import User

    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code,
                                    tenant_id=user.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


def _data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


def _purge_bot(bot_id: str) -> None:
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        for table in ("voice_bot_settings", "voice_bot_readiness",
                      "bot_languages", "workflows", "prompts"):
            conn.execute(sa_text(f"DELETE FROM `{table}` WHERE bot_id = :b"), {"b": bot_id})
        conn.execute(sa_text("DELETE FROM audit_logs WHERE entity_id = :b"), {"b": bot_id})
        conn.execute(sa_text("DELETE FROM voice_bots WHERE id = :b"), {"b": bot_id})


@pytest.fixture(scope="module")
def super_admin():
    return _bearer("admin@aurexion.com")


@pytest.fixture(scope="module")
def tenant_admin():
    return _bearer("priya.sharma@meridianhealth.com")  # tenant_admin of tn-001


@pytest.fixture(scope="module")
def disabled_language(client, super_admin):
    """A platform language that exists but is disabled."""
    created = _data(client.post(f"{API}/master/languages", headers=super_admin, json={
        "code": f"q{_SUFFIX[:4]}-XX", "name": "Disabledish", "direction": "ltr",
    }))
    _data(client.post(f"{API}/master/languages/{created['id']}/status",
                      headers=super_admin, json={"status": "inactive"}))
    yield created["code"]

    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        conn.execute(sa_text("DELETE FROM audit_logs WHERE entity_id = :i"),
                     {"i": created["id"]})
        conn.execute(sa_text("DELETE FROM supported_languages WHERE id = :i"),
                     {"i": created["id"]})


def _create_bot(client, headers, languages):
    response = client.post(f"{API}/bots", headers=headers, json={
        "name": f"Lang Test Bot {_SUFFIX} {len(_bot_ids)}",
        "useCase": "languages", "languages": languages,
    })
    if response.status_code == 201:
        _bot_ids.append(response.json()["data"]["id"])
    return response


# ── Catalog (form option source) ──────────────────────────────────────────────


def test_catalog_serves_only_enabled_languages(client, tenant_admin, disabled_language):
    languages = _data(client.get(f"{API}/languages", headers=tenant_admin))
    assert languages, "language catalog must be seeded"
    assert all(l["enabled"] for l in languages)
    assert disabled_language not in {l["code"] for l in languages}


# ── Create ────────────────────────────────────────────────────────────────────


def test_create_persists_selected_languages(client, tenant_admin):
    created = _data(_create_bot(client, tenant_admin, ["en-US", "hi-IN"]))
    fetched = _data(client.get(f"{API}/bots/{created['id']}", headers=tenant_admin))
    assert sorted(fetched["languages"]) == ["en-US", "hi-IN"]


def test_create_dedupes_duplicate_codes(client, tenant_admin):
    created = _data(_create_bot(client, tenant_admin, ["en-US", "en-US", "hi-IN", "hi-IN"]))
    fetched = _data(client.get(f"{API}/bots/{created['id']}", headers=tenant_admin))
    assert sorted(fetched["languages"]) == ["en-US", "hi-IN"]


def test_create_rejects_unknown_language(client, tenant_admin):
    response = _create_bot(client, tenant_admin, ["en-US", "zz-ZZ"])
    assert response.status_code == 422
    assert "zz-ZZ" in response.json()["message"]


def test_create_rejects_disabled_language(client, tenant_admin, disabled_language):
    response = _create_bot(client, tenant_admin, [disabled_language])
    assert response.status_code == 422
    assert disabled_language in response.json()["message"]


def test_create_rejects_empty_selection(client, tenant_admin):
    response = _create_bot(client, tenant_admin, [])
    assert response.status_code == 422


# ── Update ────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def update_bot(client, tenant_admin):
    created = _data(_create_bot(client, tenant_admin, ["en-US"]))
    return created["id"]


def test_update_replaces_languages(client, tenant_admin, update_bot):
    updated = _data(client.patch(f"{API}/bots/{update_bot}", headers=tenant_admin,
                                 json={"languages": ["hi-IN", "ta-IN"]}))
    assert sorted(updated["languages"]) == ["hi-IN", "ta-IN"]
    # persisted, not just serialized from the request
    fetched = _data(client.get(f"{API}/bots/{update_bot}", headers=tenant_admin))
    assert sorted(fetched["languages"]) == ["hi-IN", "ta-IN"]


def test_update_add_and_remove(client, tenant_admin, update_bot):
    updated = _data(client.patch(f"{API}/bots/{update_bot}", headers=tenant_admin,
                                 json={"languages": ["hi-IN", "en-US"]}))
    assert sorted(updated["languages"]) == ["en-US", "hi-IN"]


def test_update_rejects_unknown_and_keeps_previous(client, tenant_admin, update_bot):
    response = client.patch(f"{API}/bots/{update_bot}", headers=tenant_admin,
                            json={"languages": ["en-US", "nope-XX"]})
    assert response.status_code == 422
    fetched = _data(client.get(f"{API}/bots/{update_bot}", headers=tenant_admin))
    assert sorted(fetched["languages"]) == ["en-US", "hi-IN"]


def test_update_rejects_disabled(client, tenant_admin, update_bot, disabled_language):
    response = client.patch(f"{API}/bots/{update_bot}", headers=tenant_admin,
                            json={"languages": [disabled_language, "en-US"]})
    assert response.status_code == 422


def test_update_rejects_empty(client, tenant_admin, update_bot):
    response = client.patch(f"{API}/bots/{update_bot}", headers=tenant_admin,
                            json={"languages": []})
    assert response.status_code == 422
