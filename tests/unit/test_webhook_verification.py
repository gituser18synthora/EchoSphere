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
        out = self._sanitize({f"k{i}": "v" for i in range(50)})
        assert len(out) == 20
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
