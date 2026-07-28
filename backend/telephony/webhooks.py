"""Compatibility shim — the implementation moved to shared.telephony_webhooks
so the voice worker's telephony gateway can serve the same signed webhook."""

from shared.telephony_webhooks import (  # noqa: F401
    MAX_SKEW_SECONDS,
    WebhookVerificationError,
    check_replay,
    verify_generic_signature,
    verify_twilio_signature,
)
