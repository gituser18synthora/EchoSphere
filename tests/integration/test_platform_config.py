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
        # assemblyai has no catalog models; the governance matrix keeps it
        # inactive, so activate it just for this scenario and restore after.
        from sqlalchemy import select as sa_select

        from shared.db.mysql import get_sessionmaker
        from shared.models import ProviderDef

        session = get_sessionmaker()()
        try:
            provider = session.execute(sa_select(ProviderDef).where(
                ProviderDef.kind == "stt", ProviderDef.code == "assemblyai",
            )).scalar_one_or_none()
            if provider is None:
                pytest.skip("assemblyai STT provider not seeded")
            previous_status = provider.status
            provider.status = "active"
            session.commit()
            try:
                response = self._create(
                    client, super_admin, sttProvider="assemblyai", sttModel="best"
                )
                assert response.status_code == 422
                assert "no configured models" in _field_errors(response)["sttModel"]
            finally:
                provider.status = previous_status
                session.commit()
        finally:
            session.close()

    def test_deepgram_flux_is_a_selectable_stt_pair(self, client, super_admin):
        # Deepgram Flux is an active, governed STT option (models seeded by
        # migration e9a1c3b5d7f9); a wrong model still fails closed.
        ok_response = self._create(
            client, super_admin,
            sttProvider="deepgram", sttModel="flux-general-multi",
        )
        created = _data(ok_response)
        _track("ai_config_profiles", created["id"])
        assert created["sttModel"] == "flux-general-multi"
        bad = self._create(
            client, super_admin, sttProvider="deepgram", sttModel="saaras:v3",
        )
        assert bad.status_code == 422

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

    def test_status_first_then_sort_order(self, client, super_admin):
        # Platform Config lists are status-first: active voices come first
        # (sort_order ascending), then the deactivated one — even though its
        # sort_order (1) sits between the active ones (0 and 2).
        mine = self._mine(self._search(client, super_admin))
        statuses = [v["status"] for v in mine]
        first_inactive = next((i for i, s in enumerate(statuses) if s != "active"), len(statuses))
        assert all(s != "active" for s in statuses[first_inactive:]), statuses
        active = [v["sortOrder"] for v in mine if v["status"] == "active"]
        inactive = [v["sortOrder"] for v in mine if v["status"] != "active"]
        assert active == sorted(active)
        assert inactive == sorted(inactive)
        # Concretely: Filter C (0, active), Filter A (2, active), Filter B (1, inactive).
        assert [str(v["name"]).split()[1] for v in mine] == ["C", "A", "B"]

    def test_unknown_voice_provider_rejected(self, client, super_admin):
        response = client.post(f"{API}/master/voices", headers=super_admin, json={
            "name": f"Bad provider voice {_SUFFIX}", "provider": "acme-tts",
        })
        assert response.status_code == 422
        assert "provider" in _field_errors(response)


# ── Languages: ordering ──────────────────────────────────────────────────────


