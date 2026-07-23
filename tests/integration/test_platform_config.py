"""Platform Configuration master-data hardening:

- plan currency whitelist + persistence
- non-negative numeric validation (create + update)
- AI-profile provider/model catalog validation (incl. embedding capability)
- voices list filters (provider/gender/status, combined, empty)
- languages/voices sort-order display and stable ordering

Same live-app harness as test_admin_features; every created row is uniquely
suffixed and removed in the module teardown.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
_created: list[tuple[str, str]] = []  # (table, id)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        for table, row_id in reversed(_created):
            conn.execute(sa_text(f"DELETE FROM `{table}` WHERE id = :id"), {"id": row_id})


@pytest.fixture(scope="module")
def super_admin():
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import User

    session = get_sessionmaker()()
    try:
        user = session.execute(
            select(User).where(User.email == "admin@aurexion.com")
        ).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code, tenant_id=user.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


def _data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


def _field_errors(response) -> dict[str, str]:
    body = response.json()
    return {e["field"]: e["message"] for e in body.get("errors", []) if isinstance(e, dict)}


def _track(table: str, row_id: str) -> None:
    _created.append((table, row_id))


# ── Plans: currency ──────────────────────────────────────────────────────────


class TestPlanCurrency:
    def test_currency_saved_and_returned(self, client, super_admin):
        created = _data(client.post(f"{API}/master/plans", headers=super_admin, json={
            "code": f"cur_{_SUFFIX}", "name": "Currency plan", "priceMonthly": 999,
            "currency": "INR",
        }))
        _track("plans", created["id"])
        assert created["currency"] == "INR"
        listed = _data(client.get(f"{API}/master/plans?search=cur_{_SUFFIX}", headers=super_admin))
        assert listed[0]["currency"] == "INR"

    def test_lowercase_currency_normalized(self, client, super_admin):
        created = _data(client.post(f"{API}/master/plans", headers=super_admin, json={
            "code": f"curl_{_SUFFIX}", "name": "Lower currency plan", "currency": "aed",
        }))
        _track("plans", created["id"])
        assert created["currency"] == "AED"

    def test_unsupported_currency_rejected(self, client, super_admin):
        response = client.post(f"{API}/master/plans", headers=super_admin, json={
            "code": f"curbad_{_SUFFIX}", "name": "Bad currency plan", "currency": "BTC",
        })
        assert response.status_code == 422
        assert "currency" in _field_errors(response)

    def test_currency_editable_on_update(self, client, super_admin):
        created = _data(client.post(f"{API}/master/plans", headers=super_admin, json={
            "code": f"curup_{_SUFFIX}", "name": "Update currency plan",
        }))
        _track("plans", created["id"])
        assert created["currency"] == "USD"  # model default = existing app configuration
        updated = _data(client.patch(f"{API}/master/plans/{created['id']}", headers=super_admin,
                                     json={"currency": "GBP"}))
        assert updated["currency"] == "GBP"


# ── Non-negative validation ──────────────────────────────────────────────────


class TestNonNegativeValidation:
    @pytest.mark.parametrize("field", ["priceMonthly", "botLimit", "seatsIncluded",
                                       "monthlyTokenLimit", "recordingRetentionDays", "sortOrder"])
    def test_negative_plan_fields_rejected_on_create(self, client, super_admin, field):
        response = client.post(f"{API}/master/plans", headers=super_admin, json={
            "code": f"neg_{field[:6]}_{_SUFFIX}", "name": "Neg plan", field: -1,
        })
        assert response.status_code == 422
        assert field in _field_errors(response)

    def test_negative_rejected_on_update(self, client, super_admin):
        created = _data(client.post(f"{API}/master/plans", headers=super_admin, json={
            "code": f"negup_{_SUFFIX}", "name": "Neg update plan", "botLimit": 5,
        }))
        _track("plans", created["id"])
        response = client.patch(f"{API}/master/plans/{created['id']}", headers=super_admin,
                                json={"botLimit": -2})
        assert response.status_code == 422
        assert "botLimit" in _field_errors(response)
        # Value not silently saved.
        listed = _data(client.get(f"{API}/master/plans?search=negup_{_SUFFIX}", headers=super_admin))
        assert listed[0]["botLimit"] == 5

    def test_negative_language_sort_order_rejected(self, client, super_admin):
        response = client.post(f"{API}/master/languages", headers=super_admin, json={
            "code": f"x{_SUFFIX[:4]}-XX", "name": "Neg lang", "sortOrder": -3,
        })
        assert response.status_code == 422
        assert "sortOrder" in _field_errors(response)

    def test_negative_voice_sort_order_rejected(self, client, super_admin):
        response = client.post(f"{API}/master/voices", headers=super_admin, json={
            "name": f"Neg voice {_SUFFIX}", "sortOrder": -1,
        })
        assert response.status_code == 422
        assert "sortOrder" in _field_errors(response)

    def test_non_numeric_rejected(self, client, super_admin):
        response = client.post(f"{API}/master/plans", headers=super_admin, json={
            "code": f"nan_{_SUFFIX}", "name": "NaN plan", "botLimit": "many",
        })
        assert response.status_code == 422
        assert "botLimit" in _field_errors(response)

    def test_zero_is_accepted(self, client, super_admin):
        created = _data(client.post(f"{API}/master/plans", headers=super_admin, json={
            "code": f"zero_{_SUFFIX}", "name": "Zero plan", "monthlyCallLimit": 0, "priceMonthly": 0,
        }))
        _track("plans", created["id"])
        assert created["monthlyCallLimit"] == 0


# ── AI profile provider/model validation ────────────────────────────────────


class TestProviderModelValidation:
    def _create(self, client, headers, **overrides):
        payload = {"code": f"aip_{uuid.uuid4().hex[:8]}", "name": "PM profile", **overrides}
        return client.post(f"{API}/master/ai-profiles", headers=headers, json=payload)

    def test_valid_llm_pair_accepted(self, client, super_admin):
        response = self._create(client, super_admin, llmProvider="openai", llmModel="gpt-4o-mini")
        created = _data(response)
        _track("ai_config_profiles", created["id"])
        assert created["llmProvider"] == "openai" and created["llmModel"] == "gpt-4o-mini"

    def test_model_not_of_provider_rejected(self, client, super_admin):
        response = self._create(client, super_admin, llmProvider="openai", llmModel="bulbul:v3")
        assert response.status_code == 422
        assert "does not belong" in _field_errors(response)["llmModel"]

    def test_model_without_provider_rejected(self, client, super_admin):
        response = self._create(client, super_admin, ttsModel="bulbul:v3")
        assert response.status_code == 422
        assert "provider" in _field_errors(response)["ttsModel"].lower()

    def test_unknown_provider_rejected(self, client, super_admin):
        response = self._create(client, super_admin, sttProvider="acme-voice")
        assert response.status_code == 422
        assert "sttProvider" in _field_errors(response)

    def test_provider_without_models_rejects_any_model(self, client, super_admin):
        # deepgram is a configured STT provider but has no catalog models yet.
        response = self._create(client, super_admin, sttProvider="deepgram", sttModel="nova-2")
        assert response.status_code == 422
        assert "no configured models" in _field_errors(response)["sttModel"]

    def test_embedding_pair_validated(self, client, super_admin):
        ok_response = self._create(
            client, super_admin,
            embeddingProvider="openai", embeddingModel="text-embedding-3-small",
        )
        created = _data(ok_response)
        _track("ai_config_profiles", created["id"])
        assert created["embeddingModel"] == "text-embedding-3-small"

        bad = self._create(client, super_admin,
                           embeddingProvider="openai", embeddingModel="ada-005")
        assert bad.status_code == 422
        assert "embeddingModel" in _field_errors(bad)

    def test_stt_tts_pairs_validated(self, client, super_admin):
        ok_response = self._create(
            client, super_admin,
            sttProvider="sarvam", sttModel="saaras:v3",
            ttsProvider="elevenlabs", ttsModel="eleven_flash_v2_5",
        )
        created = _data(ok_response)
        _track("ai_config_profiles", created["id"])

        bad = self._create(client, super_admin, ttsProvider="sarvam", ttsModel="eleven_flash_v2_5")
        assert bad.status_code == 422

    def test_update_validates_against_stored_provider(self, client, super_admin):
        created = _data(self._create(client, super_admin,
                                     llmProvider="openai", llmModel="gpt-4o-mini"))
        _track("ai_config_profiles", created["id"])
        # Changing only the model must be validated against the stored provider.
        bad = client.patch(f"{API}/master/ai-profiles/{created['id']}", headers=super_admin,
                           json={"llmModel": "saaras:v3"})
        assert bad.status_code == 422
        good = _data(client.patch(f"{API}/master/ai-profiles/{created['id']}", headers=super_admin,
                                  json={"llmModel": "gpt-4o"}))
        assert good["llmModel"] == "gpt-4o"

    def test_empty_string_clears_model(self, client, super_admin):
        created = _data(self._create(client, super_admin,
                                     llmProvider="openai", llmModel="gpt-4o-mini"))
        _track("ai_config_profiles", created["id"])
        cleared = _data(client.patch(f"{API}/master/ai-profiles/{created['id']}", headers=super_admin,
                                     json={"llmModel": ""}))
        assert not cleared["llmModel"]

    def test_catalog_serves_embedding_capability(self, client, super_admin):
        catalog = _data(client.get(f"{API}/providers/catalog?capability=embedding", headers=super_admin))
        codes = [p["code"] for p in catalog["embedding"]]
        assert "openai" in codes
        models = _data(client.get(f"{API}/providers/embedding/openai/models", headers=super_admin))
        assert "text-embedding-3-small" in [m["code"] for m in models]


# ── Voices: filters + sort order ─────────────────────────────────────────────


@pytest.fixture(scope="class")
def voice_fixture_rows(request, client, super_admin):
    rows = []
    specs = [
        {"name": f"Filter A {_SUFFIX}", "provider": "sarvam", "gender": "female", "sortOrder": 2, "locale": "hi-IN"},
        {"name": f"Filter B {_SUFFIX}", "provider": "sarvam", "gender": "male", "sortOrder": 1, "locale": "en-IN"},
        {"name": f"Filter C {_SUFFIX}", "provider": "elevenlabs", "gender": "female", "sortOrder": 0, "locale": "en-IN"},
    ]
    for payload in specs:
        created = _data(client.post(f"{API}/master/voices", headers=super_admin, json=payload))
        _track("voice_profiles", created["id"])
        rows.append(created)
    # One deactivated row for the status filter.
    client.post(f"{API}/master/voices/{rows[1]['id']}/status", headers=super_admin,
                json={"status": "inactive"})
    request.cls.rows = rows
    return rows


@pytest.mark.usefixtures("voice_fixture_rows")
class TestVoiceFilters:
    rows: list

    def _search(self, client, headers, **params):
        query = "&".join(f"{k}={v}" for k, v in {"search": f"Filter", **params}.items())
        return _data(client.get(f"{API}/master/voices?{query}&pageSize=50", headers=headers))

    def _mine(self, items):
        return [v for v in items if str(v["name"]).endswith(_SUFFIX)]

    def test_provider_filter(self, client, super_admin):
        mine = self._mine(self._search(client, super_admin, provider="sarvam"))
        assert {v["provider"] for v in mine} == {"sarvam"} and len(mine) == 2

    def test_filters_combine(self, client, super_admin):
        mine = self._mine(self._search(client, super_admin, provider="sarvam", gender="female"))
        assert len(mine) == 1 and mine[0]["gender"] == "female"

    def test_status_filter(self, client, super_admin):
        mine = self._mine(self._search(client, super_admin, status="inactive"))
        assert len(mine) == 1 and mine[0]["name"].startswith("Filter B")

    def test_language_filter(self, client, super_admin):
        mine = self._mine(self._search(client, super_admin, language="hi-IN"))
        assert len(mine) == 1 and mine[0]["locale"] == "hi-IN"

    def test_no_match_returns_empty(self, client, super_admin):
        mine = self._mine(self._search(client, super_admin, provider="sarvam", gender="neutral"))
        assert mine == []

    def test_invalid_status_value_rejected(self, client, super_admin):
        response = client.get(f"{API}/master/voices?status=bogus", headers=super_admin)
        assert response.status_code == 422

    def test_voice_serializer_exposes_sort_order(self, client, super_admin):
        mine = self._mine(self._search(client, super_admin))
        assert all(isinstance(v["sortOrder"], int) for v in mine)

    def test_ordered_by_sort_order_then_name(self, client, super_admin):
        mine = self._mine(self._search(client, super_admin))
        orders = [v["sortOrder"] for v in mine]
        assert orders == sorted(orders)

    def test_unknown_voice_provider_rejected(self, client, super_admin):
        response = client.post(f"{API}/master/voices", headers=super_admin, json={
            "name": f"Bad provider voice {_SUFFIX}", "provider": "acme-tts",
        })
        assert response.status_code == 422
        assert "provider" in _field_errors(response)


# ── Languages: ordering ──────────────────────────────────────────────────────


class TestLanguageOrdering:
    def test_default_order_is_sort_order_with_stable_secondary(self, client, super_admin):
        items = _data(client.get(f"{API}/master/languages?pageSize=100", headers=super_admin))
        # Secondary name sort uses the DB's case-insensitive collation.
        keys = [(lang["sortOrder"], str(lang["name"]).casefold()) for lang in items]
        assert keys == sorted(keys)

    def test_language_sort_order_editable(self, client, super_admin):
        created = _data(client.post(f"{API}/master/languages", headers=super_admin, json={
            "code": f"z{_SUFFIX[:4]}-ZZ", "name": f"Sortable Lang {_SUFFIX}", "sortOrder": 7,
        }))
        _track("supported_languages", created["id"])
        assert created["sortOrder"] == 7
        updated = _data(client.patch(f"{API}/master/languages/{created['id']}", headers=super_admin,
                                     json={"sortOrder": 3}))
        assert updated["sortOrder"] == 3


# ── Countries + Data Regions ───────────────────────────────────────────


class TestAsiaCountryCatalog:
    def test_active_country_catalog_is_database_driven_and_asia_only(self, client, super_admin):
        countries = _data(client.get(
            f"{API}/master/countries?includeInactive=false&pageSize=100&sortBy=name&sortDir=asc",
            headers=super_admin,
        ))
        by_iso2 = {country["iso2"]: country for country in countries}

        assert len(countries) >= 49
        assert by_iso2["IN"]["name"] == "India"
        assert by_iso2["IN"]["iso3"] == "IND"
        assert by_iso2["NP"]["name"] == "Nepal"
        assert by_iso2["NP"]["iso3"] == "NPL"
        assert all(isinstance(country["id"], int) for country in countries)
        assert all(country["region"] == "Asia" for country in countries)

    def test_data_region_uses_numeric_country_id_and_server_canonicalizes_region(
        self, client, super_admin
    ):
        countries = _data(client.get(
            f"{API}/master/countries?search=Nepal&pageSize=10", headers=super_admin
        ))
        nepal = next(country for country in countries if country["iso2"] == "NP")
        created = _data(client.post(f"{API}/master/data-regions", headers=super_admin, json={
            "code": f"np_{_SUFFIX}",
            "name": f"Nepal Region {_SUFFIX}",
            "countryId": nepal["id"],
            # A stale/tampered client value must never override the country master.
            "region": "Europe",
        }))
        _track("data_regions", created["id"])

        assert created["countryId"] == nepal["id"]
        assert created["countryCode"] == "np"
        assert created["countryIso2"] == "NP"
        assert created["countryIso3"] == "NPL"
        assert created["country"] == "Nepal"
        assert created["region"] == "Asia"

    def test_unknown_country_cannot_be_saved_in_data_region(self, client, super_admin):
        response = client.post(f"{API}/master/data-regions", headers=super_admin, json={
            "code": f"unknown_{_SUFFIX}",
            "name": f"Unknown Country Region {_SUFFIX}",
            "countryId": 999999,
        })

        assert response.status_code == 422
        assert "countryId" in _field_errors(response)
