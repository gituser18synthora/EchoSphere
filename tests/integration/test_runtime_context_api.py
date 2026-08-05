"""Runtime context API + Testing Studio simulator — multi-domain, live DB.

Proves the redesign end-to-end over the real app: a healthcare tenant and a
real-estate tenant configure their own fields with zero code changes,
payloads validate with types preserved, sensitive values only ever leave the
server masked, records match calls by phone, cross-tenant access is
impossible, and the simulator returns the full decision trace (context
sources, rendered prompt, intent, tools, response) for final and partial
transcripts.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
_PHONE = "+9198888" + str(int(_SUFFIX, 16) % 100000).zfill(5)

HEALTHCARE_FIELDS = [
    {"key": "patient_name", "label": "Patient name", "type": "string", "required": True},
    {"key": "patient_id", "label": "MRN", "type": "string", "sensitive": True},
    {"key": "appointment", "label": "Appointment", "type": "object"},
    {"key": "allergies", "type": "array"},
    {"key": "insurance_verified", "type": "boolean"},
    {"key": "copay_due", "type": "number"},
]

HEALTHCARE_PAYLOAD = {
    "patient_name": "Meera Iyer",
    "patient_id": "MRN-778812",
    "appointment": {"date": "2026-08-11", "time": "10:15",
                    "doctor": "Dr. Kulkarni", "department": "Cardiology"},
    "allergies": ["penicillin"],
    "insurance_verified": True,
    "copay_due": 350.5,
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


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


def _data(response, expect=200):
    assert response.status_code == expect, response.text
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


@pytest.fixture(scope="module")
def tenant_admin():
    return _bearer("priya.sharma@meridianhealth.com")


def _enabled_language() -> str:
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import SupportedLanguage

    session = get_sessionmaker()()
    try:
        codes = session.scalars(
            select(SupportedLanguage.code).where(SupportedLanguage.enabled.is_(True))
        ).all()
        for preferred in ("hi-IN", "en-IN", "en-US"):
            if preferred in codes:
                return preferred
        assert codes
        return codes[0]
    finally:
        session.close()


def _make_bot(client, tenant_admin, name: str) -> str:
    created = _data(client.post(f"{API}/bots", headers=tenant_admin, json={
        "name": name, "useCase": "support",
        "languages": [_enabled_language()],
    }), expect=201)
    return created["id"]


def _teardown_bot(bot_id: str) -> None:
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        conn.execute(sa_text(
            "DELETE pv FROM prompt_versions pv JOIN prompts p ON pv.prompt_id = p.id "
            "WHERE p.bot_id = :b"), {"b": bot_id})
        for table in ("runtime_context_schemas", "runtime_context_records",
                      "prompts", "voice_bot_readiness", "bot_languages",
                      "voice_bot_settings", "workflows", "intents",
                      "api_connections", "audit_logs"):
            column = "entity_id" if table == "audit_logs" else "bot_id"
            try:
                conn.execute(sa_text(f"DELETE FROM {table} WHERE {column} = :b"),
                             {"b": bot_id})
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        conn.execute(sa_text("DELETE FROM voice_bots WHERE id = :b"), {"b": bot_id})


@pytest.fixture(scope="module")
def health_bot(client, tenant_admin):
    bot_id = _make_bot(client, tenant_admin, f"Clinic Bot {_SUFFIX}")
    # Pin the bot to the keyless mock LLM so the simulator is deterministic.
    from shared.db.mysql import get_sessionmaker
    from shared.ids import new_id
    from shared.models import VoiceBot, VoiceBotSetting

    session = get_sessionmaker()()
    try:
        tenant_id = session.get(VoiceBot, bot_id).tenant_id
        session.add(VoiceBotSetting(id=new_id("vbs"), bot_id=bot_id,
                                    tenant_id=tenant_id,
                                    llm_provider="mock", llm_model="mock-1"))
        session.commit()
    finally:
        session.close()
    yield {"id": bot_id}
    _teardown_bot(bot_id)


@pytest.fixture(scope="module")
def estate_bot(client, tenant_admin):
    bot_id = _make_bot(client, tenant_admin, f"Realty Bot {_SUFFIX}")
    yield {"id": bot_id}
    _teardown_bot(bot_id)


class TestSchemaConfiguration:
    def test_defaults_before_configuration(self, client, tenant_admin, health_bot):
        data = _data(client.get(
            f"{API}/bots/{health_bot['id']}/runtime-context", headers=tenant_admin,
        ))
        assert data["configured"] is False
        assert data["sourceMode"] == "manual"
        assert data["fields"] == []

    def test_healthcare_schema_saved_without_code_changes(
        self, client, tenant_admin, health_bot,
    ):
        data = _data(client.put(
            f"{API}/bots/{health_bot['id']}/runtime-context", headers=tenant_admin,
            json={
                "name": "Patient details",
                "sourceMode": "manual",
                "fields": HEALTHCARE_FIELDS,
                "allowAdditional": True,
                "testPayload": HEALTHCARE_PAYLOAD,
                "missingValuePolicy": "Say the front desk will confirm it.",
                "domainPolicy": "generic",
            },
        ))
        assert data["configured"] is True
        assert data["fields"][0]["key"] == "patient_name"
        # The stored test payload keeps its JSON types.
        assert data["testPayload"]["copay_due"] == 350.5
        assert data["testPayload"]["insurance_verified"] is True
        assert data["testPayload"]["appointment"]["doctor"] == "Dr. Kulkarni"

    def test_real_estate_schema_same_platform(self, client, tenant_admin, estate_bot):
        data = _data(client.put(
            f"{API}/bots/{estate_bot['id']}/runtime-context", headers=tenant_admin,
            json={
                "name": "Lead details",
                "sourceMode": "manual",
                "fields": [
                    {"key": "lead_name", "type": "string"},
                    {"key": "budget_max", "type": "number"},
                    {"key": "properties", "type": "array"},
                    {"key": "site_visit", "type": "object"},
                ],
                "testPayload": {
                    "lead_name": "Arjun", "budget_max": 7500000,
                    "properties": [{"id": "P-12", "locality": "Baner", "bhk": 2}],
                    "site_visit": {"scheduled": False},
                },
                "domainPolicy": "generic",
            },
        ))
        assert data["configured"] is True
        assert data["testPayload"]["properties"][0]["locality"] == "Baner"

    def test_invalid_schema_rejected(self, client, tenant_admin, health_bot):
        response = client.put(
            f"{API}/bots/{health_bot['id']}/runtime-context", headers=tenant_admin,
            json={"fields": [{"key": "bad key!", "type": "money"}],
                  "sourceMode": "teleport"},
        )
        assert response.status_code == 422

    def test_api_source_requires_owned_connection(self, client, tenant_admin, health_bot):
        response = client.put(
            f"{API}/bots/{health_bot['id']}/runtime-context", headers=tenant_admin,
            json={"sourceMode": "api", "apiConnectionId": "api_does_not_exist",
                  "fields": []},
        )
        assert response.status_code == 422


class TestPayloadValidationEndpoint:
    def test_valid_payload_with_sources_and_masking(self, client, tenant_admin, health_bot):
        data = _data(client.post(
            f"{API}/bots/{health_bot['id']}/runtime-context/validate",
            headers=tenant_admin, json={"payload": HEALTHCARE_PAYLOAD},
        ))
        assert data["valid"] is True
        values = {v["key"]: v for v in data["effective"]}
        assert values["patient_name"]["value"] == "Meera Iyer"
        assert values["patient_name"]["source"] in ("test", "api")
        # Sensitive field masked in the API response.
        assert values["patient_id"]["sensitive"] is True
        assert values["patient_id"]["value"] != "MRN-778812"
        # Nested object round-trips typed.
        assert values["appointment"]["value"]["doctor"] == "Dr. Kulkarni"
        assert "Meera Iyer" in data["promptSection"]
        assert "MRN-778812" not in data["promptSection"]

    def test_invalid_types_reported(self, client, tenant_admin, health_bot):
        data = _data(client.post(
            f"{API}/bots/{health_bot['id']}/runtime-context/validate",
            headers=tenant_admin,
            json={"payload": {"patient_name": 42, "copay_due": "lots",
                              "insurance_verified": "yes"}},
        ))
        assert data["valid"] is False
        bad = {e["field"] for e in data["errors"]}
        assert bad >= {"patient_name", "copay_due", "insurance_verified"}

    def test_missing_required_reported(self, client, tenant_admin, health_bot):
        data = _data(client.post(
            f"{API}/bots/{health_bot['id']}/runtime-context/validate",
            headers=tenant_admin, json={"payload": {"copay_due": 10}},
        ))
        assert data["valid"] is False
        assert any(e["field"] == "patient_name" for e in data["errors"])


class TestRecords:
    def test_create_lookup_and_masking(self, client, tenant_admin, health_bot):
        record = _data(client.post(
            f"{API}/bots/{health_bot['id']}/runtime-context/records",
            headers=tenant_admin,
            json={"customerRef": f"PAT-{_SUFFIX}", "phone": _PHONE,
                  "data": HEALTHCARE_PAYLOAD},
        ), expect=201)
        assert record["id"].startswith("rcr_")
        assert record["phoneMasked"] == "XXXXXX" + _PHONE[-4:]
        assert record["data"]["patient_id"] != "MRN-778812"
        assert record["data"]["appointment"]["date"] == "2026-08-11"

        listing = _data(client.get(
            f"{API}/bots/{health_bot['id']}/runtime-context/records",
            headers=tenant_admin,
        ))
        assert any(r["id"] == record["id"] for r in listing)

    def test_record_rejects_schema_violations(self, client, tenant_admin, health_bot):
        response = client.post(
            f"{API}/bots/{health_bot['id']}/runtime-context/records",
            headers=tenant_admin,
            json={"customerRef": "BAD", "data": {"patient_name": None,
                                                 "copay_due": "x"}},
        )
        assert response.status_code == 422

    async def test_runtime_loader_matches_phone(self, health_bot):
        """The voice worker's loader resolves the record by caller number."""
        from shared.runtime_context import load_runtime_context

        ctx = await load_runtime_context(
            _tenant_of(health_bot["id"]), health_bot["id"], phone=_PHONE,
        )
        assert ctx is not None
        assert ctx.get("patient_name") == "Meera Iyer"
        assert ctx.values["patient_name"].source == "record"
        assert ctx.record_id and ctx.record_id.startswith("rcr_")
        # Sensitive masked before it ever reaches a call.
        assert ctx.get("patient_id") != "MRN-778812"

    async def test_runtime_loader_uses_test_payload_without_record(self, estate_bot):
        from shared.runtime_context import load_runtime_context

        ctx = await load_runtime_context(
            _tenant_of(estate_bot["id"]), estate_bot["id"], phone="+911234567890",
        )
        assert ctx is not None
        assert ctx.get("lead_name") == "Arjun"
        assert ctx.values["lead_name"].source == "test"


