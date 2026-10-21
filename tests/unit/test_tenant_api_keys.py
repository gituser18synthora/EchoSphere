"""Tenant API tokens: key shape/hashing, the auth dependency's token branch,
and the webhook's variable cap reporting. DB-free (the dependency's lookups
are stubbed); the end-to-end path runs in tests/integration/test_tenant_api_token.py."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

import backend.core.api_keys as api_keys
from backend.core.api_keys import (
    KEY_PREFIX,
    display_prefix,
    generate_api_key,
    hash_api_key,
    looks_like_api_key,
    serialize_api_key,
    service_account_email,
)
from backend.core.deps import get_current_user
from shared.errors import ApiError


class TestKeyShape:
    def test_generated_keys_are_prefixed_unique_and_url_safe(self):
        keys = {generate_api_key() for _ in range(50)}
        assert len(keys) == 50
        for key in keys:
            assert key.startswith(KEY_PREFIX) and len(key) >= 40
            assert all(c.isalnum() or c in "-_" for c in key[len(KEY_PREFIX):])
            assert looks_like_api_key(key)

    def test_hash_is_stable_and_prefix_is_display_only(self):
        key = generate_api_key()
        assert hash_api_key(key) == hash_api_key(key)
        assert len(hash_api_key(key)) == 64
        assert hash_api_key(key) != hash_api_key(generate_api_key())
        assert display_prefix(key) == key[:12]
        assert not looks_like_api_key(display_prefix(key)[:3])

    def test_jwts_and_empty_values_are_not_keys(self):
        assert not looks_like_api_key("eyJhbGciOiJIUzI1NiJ9.x.y")
        assert not looks_like_api_key("")
        assert not looks_like_api_key(None)

    def test_service_account_email_is_per_tenant_and_lowercase(self):
        assert service_account_email("tn_ABC") == "api-tn_abc@service.echosphere.internal"

    def test_serialize_never_includes_the_token_unless_given(self):
        row = SimpleNamespace(id="tak_1", name="Integration token", key_prefix="esk_abcdefgh",
                              status="active", created_at=None, last_used_at=None, revoked_at=None)
        account = SimpleNamespace(email="api-tn@service.echosphere.internal",
                                  role=SimpleNamespace(code="tenant_user"))
        data = serialize_api_key(row, account)
        assert data["configured"] is True and data["token"] is None
        assert data["prefix"] == "esk_abcdefgh" and data["readOnly"] is True
        assert serialize_api_key(None) == {"configured": False, "status": None,
                                           "prefix": None, "token": None}
        assert serialize_api_key(row, account, token="esk_full")["token"] == "esk_full"


def _request(method: str = "GET", headers: dict | None = None) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": "/api/v1/conversations",
             "query_string": b"", "headers": raw_headers}
    return Request(scope)


def _creds(token: str | None):
    return None if token is None else HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.fixture
def principal(monkeypatch):
    """A stubbed active token → service account, plus an active tenant."""
    user = SimpleNamespace(id="usr_svc", tenant_id="tn_1", status="active", is_deleted=False,
                           is_service_account=True, last_active_at=None,
                           role=SimpleNamespace(code="tenant_user", permissions=[]))
    key = SimpleNamespace(id="tak_1", tenant_id="tn_1", status="active", last_used_at=None)
    state = {"valid": "esk_valid_token", "calls": 0}

    def fake_authenticate(db, raw):
        state["calls"] += 1
        return (key, user) if raw == state["valid"] else None

    monkeypatch.setattr(api_keys, "authenticate_api_key", fake_authenticate)
    db = MagicMock()
    db.get.return_value = SimpleNamespace(is_deleted=False, status="active")
    return SimpleNamespace(user=user, key=key, db=db, state=state)


class TestDependencyTokenBranch:
    def test_bearer_token_resolves_to_the_service_account(self, principal):
        request = _request("GET")
        user = get_current_user(request, _creds("esk_valid_token"), principal.db)
        assert user is principal.user
        assert request.state.api_key_id == "tak_1"
        assert principal.key.last_used_at is not None
        principal.db.commit.assert_called_once()

    def test_x_api_key_header_is_accepted(self, principal):
        request = _request("GET", {"X-API-Key": " esk_valid_token "})
        assert get_current_user(request, None, principal.db) is principal.user

    def test_unknown_or_revoked_token_is_401_without_detail(self, principal):
        with pytest.raises(ApiError) as excinfo:
            get_current_user(_request("GET"), _creds("esk_other"), principal.db)
        assert excinfo.value.status_code == 401
        assert "Invalid API token" in str(excinfo.value.message if hasattr(excinfo.value, "message") else excinfo.value)

    @pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
    def test_tokens_are_read_only(self, principal, method):
        with pytest.raises(ApiError) as excinfo:
            get_current_user(_request(method), _creds("esk_valid_token"), principal.db)
        assert excinfo.value.status_code == 403
        principal.db.commit.assert_not_called()

    def test_suspended_or_deleted_tenant_is_refused(self, principal):
        principal.db.get.return_value = SimpleNamespace(is_deleted=False, status="suspended")
        with pytest.raises(ApiError) as excinfo:
            get_current_user(_request("GET"), _creds("esk_valid_token"), principal.db)
        assert excinfo.value.status_code == 403
        principal.db.get.return_value = None
        with pytest.raises(ApiError) as excinfo:
            get_current_user(_request("GET"), _creds("esk_valid_token"), principal.db)
        assert excinfo.value.status_code == 403

    def test_jwt_bearer_does_not_touch_the_token_path(self, principal, monkeypatch):
        import backend.core.security as security

        monkeypatch.setattr(security, "decode_access_token",
                            lambda token: (_ for _ in ()).throw(__import__("jwt").InvalidTokenError()))
        with pytest.raises(ApiError) as excinfo:
            get_current_user(_request("GET"), _creds("eyJ.not.a.key"), principal.db)
        assert excinfo.value.status_code == 401
        assert principal.state["calls"] == 0

    def test_no_credentials_is_401(self, principal):
        with pytest.raises(ApiError) as excinfo:
            get_current_user(_request("GET"), None, principal.db)
        assert excinfo.value.status_code == 401


class TestWebhookVariableCap:
    def test_cap_is_fifty_and_overflow_is_reported_by_name(self):
        from shared.telephony_webhooks import _MAX_VARIABLES, sanitize_variables_report

        assert _MAX_VARIABLES == 50
        kept, dropped = sanitize_variables_report({f"k{i}": "v" for i in range(60)})
        assert len(kept) == 50 and list(kept)[:2] == ["k0", "k1"]
        assert dropped == [f"k{i}" for i in range(50, 60)]

    def test_invalid_entries_are_reported_and_nulls_are_simply_absent(self):
        from shared.telephony_webhooks import sanitize_variables_report

        kept, dropped = sanitize_variables_report({
            "customer_name": "Raju", "bad key!": "x", "nested": {"a": 1},
            "empty": None, "contextLine": "line", "x" * 41: "too long",
        })
        assert kept == {"customer_name": "Raju", "contextLine": "line"}
        assert dropped == ["bad key!", "nested", "x" * 40]

    def test_legacy_wrapper_and_session_payload(self):
        import asyncio

        from shared.telephony_webhooks import _sanitize_variables

        assert _sanitize_variables({"a": 1, "b": None}) == {"a": "1"}
        # create_voice_session only writes the key when something was dropped.
        import shared.voice_sessions as vs

        captured = {}

        class _Redis:
            async def set(self, key, value, ex=None):
                captured["value"] = value

        vs_get_redis = vs.get_redis
        vs.get_redis = lambda: _Redis()
        try:
            payload = asyncio.run(vs.create_voice_session(
                tenant_id="tn", bot_id="bot", user_id=None, channel="phone",
                variables={"a": "1"}, variables_dropped=["bad key!"],
            ))
            assert payload["variables_dropped"] == ["bad key!"]
            payload = asyncio.run(vs.create_voice_session(
                tenant_id="tn", bot_id="bot", user_id=None, channel="phone",
                variables={"a": "1"}, variables_dropped=[],
            ))
            assert "variables_dropped" not in payload
        finally:
            vs.get_redis = vs_get_redis


class TestRecorderCallId:
    def test_recorder_keeps_the_telephony_call_id(self):
        from voice_runtime.recording import SessionRecorder

        config = SimpleNamespace(tenant_id="tn", bot_id="bot", language="hi-IN", version=1,
                                 prompt_id=None, prompt_version=None, prompt_mode=None,
                                 llm={}, tts={}, stt={})
        rec = SessionRecorder("vs_x", config, channel="freeswitch", caller="+91",
                              call_id="652db2e9-a64b-4688-aff2-933a70e59c84")
        assert rec.call_id == "652db2e9-a64b-4688-aff2-933a70e59c84"
        assert SessionRecorder("vs_y", config).call_id is None
        assert SessionRecorder("vs_z", config, call_id="").call_id is None
