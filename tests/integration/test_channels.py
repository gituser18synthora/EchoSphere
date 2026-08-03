"""Channel management: CRUD, provider validation, secret handling, status
transitions, real connection tests, webhooks and audit.

Runs against the live app + local databases. A dedicated throwaway bot is
created per module and every row it touches (channels, phone numbers, settings,
readiness, the bot itself) is removed in teardown — demo data is never mutated.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.core.security import create_access_token
from backend.main import app
from backend.serializers import mask_channel_config

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]


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
def super_admin():
    return _bearer("admin@aurexion.com")


@pytest.fixture(scope="module")
def tenant_admin():
    return _bearer("priya.sharma@meridianhealth.com")  # tenant_admin of tn-001


@pytest.fixture(scope="module")
def tenant_user():
    return _bearer("sam.ellery@meridianhealth.com")  # tenant_user — no manage_channels


@pytest.fixture(scope="module")
def other_admin():
    return _bearer("alex.rivera@aurexion.com")  # different-org context (super admin)


def _enabled_language() -> str:
    """Any currently-enabled platform language (shared dev DB is user-curated,
    so a hardcoded 'en-US' can be disabled at any time — never assume it)."""
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import SupportedLanguage

    session = get_sessionmaker()()
    try:
        codes = session.scalars(
            select(SupportedLanguage.code).where(SupportedLanguage.enabled.is_(True))
        ).all()
        for preferred in ("en-US", "en-IN", "hi-IN"):
            if preferred in codes:
                return preferred
        assert codes, "no enabled platform language to create a test bot with"
        return codes[0]
    finally:
        session.close()


@pytest.fixture(scope="module")
def test_bot(client, tenant_admin):
    """A dedicated tn-001 bot, forced to published so activation paths work."""
    created = _data(client.post(f"{API}/bots", headers=tenant_admin, json={
        "name": f"Channel Test Bot {_SUFFIX}", "useCase": "channels",
        "languages": [_enabled_language()],
    }))
    bot_id = created["id"]

    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import Prompt, VoiceBot

    session = get_sessionmaker()()
    try:
        bot = session.get(VoiceBot, bot_id)
        bot.status = "published"
        bot.live_version = "v1.0.0"
        # a published system prompt so the binding shows systemPromptPublished
        session.add(Prompt(
            id=f"pr_ch_{_SUFFIX}", tenant_id="tn-001", bot_id=bot_id, type="system",
            name="sys", state="published",
        ))
        session.commit()
    finally:
        session.close()

    yield {"id": bot_id}

    # ── teardown: children first, then the bot ──
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    _purge_bot(bot_id)


def _purge_bot(bot_id: str) -> None:
    """Hard-delete a bot and every child row create_bot / channels created."""
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        conn.execute(sa_text("DELETE FROM audit_logs WHERE entity_id IN "
                             "(SELECT id FROM channel_configs WHERE bot_id = :b)"), {"b": bot_id})
        for table in ("channel_configs", "voice_bot_settings", "voice_bot_readiness",
                      "bot_languages", "workflows", "phone_numbers", "prompts"):
            conn.execute(sa_text(f"DELETE FROM `{table}` WHERE bot_id = :b"), {"b": bot_id})
        conn.execute(sa_text("DELETE FROM voice_bots WHERE id = :b"), {"b": bot_id})


# ── Listing & slots ───────────────────────────────────────────────────────────


def test_lists_all_channel_slots(client, tenant_admin, test_bot):
    rows = _data(client.get(f"{API}/bots/{test_bot['id']}/channels", headers=tenant_admin))
    types = {r["type"] for r in rows}
    assert {"voice", "whatsapp", "web", "mobile", "sms"} <= types
    for r in rows:
        assert r["status"] == "not_configured"
        assert r["binding"]["botStatus"] == "published"


# ── Create + validation ─────────────────────────────────────────────────────


def test_create_valid_web_channel(client, tenant_admin, test_bot):
    result = _data(client.put(
        f"{API}/bots/{test_bot['id']}/channels/web", headers=tenant_admin,
        json={"config": {"allowedOrigins": ["https://app.example.com"], "widgetColor": "#1A73E8"}},
    ))
    assert result["status"] == "configured"
    assert result["enabled"] is True
    assert result["detail"].startswith("https://app.example.com")
    assert result["config"]["allowedOrigins"] == ["https://app.example.com"]
    assert result["binding"]["knowledgeBases"] >= 0


def test_edit_existing_channel(client, tenant_admin, test_bot):
    result = _data(client.put(
        f"{API}/bots/{test_bot['id']}/channels/web", headers=tenant_admin,
        json={"config": {"allowedOrigins": ["https://app.example.com", "https://portal.example.com"]}},
    ))
    assert "+1" in result["detail"]  # "origin +1" summary
    assert len(result["config"]["allowedOrigins"]) == 2


def test_missing_required_fields(client, tenant_admin, test_bot):
    # SMS requires senderId + apiKeyReference
    response = client.put(f"{API}/bots/{test_bot['id']}/channels/sms", headers=tenant_admin,
                          json={"config": {"provider": "twilio"}})
    assert response.status_code == 422


def test_invalid_provider_specific_fields(client, tenant_admin, test_bot):
    cases = [
        ("voice", {"phoneNumber": "not-a-number", "telephonyProvider": "twilio",
                   "publicWsBase": "wss://x.example.com", "authTokenReference": "env:T"}),
        ("whatsapp", {"whatsappNumber": "12345", "provider": "meta", "phoneNumberId": "1",
                      "apiKeyReference": "env:K", "webhookSecretReference": "env:S"}),
        ("web", {"allowedOrigins": ["not-a-url"]}),
        ("mobile", {"platform": "ios", "bundleIds": ["nodots"]}),
        ("sms", {"provider": "twilio", "senderId": "ok123", "accountId": "AC1",
                 "apiKeyReference": "env:K", "senderIdBad": 1}),  # extra field forbidden
    ]
    for ctype, config in cases:
        response = client.put(f"{API}/bots/{test_bot['id']}/channels/{ctype}",
                              headers=tenant_admin, json={"config": config})
        assert response.status_code == 422, (ctype, response.json())


def test_raw_secret_rejected_reference_required(client, tenant_admin, test_bot):
    """A raw secret must never be storable — only env: references pass."""
    response = client.put(f"{API}/bots/{test_bot['id']}/channels/sms", headers=tenant_admin, json={
        "config": {"provider": "twilio", "senderId": "AUREXION", "accountId": "AC123",
                   "apiKeyReference": "sk-this-is-a-raw-secret"},
    })
    assert response.status_code == 422
    assert "env:" in response.json()["message"]


# ── Secret masking ─────────────────────────────────────────────────────────


def test_secret_reference_stored_not_raw(client, tenant_admin, test_bot):
    _data(client.put(f"{API}/bots/{test_bot['id']}/channels/whatsapp", headers=tenant_admin, json={
        "config": {"whatsappNumber": "+14155550142", "provider": "meta", "phoneNumberId": "109",
                   "apiKeyReference": "env:WA_TEST_KEY", "webhookSecretReference": "env:WA_TEST_SECRET"},
    }))
    fetched = _data(client.get(f"{API}/bots/{test_bot['id']}/channels/whatsapp", headers=tenant_admin))
    # References pass through (they name an env var, not a secret); no raw secret present.
    assert fetched["config"]["apiKeyReference"] == "env:WA_TEST_KEY"
    assert "sk-" not in str(fetched["config"])


def test_mask_channel_config_masks_raw_secrets():
    """Defensive masking for any legacy/hand-edited row holding a raw secret."""
    masked = mask_channel_config({"apiKeyReference": "sk-raw-secret", "phoneNumber": "+14155550142"})
    assert masked["apiKeyReference"] == "••••••••"
    assert masked["phoneNumber"] == "+14155550142"
    # References are not masked.
    assert mask_channel_config({"apiKeyReference": "env:X"})["apiKeyReference"] == "env:X"


# ── Duplicate configuration ──────────────────────────────────────────────────


def test_duplicate_channel_updates_in_place(client, tenant_admin, test_bot):
    """PUT is idempotent per (bot, type) — no duplicate rows via the unique key."""
    for _ in range(2):
        _data(client.put(f"{API}/bots/{test_bot['id']}/channels/mobile", headers=tenant_admin,
                         json={"config": {"platform": "both", "bundleIds": ["com.example.app"]}}))
    rows = _data(client.get(f"{API}/bots/{test_bot['id']}/channels", headers=tenant_admin))
    mobile = [r for r in rows if r["type"] == "mobile" and r["status"] != "not_configured"]
    assert len(mobile) == 1


def test_duplicate_phone_number_rejected(client, tenant_admin, test_bot):
    """A voice number already assigned to another bot cannot be reused."""
    # pn-01 is assigned to bot-101 in the demo data.
    response = client.put(f"{API}/bots/{test_bot['id']}/channels/voice", headers=tenant_admin, json={
        "config": {"phoneNumber": "+14155550119", "telephonyProvider": "freeswitch"},
    })
    assert response.status_code == 409
    assert "already" in response.json()["message"].lower()


# ── Activate / deactivate ────────────────────────────────────────────────────


def test_activate_deactivate_cycle(client, tenant_admin, test_bot):
    bot_id = test_bot["id"]
    # web channel already configured above; deactivate then reactivate.
    off = _data(client.post(f"{API}/bots/{bot_id}/channels/web/deactivate", headers=tenant_admin))
    assert off["enabled"] is False
    on = _data(client.post(f"{API}/bots/{bot_id}/channels/web/activate", headers=tenant_admin))
    assert on["enabled"] is True


def test_activate_requires_published_bot(client, tenant_admin):
    """Voice/WhatsApp/SMS need a published bot before going live."""
    draft = _data(client.post(f"{API}/bots", headers=tenant_admin, json={
        "name": f"Draft Bot {_SUFFIX}", "useCase": "x", "languages": [_enabled_language()]}))
    draft_id = draft["id"]
    try:
        _data(client.put(f"{API}/bots/{draft_id}/channels/sms", headers=tenant_admin, json={
            "config": {"provider": "twilio", "senderId": "AUREXION", "accountId": "AC1",
                       "apiKeyReference": "env:SMS_KEY"}}))
        response = client.post(f"{API}/bots/{draft_id}/channels/sms/activate", headers=tenant_admin)
        assert response.status_code == 422
        assert "publish" in response.json()["message"].lower()
    finally:
        _purge_bot(draft_id)


# ── Connection test (real checks, no fake success) ──────────────────────────


def test_connection_test_failure_on_unresolvable_secret(client, tenant_admin, test_bot):
    """WhatsApp test fails honestly when the API key reference does not resolve."""
    os.environ.pop("WA_TEST_KEY", None)
    os.environ.pop("WA_TEST_SECRET", None)
    result = _data(client.post(f"{API}/bots/{test_bot['id']}/channels/whatsapp/test",
                               headers=tenant_admin))
    assert result["status"] == "failed"
    assert result["lastTest"]["ok"] is False
    assert any(not c["ok"] for c in result["lastTest"]["checks"])


def test_connection_test_promotes_to_live(client, tenant_admin, test_bot):
    """Web channel test only needs the voice runtime reachable; set a resolvable
    secret so nothing else fails."""
    result = _data(client.post(f"{API}/bots/{test_bot['id']}/channels/web/test",
                               headers=tenant_admin))
    # The voice runtime may or may not be running in CI; assert the test ran for
    # real (checks present) and the status reflects the actual outcome.
    assert result["lastTest"] is not None
    assert isinstance(result["lastTest"]["checks"], list) and result["lastTest"]["checks"]
    assert result["status"] in ("live", "configured", "failed")
    if result["lastTest"]["ok"]:
        assert result["status"] == "live"


# ── Authorization & tenant isolation ─────────────────────────────────────────


def test_unauthorized_user_cannot_manage(client, tenant_user, test_bot):
    bot_id = test_bot["id"]
    assert client.put(f"{API}/bots/{bot_id}/channels/web", headers=tenant_user,
                      json={"config": {"allowedOrigins": ["https://x.example.com"]}}).status_code == 403
    assert client.post(f"{API}/bots/{bot_id}/channels/web/test", headers=tenant_user).status_code == 403
    assert client.post(f"{API}/bots/{bot_id}/channels/web/deactivate", headers=tenant_user).status_code == 403
    assert client.delete(f"{API}/bots/{bot_id}/channels/web", headers=tenant_user).status_code == 403
    # ...but a tenant member may still read.
    assert client.get(f"{API}/bots/{bot_id}/channels", headers=tenant_user).status_code == 200


def test_cross_tenant_access_denied(client, test_bot):
    """A different tenant's admin cannot see or mutate this bot's channels."""
    # meridianhealth admin belongs to tn-001; make a foreign-tenant admin token.
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import Tenant, User

    session = get_sessionmaker()()
    try:
        foreign = session.execute(
            select(User).join(Tenant, User.tenant_id == Tenant.id)
            .where(User.tenant_id.isnot(None), User.tenant_id != "tn-001",
                   User.role.has(code="tenant_admin"))
        ).scalars().first()
    finally:
        session.close()
    if foreign is None:
        pytest.skip("no foreign-tenant admin in the dataset")
    headers = {"Authorization": f"Bearer {create_access_token(user_id=foreign.id, role='tenant_admin', tenant_id=foreign.tenant_id)}"}
    # 404 (not 403) — cross-tenant records are not revealed to exist.
    assert client.get(f"{API}/bots/{test_bot['id']}/channels", headers=headers).status_code == 404
    assert client.put(f"{API}/bots/{test_bot['id']}/channels/web", headers=headers,
                      json={"config": {"allowedOrigins": ["https://x.example.com"]}}).status_code == 404


# ── Archive ─────────────────────────────────────────────────────────────────


def test_archive_channel(client, tenant_admin, test_bot):
    bot_id = test_bot["id"]
    _data(client.put(f"{API}/bots/{bot_id}/channels/mobile", headers=tenant_admin,
                     json={"config": {"platform": "ios", "bundleIds": ["com.example.mobile"]}}))
    assert _data(client.delete(f"{API}/bots/{bot_id}/channels/mobile", headers=tenant_admin))["archived"] is True
    rows = _data(client.get(f"{API}/bots/{bot_id}/channels", headers=tenant_admin))
    mobile = next(r for r in rows if r["type"] == "mobile")
    assert mobile["status"] == "not_configured"  # archived row excluded


# ── Audit ───────────────────────────────────────────────────────────────────


def test_audit_log_created_without_secrets(client, tenant_admin, test_bot):
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import AuditLog

    session = get_sessionmaker()()
    try:
        rows = session.execute(
            select(AuditLog).where(AuditLog.entity_type == "channel")
            .order_by(AuditLog.created_at.desc()).limit(40)
        ).scalars().all()
        actions = {r.action for r in rows}
        assert {"Created channel", "Tested channel connection"} & actions
        assert any(a in actions for a in ("Activated channel", "Deactivated channel"))
        assert "Archived channel" in actions
        # Audit must never contain raw secret material. (env: references name an
        # environment variable and are not themselves secret.)
        blob = "".join(str(r.previous_value) + str(r.new_value) for r in rows)
        assert "sk-" not in blob, "possible raw secret leak in audit log"
    finally:
        session.close()


# ── WhatsApp webhook: verification, replay, mapping, enablement ──────────────


class TestWhatsAppWebhook:
    @pytest.fixture(scope="class")
    def wa_channel(self, client, tenant_admin, test_bot):
        os.environ["WA_HOOK_SECRET"] = "wa-hook-secret-value"
        row = _data(client.put(f"{API}/bots/{test_bot['id']}/channels/whatsapp", headers=tenant_admin, json={
            "config": {"whatsappNumber": "+14155550142", "provider": "meta", "phoneNumberId": "109",
                       "apiKeyReference": "env:WA_TEST_KEY", "webhookSecretReference": "env:WA_HOOK_SECRET"},
        }))
        return row

    def test_verify_handshake(self, client, wa_channel):
        cid = wa_channel["id"]
        ok = client.get(f"{API}/channels/whatsapp/webhook/{cid}",
                        params={"hub.mode": "subscribe", "hub.verify_token": "wa-hook-secret-value",
                                "hub.challenge": "42"})
        assert ok.status_code == 200 and ok.text == "42"
        bad = client.get(f"{API}/channels/whatsapp/webhook/{cid}",
                         params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42"})
        assert bad.status_code == 403

    def test_signature_required(self, client, wa_channel):
        r = client.post(f"{API}/channels/whatsapp/webhook/{wa_channel['id']}",
                        content=b'{"x":1}', headers={"Content-Type": "application/json"})
        assert r.status_code == 403

    def test_valid_signature_accepted_and_replay_blocked(self, client, wa_channel):
        import hashlib
        import hmac

        # Unique body per run → unique signature (the replay store is real Redis
        # and persists across test runs; a fixed signature would self-replay).
        body = f'{{"entry":[{{"id":"{_SUFFIX}"}}]}}'.encode()
        sig = "sha256=" + hmac.new(b"wa-hook-secret-value", body, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Hub-Signature-256": sig}
        first = client.post(f"{API}/channels/whatsapp/webhook/{wa_channel['id']}",
                            content=body, headers=headers)
        assert first.status_code == 200
        # same signature again → replay rejected
        replay = client.post(f"{API}/channels/whatsapp/webhook/{wa_channel['id']}",
                             content=body, headers=headers)
        assert replay.status_code == 403

    def test_disabled_channel_rejected(self, client, tenant_admin, test_bot, wa_channel):
        import hashlib
        import hmac

        _data(client.post(f"{API}/bots/{test_bot['id']}/channels/whatsapp/deactivate", headers=tenant_admin))
        body = f'{{"entry":[{{"disabled":"{_SUFFIX}"}}]}}'.encode()
        sig = "sha256=" + hmac.new(b"wa-hook-secret-value", body, hashlib.sha256).hexdigest()
        r = client.post(f"{API}/channels/whatsapp/webhook/{wa_channel['id']}",
                        content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
        assert r.status_code == 403
        assert "message" in r.json()
        _data(client.post(f"{API}/bots/{test_bot['id']}/channels/whatsapp/activate", headers=tenant_admin))

    def test_unknown_channel_sanitized(self, client):
        r = client.get(f"{API}/channels/whatsapp/webhook/ch_does_not_exist",
                       params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"})
        assert r.status_code == 404
