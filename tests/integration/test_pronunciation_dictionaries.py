"""Pronunciation dictionary API — Sarvam proxy with local tenant metadata.

The provider is faked at the `_provider_call` seam, so these tests pin:
- validation (locales, empty rows, word limits, duplicate names) happens
  BEFORE any provider call;
- create uploads the wire JSON (platform or-IN mapped to Sarvam od-IN) and
  stores the returned dictionary id with the tenant-facing name;
- get merges live provider mappings back onto platform locale codes;
- update/delete hit the provider with the stored dict_id and keep local
  metadata consistent;
- rows are tenant-scoped.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import backend.routers.pronunciation as pronunciation
from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_sessionmaker
from shared.models import PronunciationDictionary, User

pytestmark = pytest.mark.integration

API = "/api/v1"
TENANT = "tn-001"


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def tenant_admin():
    db = get_sessionmaker()()
    try:
        user = db.scalar(select(User).where(User.email == "priya.sharma@meridianhealth.com"))
        token = create_access_token(
            user_id=user.id, role=user.role.code, tenant_id=user.tenant_id
        )
        return {"Authorization": f"Bearer {token}"}
    finally:
        db.close()


@pytest.fixture()
def fake_provider(monkeypatch):
    """Record provider calls and answer with canned Sarvam responses."""
    calls: list[dict] = []

    async def _fake(method, url, key, *, files=None):
        entry = {"method": method, "url": url, "files": files}
        calls.append(entry)
        if method == "POST":
            return FakeResponse({"dictionary_id": f"p_{uuid.uuid4().hex[:8]}"})
        if method == "GET":
            return FakeResponse({"pronunciations": {
                "hi-IN": {"EMI": "ई एम आई"},
                "od-IN": {"KYC": "के वाई सी"},
            }})
        if method == "PUT":
            return FakeResponse({"updated_pronunciations": {}})
        return FakeResponse({})

    monkeypatch.setattr(pronunciation, "_provider_call", _fake)
    monkeypatch.setattr(pronunciation, "_sarvam_key", lambda db: "sk-test")
    return calls


@pytest.fixture()
def cleanup():
    yield
    session = get_sessionmaker()()
    session.query(PronunciationDictionary).filter(
        PronunciationDictionary.name.like("Test dict %")
    ).delete(synchronize_session=False)
    session.commit()
    session.close()


def data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


_DEFAULT = object()


def make(client, headers, name, pronunciations=_DEFAULT):
    if pronunciations is _DEFAULT:
        pronunciations = {"hi-IN": {"EMI": "ई एम आई", "CIBIL": "सिबिल"}}
    return client.post(f"{API}/pronunciation-dictionaries", headers=headers, json={
        "name": name, "pronunciations": pronunciations,
    })


class TestValidationBeforeProvider:
    def test_unsupported_language_is_rejected_without_a_provider_call(
        self, client, tenant_admin, fake_provider, cleanup
    ):
        response = make(client, tenant_admin, f"Test dict {uuid.uuid4().hex[:6]}",
                        {"fr-FR": {"Bonjour": "bon zhoor"}})
        assert response.status_code == 422
        assert "fr-FR" in response.json()["message"]
        assert fake_provider == []

    def test_empty_rows_and_blank_words_are_rejected(
        self, client, tenant_admin, fake_provider, cleanup
    ):
        for bad in ({}, {"hi-IN": {}}, {"hi-IN": {"": "x"}}, {"hi-IN": {"x": " "}}):
            response = make(client, tenant_admin, f"Test dict {uuid.uuid4().hex[:6]}", bad)
            assert response.status_code == 422, bad
        assert fake_provider == []

    def test_word_limit_is_enforced_locally(
        self, client, tenant_admin, fake_provider, cleanup
    ):
        too_many = {"hi-IN": {f"word{i}": f"say{i}" for i in range(101)}}
        response = make(client, tenant_admin, f"Test dict {uuid.uuid4().hex[:6]}", too_many)
        assert response.status_code == 422
        assert "100" in response.json()["message"]
        assert fake_provider == []


class TestLifecycle:
    def test_create_uploads_wire_json_and_stores_metadata(
        self, client, tenant_admin, fake_provider, cleanup
    ):
        name = f"Test dict {uuid.uuid4().hex[:6]}"
        created = data(make(client, tenant_admin, name, {
            "hi-IN": {"EMI": "ई एम आई"},
            "or-IN": {"KYC": "के वाई सी"},   # platform Odia code
        }))
        assert created["name"] == name
        assert created["dictId"].startswith("p_")
        assert created["languageWordCounts"] == {"hi-IN": 1, "or-IN": 1}

        # The uploaded multipart file is application/json with Sarvam WIRE
        # locale codes (or-IN → od-IN).
        upload = fake_provider[0]
        assert upload["method"] == "POST"
        filename, body, content_type = upload["files"]["file"]
        assert content_type == "application/json"
        wire = json.loads(body)
        assert wire["pronunciations"]["od-IN"] == {"KYC": "के वाई सी"}
        assert "or-IN" not in wire["pronunciations"]

        listed = data(client.get(f"{API}/pronunciation-dictionaries", headers=tenant_admin))
        assert any(d["id"] == created["id"] for d in listed)

    def test_get_returns_live_mappings_on_platform_locales(
        self, client, tenant_admin, fake_provider, cleanup
    ):
        created = data(make(client, tenant_admin, f"Test dict {uuid.uuid4().hex[:6]}"))
        detail = data(client.get(
            f"{API}/pronunciation-dictionaries/{created['id']}", headers=tenant_admin
        ))
        # Sarvam answered with od-IN; the UI sees the platform code or-IN.
        assert detail["pronunciations"]["or-IN"] == {"KYC": "के वाई सी"}
        assert detail["pronunciations"]["hi-IN"] == {"EMI": "ई एम आई"}

    def test_update_reaches_the_provider_with_the_stored_dict_id(
        self, client, tenant_admin, fake_provider, cleanup
    ):
        created = data(make(client, tenant_admin, f"Test dict {uuid.uuid4().hex[:6]}"))
        updated = data(client.put(
            f"{API}/pronunciation-dictionaries/{created['id']}", headers=tenant_admin,
            json={"name": f"Test dict {uuid.uuid4().hex[:6]}",
                  "pronunciations": {"hi-IN": {"NACH": "नाच"}}},
        ))
        assert updated["languageWordCounts"] == {"hi-IN": 1}
        put_call = next(c for c in fake_provider if c["method"] == "PUT")
        assert f"dict_id={created['dictId']}" in put_call["url"]

    def test_duplicate_names_are_rejected(self, client, tenant_admin, fake_provider, cleanup):
        name = f"Test dict {uuid.uuid4().hex[:6]}"
        data(make(client, tenant_admin, name))
        response = make(client, tenant_admin, name)
        assert response.status_code == 422
        assert "already exists" in response.json()["message"]

    def test_delete_removes_provider_dictionary_and_hides_the_row(
        self, client, tenant_admin, fake_provider, cleanup
    ):
        created = data(make(client, tenant_admin, f"Test dict {uuid.uuid4().hex[:6]}"))
        result = data(client.delete(
            f"{API}/pronunciation-dictionaries/{created['id']}", headers=tenant_admin
        ))
        assert result["deleted"] is True and result["providerDeleted"] is True
        delete_call = next(c for c in fake_provider if c["method"] == "DELETE")
        assert f"dict_id={created['dictId']}" in delete_call["url"]
        assert client.get(
            f"{API}/pronunciation-dictionaries/{created['id']}", headers=tenant_admin
        ).status_code == 404

    def test_rows_are_tenant_scoped(self, client, tenant_admin, fake_provider, cleanup):
        created = data(make(client, tenant_admin, f"Test dict {uuid.uuid4().hex[:6]}"))
        session = get_sessionmaker()()
        try:
            row = session.get(PronunciationDictionary, created["id"])
            assert row.tenant_id == TENANT
        finally:
            session.close()
