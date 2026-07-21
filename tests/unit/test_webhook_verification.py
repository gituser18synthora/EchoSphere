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