class TestLanguageOrdering:
    def test_enabled_first_then_sort_order_with_stable_secondary(self, client, super_admin):
        # Languages use the `enabled` flag as their lifecycle column: enabled
        # languages are returned before disabled ones, and within each group the
        # (sort_order, name) order holds (secondary name sort uses the DB's
        # case-insensitive collation).
        items = _data(client.get(f"{API}/master/languages?pageSize=200", headers=super_admin))
        priorities = [0 if lang["enabled"] else 1 for lang in items]
        assert priorities == sorted(priorities), "all enabled languages must precede disabled ones"
        for enabled in (True, False):
            keys = [(lang["sortOrder"], str(lang["name"]).casefold())
                    for lang in items if lang["enabled"] is enabled]
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
    def test_country_catalog_is_database_driven_and_asia_only(self, client, super_admin):
        # Full catalog (admins may deactivate countries in the shared dev DB,
        # so the seed is verified over includeInactive=true).
        countries = _data(client.get(
            f"{API}/master/countries?includeInactive=true&pageSize=100&sortBy=name&sortDir=asc",
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

        # The active listing is a strict subset of the catalog.
        active = _data(client.get(
            f"{API}/master/countries?includeInactive=false&pageSize=100",
            headers=super_admin,
        ))
        assert {c["iso2"] for c in active} <= set(by_iso2)
        assert all(c["status"] == "active" for c in active)

    def test_data_region_uses_numeric_country_id_and_server_canonicalizes_region(
        self, client, super_admin
    ):
        # Data regions only accept ACTIVE countries; pick one that is active
        # right now instead of assuming a specific seed row still is.
        active = _data(client.get(
            f"{API}/master/countries?includeInactive=false&pageSize=100",
            headers=super_admin,
        ))
        if not active:
            pytest.skip("no active countries in the shared dev DB")
        country = active[0]
        created = _data(client.post(f"{API}/master/data-regions", headers=super_admin, json={
            "code": f"cc_{_SUFFIX}",
            "name": f"Canonical Region {_SUFFIX}",
            "countryId": country["id"],
            # A stale/tampered client value must never override the country master.
            "region": "Europe",
        }))
        _track("data_regions", created["id"])

        assert created["countryId"] == country["id"]
        assert created["countryCode"] == country["iso2"].lower()
        assert created["countryIso2"] == country["iso2"]
        assert created["countryIso3"] == country["iso3"]
        assert created["country"] == country["name"]
        assert created["region"] == "Asia"

    def test_unknown_country_cannot_be_saved_in_data_region(self, client, super_admin):
        response = client.post(f"{API}/master/data-regions", headers=super_admin, json={
            "code": f"unknown_{_SUFFIX}",
            "name": f"Unknown Country Region {_SUFFIX}",
            "countryId": 999999,
        })

        assert response.status_code == 422
        assert "countryId" in _field_errors(response)


# ── Providers: status-first ordering ─────────────────────────────────────────


class TestProviderOrdering:
    """Providers list active rows before inactive/archived ones; within a status
    group they order by sort_order ascending with a stable name/id fallback.
    Deactivating a provider drops it to the bottom on the next fetch (no manual
    refresh needed by the UI, which refetches after a status change);
    reactivating lifts it back into the active group. The ordering holds for
    search/filtered responses too."""

    def _make(self, client, headers, code, name, sort_order):
        created = _data(client.post(f"{API}/master/providers", headers=headers, json={
            "kind": "llm", "code": code, "name": name, "sortOrder": sort_order,
        }))
        _track("provider_defs", created["id"])
        return created

    def _codes_in_order(self, client, headers, tag):
        # A tag-scoped search exercises the "search results preserve ordering"
        # path and isolates this test's rows from the seeded catalog.
        items = _data(client.get(
            f"{API}/master/providers?kind=llm&pageSize=200&search={tag}", headers=headers))
        return [p["code"] for p in items if tag in str(p["code"])]

    def test_active_first_sort_order_and_deactivation_moves_to_bottom(self, client, super_admin):
        tag = f"pmove{_SUFFIX}"
        a = self._make(client, super_admin, f"{tag}_a", f"P A {_SUFFIX}", 5)
        b = self._make(client, super_admin, f"{tag}_b", f"P B {_SUFFIX}", 1)
        c = self._make(client, super_admin, f"{tag}_c", f"P C {_SUFFIX}", 3)
        # All active → sort_order ascending: b(1), c(3), a(5).
        assert self._codes_in_order(client, super_admin, tag) == [b["code"], c["code"], a["code"]]

        # Deactivate the lowest-sort-order active row → it drops to the bottom.
        response = client.post(f"{API}/master/providers/{b['id']}/status",
                               headers=super_admin, json={"status": "inactive"})
        assert response.status_code == 200
        assert self._codes_in_order(client, super_admin, tag) == [c["code"], a["code"], b["code"]]

        # Reactivate → b returns to its sort_order position in the active group.
        client.post(f"{API}/master/providers/{b['id']}/status",
                    headers=super_admin, json={"status": "active"})
        assert self._codes_in_order(client, super_admin, tag) == [b["code"], c["code"], a["code"]]

    def test_equal_sort_order_uses_deterministic_name_fallback(self, client, super_admin):
        tag = f"peq{_SUFFIX}"
        # Created zeta first, but the secondary name sort must order alpha first.
        zeta = self._make(client, super_admin, f"{tag}_z", f"Zeta {_SUFFIX}", 4)
        alpha = self._make(client, super_admin, f"{tag}_a", f"Alpha {_SUFFIX}", 4)
        assert self._codes_in_order(client, super_admin, tag) == [alpha["code"], zeta["code"]]

    def test_archived_rows_sort_after_active(self, client, super_admin):
        tag = f"parch{_SUFFIX}"
        active = self._make(client, super_admin, f"{tag}_a", f"AA {_SUFFIX}", 9)
        archived = self._make(client, super_admin, f"{tag}_b", f"BB {_SUFFIX}", 0)
        client.post(f"{API}/master/providers/{archived['id']}/status",
                    headers=super_admin, json={"status": "archived"})
        # Despite its lower sort_order, the archived row lands after the active one.
        assert self._codes_in_order(client, super_admin, tag) == [active["code"], archived["code"]]

    def test_order_field_is_editable_and_persisted(self, client, super_admin):
        created = self._make(client, super_admin, f"pedit_{_SUFFIX}", f"Editable {_SUFFIX}", 8)
        assert created["sortOrder"] == 8
        updated = _data(client.patch(f"{API}/master/providers/{created['id']}",
                                     headers=super_admin, json={"sortOrder": 2}))
        assert updated["sortOrder"] == 2

    def test_negative_provider_sort_order_rejected(self, client, super_admin):
        response = client.post(f"{API}/master/providers", headers=super_admin, json={
            "kind": "llm", "code": f"pneg_{_SUFFIX}", "name": f"Neg {_SUFFIX}", "sortOrder": -1,
        })
        assert response.status_code == 422
        assert "sortOrder" in _field_errors(response)


# ── Provider models: status-first ordering ───────────────────────────────────


class TestProviderModelOrdering:
    """Provider-model lists follow the same active-first, sort_order-ascending
    contract as providers. Models live under a dedicated test provider so the
    seeded catalog never interferes."""

    @pytest.fixture
    def provider(self, client, super_admin):
        code = f"pmord_{uuid.uuid4().hex[:8]}"
        created = _data(client.post(f"{API}/master/providers", headers=super_admin, json={
            "kind": "llm", "code": code, "name": f"PM Order Provider {code}",
        }))
        _track("provider_defs", created["id"])
        return created

    def _make(self, client, headers, provider, code, sort_order):
        created = _data(client.post(f"{API}/master/provider-models", headers=headers, json={
            "capability": "llm", "providerCode": provider["code"],
            "code": code, "name": code, "sortOrder": sort_order,
        }))
        _track("provider_models", created["id"])
        return created

    def _codes_in_order(self, client, headers, provider):
        items = _data(client.get(
            f"{API}/master/provider-models?capability=llm&provider={provider['code']}&pageSize=200",
            headers=headers))
        return [m["code"] for m in items]

    def test_active_first_and_deactivation_moves_model_to_bottom(self, client, super_admin, provider):
        a = self._make(client, super_admin, provider, "mord-a", 5)
        b = self._make(client, super_admin, provider, "mord-b", 1)
        c = self._make(client, super_admin, provider, "mord-c", 3)
        assert self._codes_in_order(client, super_admin, provider) == ["mord-b", "mord-c", "mord-a"]

        response = client.post(f"{API}/master/provider-models/{b['id']}/status",
                               headers=super_admin, json={"status": "inactive"})
        assert response.status_code == 200
        assert self._codes_in_order(client, super_admin, provider) == ["mord-c", "mord-a", "mord-b"]

        client.post(f"{API}/master/provider-models/{b['id']}/status",
                    headers=super_admin, json={"status": "active"})
        assert self._codes_in_order(client, super_admin, provider) == ["mord-b", "mord-c", "mord-a"]

    def test_provider_model_sort_order_editable(self, client, super_admin, provider):
        created = self._make(client, super_admin, provider, "mord-edit", 7)
        assert created["sortOrder"] == 7
        updated = _data(client.patch(f"{API}/master/provider-models/{created['id']}",
                                     headers=super_admin, json={"sortOrder": 1}))
        assert updated["sortOrder"] == 1


# ── Status-first ordering across the whole Platform Configuration module ──────

# Simple `status`-based master types creatable with just code + name + sortOrder.
_STATUS_TYPES = [
    ("industries", "industries"),
    ("plans", "plans"),
    ("ai-profiles", "ai_config_profiles"),
]


@pytest.mark.parametrize("mtype, table", _STATUS_TYPES)
class TestStatusFirstOrderingAcrossTypes:
    """The active-first / sort_order-ascending contract holds for *every* master
    type, not only providers — verified generically across a representative set
    of `status`-based sections. Search-scoped so seeded rows never interfere."""

    def _make(self, client, headers, mtype, table, code, sort_order):
        created = _data(client.post(f"{API}/master/{mtype}", headers=headers,
                                    json={"code": code, "name": f"SF {code}", "sortOrder": sort_order}))
        _track(table, created["id"])
        return created

    def _rows(self, client, headers, mtype, tag):
        items = _data(client.get(f"{API}/master/{mtype}?pageSize=200&search={tag}", headers=headers))
        mine = [i for i in items if tag in str(i["code"])]
        return [(i["code"], i["status"], i["sortOrder"]) for i in mine]

    def test_full_lifecycle_ordering(self, client, super_admin, mtype, table):
        tag = f"sf{mtype.replace('-', '')[:3]}{_SUFFIX}"
        a = self._make(client, super_admin, mtype, table, f"{tag}_a", 5)
        b = self._make(client, super_admin, mtype, table, f"{tag}_b", 1)
        c = self._make(client, super_admin, mtype, table, f"{tag}_c", 3)

        # All active → sort_order ascending.
        assert [r[0] for r in self._rows(client, super_admin, mtype, tag)] == \
            [b["code"], c["code"], a["code"]]

        # Deactivate b → drops below every active record despite its low sort_order.
        assert client.post(f"{API}/master/{mtype}/{b['id']}/status", headers=super_admin,
                           json={"status": "inactive"}).status_code == 200
        rows = self._rows(client, super_admin, mtype, tag)
        assert [r[0] for r in rows] == [c["code"], a["code"], b["code"]]
        assert [r[1] for r in rows] == ["active", "active", "inactive"]

        # Archive c too → both unavailable rows sit at the bottom, ordered by
        # sort_order ascending among themselves (b=1 before c=3).
        client.post(f"{API}/master/{mtype}/{c['id']}/status", headers=super_admin,
                    json={"status": "archived"})
        rows = self._rows(client, super_admin, mtype, tag)
        assert [r[0] for r in rows] == [a["code"], b["code"], c["code"]]
        assert [r[1] for r in rows] == ["active", "inactive", "archived"]

        # Reactivate b → back into the active group at its sort_order position.
        client.post(f"{API}/master/{mtype}/{b['id']}/status", headers=super_admin,
                    json={"status": "active"})
        rows = self._rows(client, super_admin, mtype, tag)
        assert [r[0] for r in rows] == [b["code"], a["code"], c["code"]]

    def test_equal_sort_order_deterministic_name_fallback(self, client, super_admin, mtype, table):
        tag = f"eq{mtype.replace('-', '')[:3]}{_SUFFIX}"
        zeta = self._make(client, super_admin, mtype, table, f"{tag}_zeta", 4)
        alpha = self._make(client, super_admin, mtype, table, f"{tag}_alpha", 4)
        # Same sort_order → the name (SF <code>) breaks the tie deterministically.
        assert [r[0] for r in self._rows(client, super_admin, mtype, tag)] == \
            [alpha["code"], zeta["code"]]


class TestLanguageStatusOrdering:
    """Languages use the `enabled` flag as their lifecycle column: disabling one
    drops it below every enabled language; re-enabling lifts it back."""

    def _make(self, client, headers, code, sort_order):
        created = _data(client.post(f"{API}/master/languages", headers=headers,
            json={"code": code, "name": f"SFLang{_SUFFIX}-{code}", "sortOrder": sort_order}))
        _track("supported_languages", created["id"])
        return created

    def _rows(self, client, headers):
        items = _data(client.get(
            f"{API}/master/languages?pageSize=200&search=SFLang{_SUFFIX}", headers=headers))
        mine = [l for l in items if f"SFLang{_SUFFIX}" in str(l["name"])]
        return [(l["code"], l["enabled"], l["sortOrder"]) for l in mine]

    def test_disable_moves_to_bottom_enable_restores(self, client, super_admin):
        a = self._make(client, super_admin, f"e{_SUFFIX[:4]}-A", 5)
        b = self._make(client, super_admin, f"e{_SUFFIX[:4]}-B", 1)
        c = self._make(client, super_admin, f"e{_SUFFIX[:4]}-C", 3)
        assert [r[0] for r in self._rows(client, super_admin)] == [b["code"], c["code"], a["code"]]

        # Disable b → enabled=False, drops to the bottom.
        assert client.post(f"{API}/master/languages/{b['id']}/status", headers=super_admin,
                           json={"status": "inactive"}).status_code == 200
        rows = self._rows(client, super_admin)
        assert [r[0] for r in rows] == [c["code"], a["code"], b["code"]]
        assert [r[1] for r in rows] == [True, True, False]

        # Re-enable b → back into the enabled group by sort_order.
        client.post(f"{API}/master/languages/{b['id']}/status", headers=super_admin,
                    json={"status": "active"})
        assert [r[0] for r in self._rows(client, super_admin)] == [b["code"], c["code"], a["code"]]


class TestPaginationDeterministicOrdering:
    """Search, filter and pagination all preserve one global status-first order:
    concatenating the pages reproduces the single-page ordering with no overlap."""

    def test_pages_concatenate_to_global_status_first_order(self, client, super_admin):
        tag = f"pg{_SUFFIX}"
        specs = [(f"{tag}_a", 3, "active"), (f"{tag}_b", 1, "active"),
                 (f"{tag}_c", 2, "inactive"), (f"{tag}_d", 0, "inactive"),
                 (f"{tag}_e", 5, "active")]
        for code, sort_order, status in specs:
            created = _data(client.post(f"{API}/master/industries", headers=super_admin,
                                        json={"code": code, "name": f"PG {code}", "sortOrder": sort_order}))
            _track("industries", created["id"])
            if status != "active":
                client.post(f"{API}/master/industries/{created['id']}/status",
                            headers=super_admin, json={"status": status})

        def page(p, ps):
            items = _data(client.get(
                f"{API}/master/industries?search={tag}&page={p}&pageSize={ps}", headers=super_admin))
            return [i["code"] for i in items if tag in str(i["code"])]

        # Active by sort_order (b=1, a=3, e=5) then inactive by sort_order (d=0, c=2).
        expected = [f"{tag}_b", f"{tag}_a", f"{tag}_e", f"{tag}_d", f"{tag}_c"]
        assert page(1, 100) == expected
        # Paginated (size 2) pages concatenate to the same global order.
        assert page(1, 2) + page(2, 2) + page(3, 2) == expected

    def test_reload_returns_same_order(self, client, super_admin):
        tag = f"rl{_SUFFIX}"
        for code, sort_order in [(f"{tag}_a", 2), (f"{tag}_b", 1)]:
            created = _data(client.post(f"{API}/master/industries", headers=super_admin,
                                        json={"code": code, "name": f"RL {code}", "sortOrder": sort_order}))
            _track("industries", created["id"])
        first = [i["code"] for i in _data(client.get(
            f"{API}/master/industries?search={tag}&pageSize=100", headers=super_admin)) if tag in str(i["code"])]
        second = [i["code"] for i in _data(client.get(
            f"{API}/master/industries?search={tag}&pageSize=100", headers=super_admin)) if tag in str(i["code"])]
        assert first == second == [f"{tag}_b", f"{tag}_a"]

    def test_page_meta_matches_request_and_survives_status_change(self, client, super_admin):
        """Refetching the *same* page after a status change returns that page's
        slice of the re-sorted global order, with meta echoing the requested
        page — the contract the frontend relies on to stay on page N."""
        tag = f"pm{_SUFFIX}"
        ids = {}
        for i in range(5):
            created = _data(client.post(f"{API}/master/industries", headers=super_admin,
                                        json={"code": f"{tag}_{i}", "name": f"PM {tag}_{i}", "sortOrder": i}))
            _track("industries", created["id"])
            ids[f"{tag}_{i}"] = created["id"]

        def fetch(p):
            response = client.get(
                f"{API}/master/industries?search={tag}&page={p}&pageSize=2", headers=super_admin)
            body = response.json()
            assert body.get("success") is True, body
            return [r["code"] for r in body["data"]], body["meta"]

        codes, meta = fetch(2)
        assert meta == {"page": 2, "pageSize": 2, "total": 5, "totalPages": 3}
        assert codes == [f"{tag}_2", f"{tag}_3"]

        # Deactivating the first page-2 record drops it to the global bottom;
        # page 2 refetched with the same number shows the shifted slice.
        client.post(f"{API}/master/industries/{ids[f'{tag}_2']}/status",
                    headers=super_admin, json={"status": "inactive"})
        codes, meta = fetch(2)
        assert meta == {"page": 2, "pageSize": 2, "total": 5, "totalPages": 3}
        assert codes == [f"{tag}_3", f"{tag}_4"]
        # And the deactivated record is now the global tail.
        assert fetch(1)[0] == [f"{tag}_0", f"{tag}_1"]
        assert fetch(3)[0] == [f"{tag}_2"]


class TestDataRegionOrdering:
    """Data Regions require a country FK, so they get a dedicated ordering test
    (the generic parametrized class only covers code+name+sortOrder types)."""

    @pytest.fixture
    def country_id(self, client, super_admin):
        countries = _data(client.get(
            f"{API}/master/countries?search=India&pageSize=10", headers=super_admin))
        return next(c["id"] for c in countries if c["iso2"] == "IN")

    def _make(self, client, headers, country_id, code, sort_order):
        created = _data(client.post(f"{API}/master/data-regions", headers=headers, json={
            "code": code, "name": f"SF {code}", "countryId": country_id, "sortOrder": sort_order,
        }))
        _track("data_regions", created["id"])
        return created

    def _rows(self, client, headers, tag):
        items = _data(client.get(
            f"{API}/master/data-regions?pageSize=200&search={tag}", headers=headers))
        mine = [i for i in items if tag in str(i["code"])]
        return [(i["code"], i["status"]) for i in mine]

    def test_status_first_and_deactivation_moves_to_bottom(self, client, super_admin, country_id):
        tag = f"drord{_SUFFIX}"
        a = self._make(client, super_admin, country_id, f"{tag}-a", 5)
        b = self._make(client, super_admin, country_id, f"{tag}-b", 1)
        c = self._make(client, super_admin, country_id, f"{tag}-c", 3)
        assert [r[0] for r in self._rows(client, super_admin, tag)] == \
            [b["code"], c["code"], a["code"]]

        client.post(f"{API}/master/data-regions/{b['id']}/status", headers=super_admin,
                    json={"status": "inactive"})
        rows = self._rows(client, super_admin, tag)
        assert [r[0] for r in rows] == [c["code"], a["code"], b["code"]]
        assert [r[1] for r in rows] == ["active", "active", "inactive"]

        client.post(f"{API}/master/data-regions/{b['id']}/status", headers=super_admin,
                    json={"status": "active"})
        assert [r[0] for r in self._rows(client, super_admin, tag)] == \
            [b["code"], c["code"], a["code"]]


@pytest.mark.parametrize("mtype", ["industries", "data-regions", "plans", "ai-profiles"])
def test_serializer_exposes_integer_sort_order(client, super_admin, mtype):
    """Data-flow guard: the four config sections whose column switched from
    Updated to Order must return a real integer `sortOrder` in the API response
    (DB sort_order → model → serializer → response) — never a timestamp."""
    items = _data(client.get(f"{API}/master/{mtype}?pageSize=5", headers=super_admin))
    assert items, f"expected seeded {mtype} rows to verify sortOrder"
    assert all(isinstance(i["sortOrder"], int) for i in items)
    assert all("sortOrder" in i for i in items)


# ── Provider pricing: STT/TTS costing configuration ─────────────────────────


class TestProviderPricingValidation:
    """Super-Admin pricing CRUD: catalog compatibility, selling price,
    currency/rate rules, capability/provider filters and authorization."""

    def _create(self, client, headers, **overrides):
        from datetime import datetime, timedelta

        offset = overrides.pop("_offset_minutes", 0)
        payload = {
            "providerCode": "elevenlabs", "capability": "tts",
            "modelCode": "eleven_flash_v2_5", "component": "characters",
            "unit": "per_1m_characters", "unitPrice": "50",
            "currencyCode": "USD",
            "effectiveFrom": (
                datetime.utcnow() - timedelta(days=30, minutes=offset)
            ).isoformat(timespec="seconds"),
        }
        payload.update(overrides)
        return client.post(f"{API}/master/provider-pricing", headers=headers, json=payload)

    def test_model_must_belong_to_provider_and_capability(self, client, super_admin):
        response = self._create(client, super_admin, providerCode="sarvam",
                                modelCode="nova-3", capability="stt",
                                component="audio_seconds", unit="per_hour")
        assert response.status_code == 422
        assert "not a configured stt model" in _field_errors(response)["modelCode"]

    def test_governance_inactive_provider_and_model_are_priceable(self, client, super_admin):
        # deepgram + nova-3 are catalogued but inactive (STT is Sarvam-only);
        # their pricing must still be configurable ahead of rollout.
        response = self._create(client, super_admin, providerCode="deepgram",
                                capability="stt", modelCode="nova-3",
                                component="audio_seconds", unit="per_minute",
                                unitPrice="0.0058")
        assert response.status_code == 201, response.json()
        created = _data(response)
        _track("provider_pricing", created["id"])
        assert created["unit"] == "per_minute"

    def test_per_1m_characters_unit_accepted_and_returned(self, client, super_admin):
        response = self._create(client, super_admin, _offset_minutes=1)
        assert response.status_code == 201, response.json()
        created = _data(response)
        _track("provider_pricing", created["id"])
        assert created["unit"] == "per_1m_characters"
        assert created["sellingPrice"] is None

    def test_unknown_unit_rejected(self, client, super_admin):
        response = self._create(client, super_admin, unit="per_fortnight")
        assert response.status_code == 422
        assert "unit" in _field_errors(response)

    def test_selling_price_must_be_positive(self, client, super_admin):
        response = self._create(client, super_admin, sellingPrice="-1")
        assert response.status_code == 422
        assert "sellingPrice" in _field_errors(response)

    def test_selling_price_saved_updated_and_cleared(self, client, super_admin):
        created = _data(self._create(client, super_admin, _offset_minutes=2,
                                     sellingPrice="75"))
        _track("provider_pricing", created["id"])
        assert created["sellingPrice"] is not None

        updated = _data(client.patch(
            f"{API}/master/provider-pricing/{created['id']}", headers=super_admin,
            json={"sellingPrice": "80"},
        ))
        assert float(updated["sellingPrice"]) == 80.0

        cleared = _data(client.patch(
            f"{API}/master/provider-pricing/{created['id']}", headers=super_admin,
            json={"sellingPrice": ""},
        ))
        assert cleared["sellingPrice"] is None

    def test_non_usd_currency_requires_configured_exchange_rate(self, client, super_admin):
        # AED is an active currency with no configured USD→AED rate.
        response = self._create(client, super_admin, currencyCode="AED")
        assert response.status_code == 422
        assert "exchange rate" in _field_errors(response)["currencyCode"]

    def test_capability_and_provider_filters(self, client, super_admin):
        stt = _data(client.get(
            f"{API}/master/provider-pricing?capability=stt&pageSize=100",
            headers=super_admin))
        assert stt and all(r["capability"] == "stt" for r in stt)
        sarvam_tts = _data(client.get(
            f"{API}/master/provider-pricing?capability=tts&provider=sarvam&pageSize=100",
            headers=super_admin))
        assert sarvam_tts
        assert all(
            r["capability"] == "tts" and r["providerCode"] == "sarvam"
            for r in sarvam_tts
        )

    def test_seeded_stt_tts_prices_present(self, client, super_admin):
        rows = _data(client.get(
            f"{API}/master/provider-pricing?pageSize=200", headers=super_admin))

        def has(provider, cap, model, unit, price, currency):
            return any(
                r["providerCode"] == provider and r["capability"] == cap
                and r["modelCode"] == model and r["unit"] == unit
                and float(r["unitPrice"]) == price and r["currencyCode"] == currency
                for r in rows
            )

        # Official rates verified 2026-07-27 (see base_seed.PROVIDER_PRICING).
        assert has("sarvam", "stt", "saarika:v2.5", "per_hour", 30.0, "INR")
        assert has("sarvam", "stt", "saaras:v3", "per_hour", 30.0, "INR")
        assert has("sarvam", "tts", "bulbul:v3", "per_1k_characters", 3.0, "INR")
        assert has("openai", "stt", "whisper-1", "per_minute", 0.006, "USD")
        assert has("deepgram", "stt", "nova-3", "per_minute", 0.0058, "USD")
        assert has("deepgram", "stt", "nova-2", "per_hour", 0.35, "USD")
        assert has("elevenlabs", "tts", "eleven_flash_v2_5", "per_1k_characters", 0.05, "USD")
        assert has("elevenlabs", "tts", "eleven_turbo_v2_5", "per_1k_characters", 0.05, "USD")

    def test_pricing_mutations_are_super_admin_only(self, client, super_admin):
        from sqlalchemy import select

        from shared.db.mysql import get_sessionmaker
        from shared.ids import new_id
        from shared.models import Role, Tenant, User

        session = get_sessionmaker()()
        try:
            role = session.execute(
                select(Role).where(Role.code == "tenant_admin")
            ).scalar_one()
            tenant = Tenant(id=new_id("tn"), name=f"Pricing T {_SUFFIX}",
                            code=f"prc_{_SUFFIX}",
                            domain=f"pricing-{_SUFFIX}.example.test", status="active")
            session.add(tenant)
            session.flush()
            user = User(id=new_id("usr"), email=f"pricing.{_SUFFIX}@example.test",
                        name="Pricing Tenant Admin", password_hash="x",
                        role_id=role.id, tenant_id=tenant.id, status="active")
            session.add(user)
            session.commit()
            _track("tenants", tenant.id)
            _track("users", user.id)
            headers = {"Authorization": f"Bearer {create_access_token(user_id=user.id, role='tenant_admin', tenant_id=tenant.id)}"}
        finally:
            session.close()

        assert self._create(client, headers).status_code == 403
        listing = client.get(f"{API}/master/provider-pricing", headers=headers)
        assert listing.status_code == 403
