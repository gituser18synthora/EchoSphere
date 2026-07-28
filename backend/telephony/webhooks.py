"""Webhook signature verification with replay protection.

Two schemes:
- Twilio: HMAC-SHA1 over URL + sorted POST params, base64, `X-Twilio-Signature`.
- Generic (Exotel/Plivo/Telnyx/Vaani/FreeSWITCH event socket bridges): HMAC-SHA256
  over `<timestamp>.<raw body>` with `X-Webhook-Signature` + `X-Webhook-Timestamp`.

Replay protection: signatures are single-use within their validity window
(Redis SETNX), and timestamps older than MAX_SKEW are rejected.
"""

import base64
import hashlib
import hmac
import logging
import time

from shared.config import get_settings
from shared.errors import ApiError

logger = logging.getLogger(__name__)

MAX_SKEW_SECONDS = 300


class WebhookVerificationError(ApiError):
    def __init__(self, message: str = "Invalid webhook signature") -> None:
        super().__init__(message, status_code=403)


def _secret() -> str:
    settings = get_settings()
    secret = settings.resolve_secret(settings.telephony_webhook_secret_reference)
    if not secret:
        raise WebhookVerificationError("Webhook secret is not configured")
    return secret


def verify_generic_signature(
    *, body: bytes, signature: str, timestamp: str, secret: str | None = None
) -> None:
    """HMAC-SHA256(`<ts>.<body>`) — constant-time compare + freshness check."""
    if not signature or not timestamp:
        raise WebhookVerificationError("Missing signature headers")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("Invalid timestamp") from exc
    if abs(time.time() - ts) > MAX_SKEW_SECONDS:
        raise WebhookVerificationError("Webhook timestamp outside validity window")
    key = (secret or _secret()).encode()
    expected = hmac.new(key, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise WebhookVerificationError()


def verify_twilio_signature(
    *, url: str, params: dict[str, str], signature: str, auth_token: str
) -> None:
    """Twilio's documented scheme: HMAC-SHA1(url + sorted k+v), base64."""
    if not signature:
        raise WebhookVerificationError("Missing X-Twilio-Signature")
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    if not hmac.compare_digest(expected, signature.strip()):
        raise WebhookVerificationError()


async def check_replay(signature: str, window_seconds: int = MAX_SKEW_SECONDS * 2) -> None:
    """Reject a signature that was already accepted (single-use)."""
    from shared.db.redis import get_redis

    key = f"webhook:seen:{hashlib.sha256(signature.encode()).hexdigest()}"
    try:
        fresh = await get_redis().set(key, "1", nx=True, ex=window_seconds)
    except Exception:  # noqa: BLE001 - Redis outage: fail open but log loudly
        logger.error("replay-protection store unavailable; accepting webhook unchecked")
        return
    if not fresh:
        raise WebhookVerificationError("Webhook replay detected")
