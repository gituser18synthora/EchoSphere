"""Real test calls — the Studio's originate routes over the real app + DB.

FreeSWITCH itself is replaced by a scripted ESL client (no socket, no phone
rings): these tests pin the HTTP contract — what the dialog is pre-filled
with, the ``vb_`` mapping of the bot's saved test JSON, the platform-owned
variables, the gates (confirmation, publish state, ESL configured, tenant
isolation) and the job lifecycle (dialing → answered/failed, hangup).
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

import backend.routers.real_calls as rc
import voice_runtime.freeswitch as fs
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]

TEST_PAYLOAD = {
    "customer_name": "Rohan",
    "product_name": "Split AC",
    "rejection_reason": "customer_refused_visit",
    "rejection_reason_label": "the visit was declined when our technician called",
}
FIELDS = [
    {"key": "customer_name", "label": "Name", "type": "string"},
    {"key": "product_name", "label": "Product", "type": "string"},
    {"key": "rejection_reason", "label": "Reason code", "type": "string"},
    {"key": "rejection_reason_label", "label": "Reason", "type": "string"},
]
CONFIGURED_DEFAULTS = (
    '{"sip_route_uri":"sip:kamailio.test:5060",'
    '"sip_h_route-header":"<sip:kamailio.test:5060;lr>",'
    '"domain_name":"dialer.test","kamailioHost":"kamailio.test",'
    '"freeswitchHost":"fs1.test","operatorId":"op1","operatorPrefix":"0",'
    '"origination_caller_id_number":"918047133633","cli":"918047133633"}'
)


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


@pytest.fixture(scope="module")
def other_tenant_admin(bot):
    """A tenant admin of ANOTHER tenant (skips when the dev DB has none)."""
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import Role, User

    session = get_sessionmaker()()
    try:
        other = session.execute(
            select(User).join(Role, User.role_id == Role.id).where(
                Role.code.in_(("tenant_admin", "tenant_owner")),
                User.tenant_id.isnot(None),
                User.tenant_id != bot["tenantId"],
                User.is_deleted.is_(False),
            )
        ).scalars().first()
        if other is None:
            pytest.skip("no second tenant admin in the dev DB")
        return {"Authorization": "Bearer " + create_access_token(
            user_id=other.id, role=other.role.code, tenant_id=other.tenant_id,
        )}
    finally:
        session.close()


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


def _set_status(bot_id: str, status: str) -> None:
    from shared.db.mysql import get_sessionmaker
    from shared.models import VoiceBot

    session = get_sessionmaker()()
    try:
        bot = session.get(VoiceBot, bot_id)
        bot.status = status
        bot.live_version = "v0.1.0" if status == "published" else None
        session.commit()
    finally:
        session.close()


def _teardown_bot(bot_id: str) -> None:
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        conn.execute(sa_text(
            "DELETE pv FROM prompt_versions pv JOIN prompts p ON pv.prompt_id = p.id "
            "WHERE p.bot_id = :b"), {"b": bot_id})
        for table in ("runtime_context_schemas", "prompts", "voice_bot_readiness",
                      "bot_languages", "voice_bot_settings", "workflows", "intents",
                      "channel_configs", "audit_logs"):
            column = "entity_id" if table == "audit_logs" else "bot_id"
            try:
                conn.execute(sa_text(f"DELETE FROM {table} WHERE {column} = :b"),
                             {"b": bot_id})
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        conn.execute(sa_text("DELETE FROM voice_bots WHERE id = :b"), {"b": bot_id})


@pytest.fixture(scope="module")
def bot(client, tenant_admin):
    created = _data(client.post(f"{API}/bots", headers=tenant_admin, json={
        "name": f"Real Call Bot {_SUFFIX}", "useCase": "support",
        "languages": [_enabled_language()],
    }), expect=201)
    bot_id = created["id"]
    _data(client.put(f"{API}/bots/{bot_id}/runtime-context", headers=tenant_admin, json={
        "name": "Customer", "sourceMode": "dialer", "fields": FIELDS,
        "testPayload": TEST_PAYLOAD,
    }))
    _set_status(bot_id, "published")
    yield {"id": bot_id, "tenantId": created["tenantId"]}
    _teardown_bot(bot_id)


class _ScriptedESL:
    """Stands in for ESLClient: records the command, answers per script."""

    instances: list = []
    outcome = (True, "1a2b3c4d-0000-1111-2222-333344445555")
    api_result = "+OK\n"

    def __init__(self, *args, **kwargs) -> None:
        self.commands: list[str] = []
        self.closed = False
        _ScriptedESL.instances.append(self)

    async def originate(self, command_tail, *, job_uuid, timeout):
        self.commands.append(command_tail)
        self.job_uuid, self.timeout = job_uuid, timeout
        outcome = _ScriptedESL.outcome
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def api(self, command):
        self.commands.append(command)
        return _ScriptedESL.api_result

    async def close(self):
        self.closed = True


@pytest.fixture()
def esl(monkeypatch):
    monkeypatch.setattr(fs, "esl_configured", lambda: True)
    monkeypatch.setattr(fs, "ESLClient", _ScriptedESL)
    monkeypatch.setattr(rc.fs, "ESLClient", _ScriptedESL)
    settings = rc.get_settings()
    monkeypatch.setattr(settings, "freeswitch_originate_defaults", CONFIGURED_DEFAULTS)
    _ScriptedESL.instances = []
    _ScriptedESL.outcome = (True, "1a2b3c4d-0000-1111-2222-333344445555")
    _ScriptedESL.api_result = "+OK\n"
    yield _ScriptedESL


def _wait_for_jobs() -> None:
    """The originate runs as a backend task; let it finish."""
    import time

    deadline = time.time() + 5
    while rc._background_jobs and time.time() < deadline:
        time.sleep(0.05)


def _body(**extra):
    base = {
        "phoneNumber": "+91 98765 43210",
        "params": {},
        "customerData": TEST_PAYLOAD,
        "confirm": True,
    }
    base.update(extra)
    return base


class TestDefaults:
    def test_dialog_prefill(self, client, tenant_admin, bot, esl):
        data = _data(client.get(
            f"{API}/bots/{bot['id']}/testing/real-call/defaults", headers=tenant_admin,
        ))
        assert data["botId"] == bot["id"] and data["tenantId"] == bot["tenantId"]
        assert data["eslConfigured"] is True
        assert data["blockers"] == []
        names = [p["name"] for p in data["params"]]
        assert names[:23] == list(rc.MANDATORY_ESL_PARAMS)
        by_name = {p["name"]: p for p in data["params"]}
        assert by_name["sip_route_uri"] == {
            "name": "sip_route_uri", "value": "sip:kamailio.test:5060",
            "mandatory": True, "platform": False, "source": "config",
        }
        assert by_name["tenantId"]["value"] == bot["tenantId"]
        assert by_name["tenantId"]["platform"] is True
        assert by_name["leadId"]["source"] == "generated"
        assert by_name["dialTimeout"]["value"] == "45"
        assert data["dialTemplate"] == "sofia/external/{number}@{domain_name}"
        assert data["application"] == "&lua(voicebot.lua)"
        customer = {c["key"]: c for c in data["customerData"]}
        assert customer["customer_name"]["variable"] == "vb_customer_name"
        assert customer["customer_name"]["value"] == "Rohan"
        assert customer["rejection_reason_label"]["variable"] == "vb_rejection_reason_label"
        assert data["runtimeContext"]["sourceMode"] == "dialer"
        assert [f["key"] for f in data["runtimeContext"]["fields"]] == [f["key"] for f in FIELDS]

    def test_blockers_when_esl_is_not_configured_and_bot_is_draft(
        self, client, tenant_admin, bot, monkeypatch,
    ):
        monkeypatch.setattr(fs, "esl_configured", lambda: False)
        _set_status(bot["id"], "draft")
        try:
            data = _data(client.get(
                f"{API}/bots/{bot['id']}/testing/real-call/defaults", headers=tenant_admin,
            ))
        finally:
            _set_status(bot["id"], "published")
        assert data["eslConfigured"] is False
        assert len(data["blockers"]) == 2
        assert "not configured" in data["blockers"][0]
        assert "not published" in data["blockers"][1]

    def test_other_tenant_cannot_see_the_bot(self, client, other_tenant_admin, bot, esl):
        response = client.get(
            f"{API}/bots/{bot['id']}/testing/real-call/defaults", headers=other_tenant_admin,
        )
        assert response.status_code == 404


class TestPreview:
    def test_effective_variables_and_command(self, client, tenant_admin, bot, esl):
        data = _data(client.post(
            f"{API}/bots/{bot['id']}/testing/real-call/preview", headers=tenant_admin,
            json=_body(
                params={"dialTimeout": 30, "customerNumber": "1111", "voicebotID": "bot_x"},
                customerData={**TEST_PAYLOAD, "vb_extra": "kept", "wo": {"n": 1}},
            ),
        ))
        v = data["variables"]
        assert data["phoneNumber"] == "+919876543210"
        assert data["phoneMasked"] == "XXXXXXXX3210"
        assert v["customerNumber"] == "+919876543210"
        assert v["voicebotID"] == bot["id"]
        assert v["tenantId"] == bot["tenantId"]
        assert v["dialTimeout"] == "30"
        assert v["origination_uuid"] == data["originationUuid"]
        assert data["customerVariables"] == {
            "vb_customer_name": "Rohan",
            "vb_product_name": "Split AC",
            "vb_rejection_reason": "customer_refused_visit",
            "vb_rejection_reason_label": "the visit was declined when our technician called",
            "vb_extra": "kept",
        }
        assert data["skippedCustomerFields"] == [
            {"key": "wo", "variable": "vb_wo", "reason": "nested"},
        ]
        assert data["missingMandatory"] == []
        assert data["canStart"] is True
        assert data["dialString"] == "sofia/external/+919876543210@dialer.test"
        assert data["command"].startswith("originate {")
        assert data["command"].endswith("}sofia/external/+919876543210@dialer.test &lua(voicebot.lua)")
        assert "vb_product_name='Split AC'" in data["command"]
        assert any("customerNumber is set by the platform" in n for n in data["notes"])
        assert any("voicebotID is set by the platform" in n for n in data["notes"])

    def test_omitted_customer_data_uses_the_bots_saved_test_json(self, client, tenant_admin, bot, esl):
        body = _body()
        body.pop("customerData")
        data = _data(client.post(
            f"{API}/bots/{bot['id']}/testing/real-call/preview", headers=tenant_admin, json=body,
        ))
        assert data["customerVariables"] == {
            "vb_customer_name": "Rohan",
            "vb_product_name": "Split AC",
            "vb_rejection_reason": "customer_refused_visit",
            "vb_rejection_reason_label": "the visit was declined when our technician called",
        }
        # An explicit empty object means "no customer data at all".
        data = _data(client.post(
            f"{API}/bots/{bot['id']}/testing/real-call/preview", headers=tenant_admin,
            json=_body(customerData={}),
        ))
        assert data["customerVariables"] == {}

    def test_blank_mandatory_blocks_start_but_previews(self, client, tenant_admin, bot, esl):
        data = _data(client.post(
            f"{API}/bots/{bot['id']}/testing/real-call/preview", headers=tenant_admin,
            json=_body(params={"sip_route_uri": "", "operatorId": None}),
        ))
        assert data["missingMandatory"] == ["sip_route_uri", "operatorId"]
        assert data["canStart"] is False

    def test_invalid_number_and_values_are_422(self, client, tenant_admin, bot, esl):
        response = client.post(
            f"{API}/bots/{bot['id']}/testing/real-call/preview", headers=tenant_admin,
            json=_body(phoneNumber="98765 abc"),
        )
        assert response.status_code == 422
        assert response.json()["errors"][0]["field"] == "phoneNumber"
        response = client.post(
            f"{API}/bots/{bot['id']}/testing/real-call/preview", headers=tenant_admin,
            json=_body(params={"sip_route_uri": "a\nuuid_kill all"}),
        )
        assert response.status_code == 422
        assert response.json()["errors"][0]["field"] == "sip_route_uri"


class TestStartAndLifecycle:
    def test_confirmation_is_required(self, client, tenant_admin, bot, esl):
        response = client.post(
            f"{API}/bots/{bot['id']}/testing/real-call", headers=tenant_admin,
            json=_body(confirm=False),
        )
        assert response.status_code == 422
        assert esl.instances == []

    def test_blank_mandatory_is_refused(self, client, tenant_admin, bot, esl):
        response = client.post(
            f"{API}/bots/{bot['id']}/testing/real-call", headers=tenant_admin,
            json=_body(params={"operatorId": ""}),
        )
        assert response.status_code == 422
        assert "operatorId" in response.json()["message"]
        assert esl.instances == []

    def test_unpublished_bot_is_refused_before_any_phone_rings(
        self, client, tenant_admin, bot, esl,
    ):
        _set_status(bot["id"], "draft")
        try:
            response = client.post(
                f"{API}/bots/{bot['id']}/testing/real-call", headers=tenant_admin,
                json=_body(),
            )
        finally:
            _set_status(bot["id"], "published")
        assert response.status_code == 409
        assert esl.instances == []

    def test_esl_unconfigured_is_409(self, client, tenant_admin, bot, esl, monkeypatch):
        monkeypatch.setattr(fs, "esl_configured", lambda: False)
        response = client.post(
            f"{API}/bots/{bot['id']}/testing/real-call", headers=tenant_admin,
            json=_body(),
        )
        assert response.status_code == 409

    def test_answered_call_job_and_hangup(self, client, tenant_admin, bot, esl):
        # The view sends only the number (+ optional ring timeout): the
        # backend fills the dialer parameters and the customer vb_* data.
        started = _data(client.post(
            f"{API}/bots/{bot['id']}/testing/real-call", headers=tenant_admin,
            json={"phoneNumber": "+91 98765 43210", "params": {"dialTimeout": "20"}, "confirm": True},
        ), expect=202)
        assert started["status"] == "dialing"
        assert started["phoneMasked"] == "XXXXXXXX3210"
        assert started["botId"] == bot["id"]
        assert started["customerVariables"]["vb_customer_name"] == "Rohan"
        assert "sip_route_uri" in started["variableNames"]
        _wait_for_jobs()
        assert len(esl.instances) == 1
        sent = esl.instances[0]
        assert sent.job_uuid == started["jobId"]
        assert sent.timeout == 90.0  # max(90, 20 + 15)
        assert sent.commands[0].startswith("originate {")
        assert f"origination_uuid={started['originationUuid']}" in sent.commands[0]
        assert f"voicebotID={bot['id']}" in sent.commands[0]
        assert "vb_rejection_reason=customer_refused_visit" in sent.commands[0]
        assert sent.closed is True

        status = _data(client.get(
            f"{API}/bots/{bot['id']}/testing/real-call/{started['jobId']}", headers=tenant_admin,
        ))
        assert status["status"] == "answered"
        assert status["channelUuid"] == "1a2b3c4d-0000-1111-2222-333344445555"
        assert status["conversation"] is None  # no webhook ran in this test

        ended = _data(client.post(
            f"{API}/bots/{bot['id']}/testing/real-call/{started['jobId']}/hangup",
            headers=tenant_admin,
        ))
        assert ended["status"] == "hungup"
        assert esl.instances[-1].commands == [
            "uuid_kill 1a2b3c4d-0000-1111-2222-333344445555 NORMAL_CLEARING",
        ]

    def test_failed_originate_records_the_cause(self, client, tenant_admin, bot, esl):
        esl.outcome = (False, "USER_BUSY")
        started = _data(client.post(
            f"{API}/bots/{bot['id']}/testing/real-call", headers=tenant_admin, json=_body(),
        ), expect=202)
        _wait_for_jobs()
        status = _data(client.get(
            f"{API}/bots/{bot['id']}/testing/real-call/{started['jobId']}", headers=tenant_admin,
        ))
        assert status["status"] == "failed"
        assert status["cause"] == "USER_BUSY"

    def test_socket_failure_is_an_error_not_a_crash(self, client, tenant_admin, bot, esl):
        esl.outcome = fs.ProviderError("freeswitch", "timeout", "ESL connect failed")
        started = _data(client.post(
            f"{API}/bots/{bot['id']}/testing/real-call", headers=tenant_admin, json=_body(),
        ), expect=202)
        _wait_for_jobs()
        status = _data(client.get(
            f"{API}/bots/{bot['id']}/testing/real-call/{started['jobId']}", headers=tenant_admin,
        ))
        assert status["status"] == "error"
        assert status["cause"] == "timeout"

    def test_no_answer_within_the_wait_is_a_timeout(self, client, tenant_admin, bot, esl):
        esl.outcome = None
        started = _data(client.post(
            f"{API}/bots/{bot['id']}/testing/real-call", headers=tenant_admin, json=_body(),
        ), expect=202)
        _wait_for_jobs()
        status = _data(client.get(
            f"{API}/bots/{bot['id']}/testing/real-call/{started['jobId']}", headers=tenant_admin,
        ))
        assert status["status"] == "timeout"

    def test_jobs_are_tenant_scoped(self, client, tenant_admin, other_tenant_admin, bot, esl):
        started = _data(client.post(
            f"{API}/bots/{bot['id']}/testing/real-call", headers=tenant_admin, json=_body(),
        ), expect=202)
        _wait_for_jobs()
        response = client.get(
            f"{API}/bots/{bot['id']}/testing/real-call/{started['jobId']}",
            headers=other_tenant_admin,
        )
        assert response.status_code == 404
        response = client.get(
            f"{API}/bots/{bot['id']}/testing/real-call/not-a-job", headers=tenant_admin,
        )
        assert response.status_code == 404

    def test_audit_row_names_the_call_without_the_full_number(self, client, tenant_admin, bot, esl):
        from sqlalchemy import select

        from shared.db.mysql import get_sessionmaker
        from shared.models import AuditLog

        started = _data(client.post(
            f"{API}/bots/{bot['id']}/testing/real-call", headers=tenant_admin, json=_body(),
        ), expect=202)
        _wait_for_jobs()
        session = get_sessionmaker()()
        try:
            row = session.scalars(
                select(AuditLog).where(
                    AuditLog.entity_id == bot["id"], AuditLog.action == "Started real test call",
                ).order_by(AuditLog.created_at.desc())
            ).first()
        finally:
            session.close()
        assert row is not None
        assert row.new_value["jobId"] == started["jobId"]
        assert row.new_value["phoneMasked"] == "XXXXXXXX3210"
        assert "9876543210" not in (row.target_label or "")
        assert "vb_customer_name" in row.new_value["variables"]