def _tenant_of(bot_id: str) -> str:
    from shared.db.mysql import get_sessionmaker
    from shared.models import VoiceBot

    session = get_sessionmaker()()
    try:
        return session.get(VoiceBot, bot_id).tenant_id
    finally:
        session.close()


class TestIsolation:
    def test_cross_tenant_access_denied(self, client, health_bot):
        from sqlalchemy import select

        from shared.db.mysql import get_sessionmaker
        from shared.models import Role, User

        session = get_sessionmaker()()
        try:
            primary_tenant = _tenant_of(health_bot["id"])
            other = session.execute(
                select(User).join(Role, User.role_id == Role.id).where(
                    Role.code.in_(("tenant_admin", "tenant_owner")),
                    User.tenant_id.isnot(None),
                    User.tenant_id != primary_tenant,
                )
            ).scalars().first()
        finally:
            session.close()
        if other is None:
            pytest.skip("no second tenant admin in the dev DB")
        headers = {"Authorization": "Bearer " + create_access_token(
            user_id=other.id, role=other.role.code, tenant_id=other.tenant_id,
        )}
        assert client.get(
            f"{API}/bots/{health_bot['id']}/runtime-context", headers=headers,
        ).status_code in (403, 404)
        assert client.get(
            f"{API}/bots/{health_bot['id']}/runtime-context/records", headers=headers,
        ).status_code in (403, 404)

    async def test_loader_scopes_record_ids(self, health_bot):
        """A record id dereferenced under the wrong tenant does not exist."""
        from sqlalchemy import select

        from shared.db.mysql import get_sessionmaker
        from shared.models import RuntimeContextRecord
        from shared.runtime_context import load_runtime_context

        session = get_sessionmaker()()
        try:
            record_id = session.scalars(
                select(RuntimeContextRecord.id).where(
                    RuntimeContextRecord.bot_id == health_bot["id"],
                )
            ).first()
        finally:
            session.close()
        assert record_id
        ctx = await load_runtime_context(
            "tn_someone_else", health_bot["id"], record_id=record_id,
        )
        assert ctx is None or ctx.record_id is None


