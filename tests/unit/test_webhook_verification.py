"""Telephony webhook signatures: generic HMAC + Twilio scheme + freshness."""

import hashlib
import hmac
import time

import pytest

from backend.telephony.webhooks import (
    WebhookVerificationError,
    verify_generic_signature,
    verify_twilio_signature,
)

SECRET = "test-webhook-secret"


def sign(body: bytes, ts: int) -> str:
    return hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


class TestGenericSignature:
    def test_valid(self):
        ts = int(time.time())
        body = b'{"To": "+15550100"}'
        verify_generic_signature(
            body=body, signature=sign(body, ts), timestamp=str(ts), secret=SECRET
        )

    def test_tampered_body(self):
        ts = int(time.time())
        with pytest.raises(WebhookVerificationError):
            verify_generic_signature(
                body=b"tampered", signature=sign(b"original", ts),
                timestamp=str(ts), secret=SECRET,
            )

    def test_stale_timestamp(self):
        ts = int(time.time()) - 3600
        body = b"x"
        with pytest.raises(WebhookVerificationError):
            verify_generic_signature(
                body=body, signature=sign(body, ts), timestamp=str(ts), secret=SECRET
            )

    def test_missing_headers(self):
        with pytest.raises(WebhookVerificationError):
            verify_generic_signature(body=b"x", signature="", timestamp="", secret=SECRET)


class TestTwilioSignature:
    URL = "https://example.com/api/v1/telephony/webhook/twilio"

    def _sign(self, params: dict) -> str:
        import base64

        payload = self.URL + "".join(f"{k}{params[k]}" for k in sorted(params))
        return base64.b64encode(
            hmac.new(SECRET.encode(), payload.encode(), hashlib.sha1).digest()
        ).decode()

    def test_valid(self):
        params = {"To": "+15550100", "From": "+15550199", "CallSid": "CA123"}
        verify_twilio_signature(
            url=self.URL, params=params, signature=self._sign(params), auth_token=SECRET
        )

    def test_invalid(self):
        params = {"To": "+15550100"}
        with pytest.raises(WebhookVerificationError):
            verify_twilio_signature(
                url=self.URL, params={"To": "+15550111"},
                signature=self._sign(params), auth_token=SECRET,
            )


@pytest.mark.integration
async def test_replay_protection_via_redis():
    from backend.telephony.webhooks import check_replay

    signature = f"sig-{time.time()}"
    await check_replay(signature)
    with pytest.raises(WebhookVerificationError):
        await check_replay(signature)


class TestDialerVariableSanitization:
    """Per-call variables from the signed telephony webhook are bounded and
    shape-checked before they reach Redis, logs or the LLM context."""

    def _sanitize(self, raw):
        from backend.routers.telephony import _sanitize_variables

        return _sanitize_variables(raw)

    def test_valid_variables_pass_through_as_strings(self):
        out = self._sanitize({"customer_name": "Rahul", "amount": 2000,
                              "vip": True, "dpd.bucket": "0-7"})
        assert out == {"customer_name": "Rahul", "amount": "2000",
                       "vip": "True", "dpd.bucket": "0-7"}

    def test_bad_keys_values_and_shapes_are_dropped(self):
        assert self._sanitize("not a dict") == {}
        assert self._sanitize(None) == {}
        out = self._sanitize({
            "ok": "yes",
            "bad key!": "dropped",              # illegal characters
            "x" * 41: "dropped",                 # key too long
            "nested": {"drop": "me"},            # non-scalar value
            "list": ["drop"],
        })
        assert out == {"ok": "yes"}

    def test_limits_are_enforced(self):
        out = self._sanitize({f"k{i}": "v" for i in range(80)})
        assert len(out) == 50          # cap raised from 20; overflow is reported by name
        out = self._sanitize({"long": "A" * 500})
        assert len(out["long"]) == 200


class TestPublicWsBase:
    """The webhook must hand providers a WS URL that actually reaches the
    voice worker: explicit TELEPHONY_PUBLIC_WS_BASE wins; empty setting keeps
    the historical derive-from-request behavior."""

    class _Req:
        base_url = "http://192.168.60.123:9011/"

    class _Settings:
        def __init__(self, base):
            self.telephony_public_ws_base = base

    def _resolve(self, base):
        from backend.routers.telephony import _public_ws_base

        return _public_ws_base(self._Settings(base), self._Req())

    def test_configured_base_wins_and_is_normalized(self):
        assert self._resolve("ws://192.168.60.123:9002/") == "ws://192.168.60.123:9002"
        assert self._resolve("wss://voice.example.com") == "wss://voice.example.com"

    def test_empty_setting_derives_from_request(self):
        assert self._resolve("") == "ws://192.168.60.123:9011"
        assert self._resolve("   ") == "ws://192.168.60.123:9011"


