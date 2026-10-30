"""Customer collection-context API: CRUD, auth, tenant scoping, masking,
call-state updates, phone lookup, validation, and the runtime loader.

Runs against the live app + local databases. A dedicated throwaway bot is
created per module and every row it touches is removed in teardown.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
_PHONE = "+9190000" + str(int(_SUFFIX, 16) % 100000).zfill(5)


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


def _data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


@pytest.fixture(scope="module")
def tenant_admin():
    return _bearer("priya.sharma@meridianhealth.com")  # tenant_admin of tn-001


@pytest.fixture(scope="module")
def tenant_user():
    return _bearer("sam.ellery@meridianhealth.com")  # tenant_user of tn-001


def _enabled_language() -> str:
    """Any enabled platform language — the shared dev DB is user-curated."""
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
        assert codes, "no enabled platform language to create a test bot with"
        return codes[0]
    finally:
        session.close()


@pytest.fixture(scope="module")
def test_bot(client, tenant_admin):
    created = _data(client.post(f"{API}/bots", headers=tenant_admin, json={
        "name": f"CC Test Bot {_SUFFIX}", "useCase": "collections",
        "languages": [_enabled_language()],
    }))
    bot_id = created["id"]
    yield {"id": bot_id}

    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        for table in ("customer_contexts", "voice_bot_readiness", "bot_languages",
                      "voice_bot_settings", "prompts", "workflows", "intents",
                      "audit_logs"):
            column = "entity_id" if table == "audit_logs" else "bot_id"
            try:
                conn.execute(sa_text(f"DELETE FROM {table} WHERE {column} = :b"),
                             {"b": bot_id})
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        conn.execute(sa_text("DELETE FROM voice_bots WHERE id = :b"), {"b": bot_id})


PAYLOAD = {
    "customerRef": f"CUST-{_SUFFIX}",
    "phone": _PHONE,
    "customerName": "Test Grahak",
    "dcsName": "eDAS Recoveries",
    "lenderName": "eDAS Finance",
    "loanAccountNumber": "LN00998877",
    "preferredLanguage": "hi-IN",
    "overdueAmount": 4850,
    "totalOutstanding": 5120,
    "minimumPayable": 2000,
    "penalCharges": 270,
    "daysOverdue": 12,
    "dueDate": "2026-07-23",
    "partialPaymentAllowed": True,
    "paymentMethods": ["UPI", "Debit Card"],
    "securePaymentLinkAvailable": True,
    "activeOffers": [{"label": "BHIM discount", "terms": "up to forty rupees"}],
    "creditReportingStatus": "not yet reported",
    "callbackNumber": _PHONE,
    "grievanceContact": "grievance@example",
    "paymentStatus": "pending",
}


class TestCrudAndMasking:
    def test_create_returns_masked_payload(self, client, tenant_admin, test_bot):
        data = _data(client.post(
            f"{API}/bots/{test_bot['id']}/customer-contexts",
            headers=tenant_admin, json=PAYLOAD,
        ))
        assert data["id"].startswith("cctx_")
        # Sensitive values never round-trip — masked derivations only.
        assert "loanAccountNumber" not in data
        assert "phone" not in data
        assert data["loanAccountMasked"] == "XX8877"
        assert data["phoneMasked"] == "XXXXXX" + _PHONE[-4:]
        assert data["callbackNumber"] == "XXXXXX" + _PHONE[-4:]
        # Typed values, unknown ≠ zero ≠ false.
        assert data["overdueAmount"] == 4850.0
        assert data["daysOverdue"] == 12
        assert data["partialPaymentAllowed"] is True
        assert data["customerVerified"] is False
        assert data["recordingNoticeRequired"] is True
        assert data["paymentStatus"] == "pending"
        TestCrudAndMasking.context_id = data["id"]

    def test_unknown_values_stay_null(self, client, tenant_admin, test_bot):
        data = _data(client.post(
            f"{API}/bots/{test_bot['id']}/customer-contexts",
            headers=tenant_admin,
            json={"customerName": "Minimal Grahak", "phone": "+919000012345"},
        ))
        assert data["overdueAmount"] is None
        assert data["daysOverdue"] is None
        assert data["partialPaymentAllowed"] is None
        assert data["paymentMethods"] is None
        assert data["loanAccountMasked"] is None
        TestCrudAndMasking.minimal_id = data["id"]

    def test_list_and_search(self, client, tenant_admin, test_bot):
        items = _data(client.get(
            f"{API}/bots/{test_bot['id']}/customer-contexts?search=Test Grahak",
            headers=tenant_admin,
        ))
        assert any(i["id"] == self.context_id for i in items)

    def test_lookup_by_phone_variants(self, client, tenant_admin, test_bot):
        # +91 form, 0-prefixed form and bare 10 digits must all resolve.
        tail10 = _PHONE[-10:]
        for variant in (_PHONE, f"0{tail10}", tail10):
            data = _data(client.get(
                f"{API}/bots/{test_bot['id']}/customer-contexts/lookup",
                headers=tenant_admin, params={"phone": variant},
            ))
            assert data["id"] == self.context_id, variant

    def test_lookup_unknown_phone_404(self, client, tenant_admin, test_bot):
        response = client.get(
            f"{API}/bots/{test_bot['id']}/customer-contexts/lookup",
            headers=tenant_admin, params={"phone": "+919999999999"},
        )
        assert response.status_code == 404

    def test_patch_profile(self, client, tenant_admin):
        data = _data(client.patch(
            f"{API}/customer-contexts/{self.context_id}",
            headers=tenant_admin, json={"overdueAmount": 5000, "daysOverdue": 13},
        ))
        assert data["overdueAmount"] == 5000.0
        assert data["daysOverdue"] == 13


class TestAuthAndValidation:
    def test_create_requires_tenant_admin(self, client, tenant_user, test_bot):
        response = client.post(
            f"{API}/bots/{test_bot['id']}/customer-contexts",
            headers=tenant_user, json={"customerName": "Nope"},
        )
        assert response.status_code == 403

    def test_call_state_open_to_tenant_members(self, client, tenant_user):
        data = _data(client.patch(
            f"{API}/customer-contexts/{TestCrudAndMasking.context_id}/call-state",
            headers=tenant_user,
            json={"customerVerified": True, "accountDisputed": True,
                  "paymentStatus": "disputed", "callbackRequested": True,
                  "lastDisposition": "account_disputed",
                  "isFinalTranscript": True, "interruptionDetected": True},
        ))
        assert data["customerVerified"] is True
        assert data["accountDisputed"] is True
        assert data["paymentStatus"] == "disputed"
        assert data["callbackRequested"] is True
        assert data["callbackRequestedAt"] is not None  # auto-stamped
        assert data["lastDisposition"] == "account_disputed"
        assert data["isFinalTranscript"] is True
        assert data["interruptionDetected"] is True

    def test_invalid_payment_status_rejected(self, client, tenant_admin):
        response = client.patch(
            f"{API}/customer-contexts/{TestCrudAndMasking.context_id}/call-state",
            headers=tenant_admin, json={"paymentStatus": "definitely-paid"},
        )
        assert response.status_code in (400, 422)

    def test_unknown_fields_rejected(self, client, tenant_admin):
        response = client.patch(
            f"{API}/customer-contexts/{TestCrudAndMasking.context_id}/call-state",
            headers=tenant_admin, json={"loanAccountNumber": "HACKED"},
        )
        assert response.status_code in (400, 422)

    def test_invalid_phone_rejected(self, client, tenant_admin, test_bot):
        response = client.post(
            f"{API}/bots/{test_bot['id']}/customer-contexts",
            headers=tenant_admin, json={"phone": "not-a-phone"},
        )
        assert response.status_code in (400, 422)

    def test_requires_auth(self, client, test_bot):
        response = client.get(f"{API}/bots/{test_bot['id']}/customer-contexts")
        assert response.status_code == 401

    def test_voice_session_validates_context_ownership(self, client, tenant_admin,
                                                       test_bot):
        # A context id from a DIFFERENT bot must 404, never leak.
        response = client.post(f"{API}/voice-sessions", headers=tenant_admin, json={
            "botId": test_bot["id"], "customerContextId": "cctx_doesnotexist",
        })
        assert response.status_code == 404

    def test_voice_session_carries_context_and_variables(self, client, tenant_admin,
                                                         test_bot):
        import asyncio

        from shared.voice_sessions import end_voice_session, load_voice_session

        data = _data(client.post(f"{API}/voice-sessions", headers=tenant_admin, json={
            "botId": test_bot["id"],
            "customerContextId": TestCrudAndMasking.context_id,
            "variables": {"campaign": "dpd-30"},
        }))
        session = asyncio.get_event_loop().run_until_complete(
            load_voice_session(data["sessionId"])
        )
        assert session["customer_context_id"] == TestCrudAndMasking.context_id
        assert session["variables"] == {"campaign": "dpd-30"}
        asyncio.get_event_loop().run_until_complete(
            end_voice_session(data["sessionId"])
        )


class TestRuntimeLoader:
    def test_load_by_phone_returns_masked_snapshot(self, test_bot):
        from shared.customer_context import _load_sync

        snap = _load_sync("tn-001", test_bot["id"], phone=f"0{_PHONE[-10:]}")
        assert snap is not None
        assert snap.customer_name == "Test Grahak"
        assert snap.loan_account_masked == "XX8877"
        assert snap.phone_last4 == _PHONE[-4:]
        # The raw phone / account number are not on the snapshot at all.
        assert not hasattr(snap, "phone")
        assert not hasattr(snap, "loan_account_number")

    def test_load_wrong_tenant_returns_none(self, test_bot):
        from shared.customer_context import _load_sync

        assert _load_sync("tn_other", test_bot["id"], phone=_PHONE) is None
        assert _load_sync("tn-001", test_bot["id"],
                          context_id=TestCrudAndMasking.context_id) is not None
        assert _load_sync("tn_other", test_bot["id"],
                          context_id=TestCrudAndMasking.context_id) is None

    def test_call_state_write_back_whitelist(self, test_bot):
        from shared.customer_context import _load_sync, record_call_state_sync

        assert record_call_state_sync(
            TestCrudAndMasking.minimal_id,
            last_disposition="callback_requested",
            callback_requested=True,
            is_final_transcript=True,
            customer_name="INJECTED",       # not writable — dropped
            payment_status="not-a-status",  # invalid — dropped
        ) is True
        snap = _load_sync("tn-001", test_bot["id"],
                          context_id=TestCrudAndMasking.minimal_id)
        assert snap.callback_requested is True
        assert snap.customer_name == "Minimal Grahak"  # injection ignored
        assert snap.payment_status == "pending"


class TestDelete:
    def test_soft_delete(self, client, tenant_admin):
        assert _data(client.delete(
            f"{API}/customer-contexts/{TestCrudAndMasking.minimal_id}",
            headers=tenant_admin,
        ))["deleted"] is True
        response = client.get(
            f"{API}/customer-contexts/{TestCrudAndMasking.minimal_id}",
            headers=tenant_admin,
        )
        assert response.status_code == 404