class TestSimulator:
    def test_partial_transcript_never_becomes_a_turn(self, client, tenant_admin, health_bot):
        trace = _data(client.post(
            f"{API}/bots/{health_bot['id']}/testing/simulate", headers=tenant_admin,
            json={"message": "mera appointment", "isFinal": False},
        ))
        assert trace["heldForFinal"] is True
        assert trace["response"] is None
        assert trace["route"] is None

    def test_final_turn_full_trace_with_manual_context(self, client, tenant_admin, health_bot):
        trace = _data(client.post(
            f"{API}/bots/{health_bot['id']}/testing/simulate", headers=tenant_admin,
            json={
                "message": "मेरी अपॉइंटमेंट कब है?",
                "contextSource": "manual",
                "contextPayload": HEALTHCARE_PAYLOAD,
                "language": "hi-IN",
                "isFinal": True,
            },
        ))
        assert trace["finalTranscript"] == "मेरी अपॉइंटमेंट कब है?"
        values = {v["key"]: v for v in trace["runtimeContext"]["values"]}
        assert values["patient_name"]["value"] == "Meera Iyer"
        assert values["patient_name"]["source"] == "test"
        assert values["patient_id"]["value"] != "MRN-778812"
        assert "Meera Iyer" in trace["renderedPrompt"]
        assert "MRN-778812" not in trace["renderedPrompt"]
        assert trace["voiceIdentity"] == {"name": "Shubh", "gender": "male"}
        assert "grammatically male forms" in trace["renderedPrompt"]
        assert trace["intent"] is not None
        assert trace["response"]
        assert trace["latencyMs"] >= 0
        assert trace["provider"] == "mock"

    def test_hangup_is_deterministic_in_simulator(self, client, tenant_admin, health_bot):
        trace = _data(client.post(
            f"{API}/bots/{health_bot['id']}/testing/simulate", headers=tenant_admin,
            json={"message": "call band kar do", "isFinal": True},
        ))
        assert trace["route"] == "call_control"
        assert trace["action"] == "hangup"

    def test_dnc_is_deterministic_in_simulator(self, client, tenant_admin, health_bot):
        trace = _data(client.post(
            f"{API}/bots/{health_bot['id']}/testing/simulate", headers=tenant_admin,
            json={"message": "dobara call mat karna", "isFinal": True},
        ))
        assert trace["route"] == "call_control"
        assert trace["action"] == "do_not_call"
        assert trace["disposition"] == "do_not_call"

    def test_mocked_tool_verification_in_collections_domain(
        self, client, tenant_admin, health_bot,
    ):
        """already_paid + mock check_payment_status → verified, not assumed."""
        bot_id = health_bot["id"]
        # Flip the bot into the collections domain with loan-style fields —
        # configuration only, same platform code.
        _data(client.put(
            f"{API}/bots/{bot_id}/runtime-context", headers=tenant_admin,
            json={
                "sourceMode": "manual",
                "fields": [
                    {"key": "customer_name", "type": "string"},
                    {"key": "overdue_amount", "type": "number"},
                    {"key": "payment_status", "type": "string"},
                ],
                "testPayload": {"customer_name": "Rahul Sharma",
                                "overdue_amount": 12500,
                                "payment_status": "unpaid"},
                "domainPolicy": "collections",
            },
        ))
        # Configure the tool (an API connection) and the intent → tool
        # binding — tenant configuration, not platform code.
        _data(client.post(
            f"{API}/api-connections", headers=tenant_admin,
            json={"name": "check_payment_status", "method": "POST",
                  "url": "https://lms.example/payments/status",
                  "botId": bot_id,
                  "responseMapping": [
                      {"source": "payment_status", "target": "payment_status"},
                  ]},
        ), expect=201)
        _data(client.post(
            f"{API}/bots/{bot_id}/intents", headers=tenant_admin,
            json={"name": "already_paid",
                  "description": "claims payment already made",
                  "samples": ["maine payment kar diya", "already paid",
                              "payment ho gayi hai"],
                  "route": "tool:check_payment_status"},
        ), expect=201)
        trace = _data(client.post(
            f"{API}/bots/{bot_id}/testing/simulate", headers=tenant_admin,
            json={
                "message": "maine payment kar diya",
                "language": "hi-IN",
                "isFinal": True,
                "mockToolResults": {
                    "check_payment_status": {"payment_status": "completed"},
                },
            },
        ))
        assert trace["signal"] == "already_paid" or (
            trace["intent"] and trace["intent"].get("intent") == "already_paid"
        )
        tool = trace["tool"]
        assert tool is not None
        assert tool["mocked"] is True and tool["ok"] is True
        # The mock's verified status reached the policy: paid is a TOOL
        # verdict, never an assumption from the claim.
        assert trace["paymentVerification"] == "completed"
        assert "payment_status: completed" in trace["renderedPrompt"] \
            or "payment IS confirmed" in trace["renderedPrompt"]