class TestDialedTargetRouting:
    """``To`` carries either a DID or a bot id (``bot_…``). The resolver
    anchors the tenant on whichever it is; the extractor shape-checks a bot
    id before any lookup and leaves DIDs untouched (exact-match contract)."""

    def test_is_bot_id(self):
        from shared.bot_config import is_bot_id

        assert is_bot_id("bot_33fa43aa0668")
        assert is_bot_id("  bot_33fa43aa0668 ")
        assert not is_bot_id("+918047133639")
        assert not is_bot_id("+91 80 4522 1010")
        assert not is_bot_id("")
        assert not is_bot_id(None)
        assert not is_bot_id("bot-101")  # legacy demo ids keep routing by number

    @staticmethod
    def _request(payload: dict):
        import json

        from starlette.requests import Request

        body = json.dumps(payload).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"",
                 "headers": [(b"content-type", b"application/json")]}
        return Request(scope, receive)

    async def test_extract_strips_and_validates_bot_id_in_to(self):
        from shared.telephony_webhooks import _extract_call_details

        called, caller, call_id, variables, bot_id, direction = await _extract_call_details(
            "freeswitch", self._request({
                "To": " bot_33fa43aa0668 ", "From": "+919812345678", "callId": "uuid-1",
                "contextLine": "inbound-api-routing-phone", "city": "Pune",
            }))
        assert called == "bot_33fa43aa0668"
        assert (caller, call_id, bot_id, direction) == ("+919812345678", "uuid-1", None, None)
        assert variables == {}  # unknown top-level fields are ignored, never an error

    async def test_extract_rejects_malformed_bot_id_in_to(self):
        from shared.errors import ApiError
        from shared.telephony_webhooks import _extract_call_details

        with pytest.raises(ApiError) as excinfo:
            await _extract_call_details(
                "freeswitch", self._request({"To": "bot_../etc/passwd"}))
        assert excinfo.value.status_code == 422

    async def test_extract_leaves_a_did_untouched(self):
        from shared.telephony_webhooks import _extract_call_details

        called, *_ = await _extract_call_details(
            "vaani", self._request({"To": "+91 80 4522 1010 "}))
        assert called == "+91 80 4522 1010 "  # exact-match contract: no normalization

    @pytest.fixture
    def resolver(self, monkeypatch):
        """resolve_bot_for_dialer with its DB helpers replaced: bot_a1/bot_a2
        in tn_a, bot_b1 in tn_b, one DID assigned to bot_a1."""
        import shared.bot_config as bc
        from shared.errors import NotFoundError

        tenants = {"bot_a1": "tn_a", "bot_a2": "tn_a", "bot_b1": "tn_b"}
        lookups = []

        def tenant_of(bot_id):
            lookups.append(("bot", bot_id))
            if bot_id not in tenants:
                raise NotFoundError("Bot")
            return tenants[bot_id]

        def assignment(number):
            lookups.append(("number", number))
            if number != "+15550001111":
                raise NotFoundError("A bot assignment for this number")
            return "bot_a1", "tn_a"

        async def resolve(bot_id, *, require_published, use_cache=True):
            return ("config", bot_id, require_published)

        monkeypatch.setattr(bc, "_bot_tenant_sync", tenant_of)
        monkeypatch.setattr(bc, "_phone_assignment_sync", assignment)
        monkeypatch.setattr(bc, "resolve_bot_config", resolve)
        return bc, lookups

    async def test_did_path_is_unchanged(self, resolver):
        bc, lookups = resolver
        assert await bc.resolve_bot_for_dialer("+15550001111") == ("config", "bot_a1", True)
        assert lookups == [("number", "+15550001111")]

    async def test_bot_id_in_to_resolves_without_number_lookup(self, resolver):
        bc, lookups = resolver
        assert await bc.resolve_bot_for_dialer("bot_a2") == ("config", "bot_a2", True)
        assert lookups == [("bot", "bot_a2")]  # phone_numbers is never queried

    async def test_bot_id_in_to_then_bot_id_selects_same_tenant_sibling(self, resolver):
        from shared.errors import NotFoundError

        bc, _ = resolver
        assert await bc.resolve_bot_for_dialer("bot_a1", "bot_a2") == ("config", "bot_a2", True)
        with pytest.raises(NotFoundError):  # cross-tenant: sanitized
            await bc.resolve_bot_for_dialer("bot_a1", "bot_b1")

    async def test_unknown_bot_id_in_to_is_not_found(self, resolver):
        from shared.errors import NotFoundError

        bc, _ = resolver
        with pytest.raises(NotFoundError):
            await bc.resolve_bot_for_dialer("bot_does_not_exist")
