"""Telephony provider catalog and connect-instruction contract.

Shared between the two services: the API's inbound-call webhook answers with
``connect_instructions`` (which embeds the voice worker's WebSocket URL), and
the voice worker validates stream requests against ``SUPPORTED_PROVIDERS``.
Media-frame serializers are runtime-only and live in
``voice_runtime/telephony.py``.

No fake integrations: each adapter validates configuration and produces the
provider's real connect instructions; live traffic additionally requires the
provider account credentials referenced in the config.
"""

from dataclasses import dataclass
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field

from shared.errors import ApiError


class TelephonyProviderConfig(BaseModel):
    """Typed per-tenant/bot telephony configuration (secrets are references)."""

    provider: str  # freeswitch | twilio | telnyx | plivo | exotel | vaani
    account_sid_reference: str = ""
    auth_token_reference: str = ""
    public_ws_base: str = ""  # wss host the provider streams media to
    sample_rate: int = Field(default=8000, ge=8000, le=48000)
    extra: dict = Field(default_factory=dict)

    def validate_for(self, provider: str) -> None:
        if self.provider != provider:
            raise ApiError(f"Configuration is for '{self.provider}', not '{provider}'", 400)
        if provider in ("twilio", "telnyx", "plivo", "exotel", "vaani") and not self.public_ws_base:
            raise ApiError("public_ws_base (wss://…) is required for media streaming", 400)
        if provider == "twilio" and not self.auth_token_reference:
            raise ApiError("auth_token_reference is required for Twilio signature checks", 400)


@dataclass
class ConnectInstructions:
    """What we answer an inbound-call webhook with."""

    content_type: str
    body: str


SUPPORTED_PROVIDERS = ("freeswitch", "twilio", "telnyx", "plivo", "exotel", "vaani")


def connect_instructions(
    provider: str, config: TelephonyProviderConfig, session_id: str
) -> ConnectInstructions:
    """Build the provider-specific 'answer and stream media to us' response."""
    config.validate_for(provider)
    ws_url = f"{config.public_ws_base.rstrip('/')}/ws/telephony/{provider}/{session_id}"

    if provider == "twilio":
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Connect><Stream url="{escape(ws_url)}" /></Connect></Response>'
        )
        return ConnectInstructions("application/xml", twiml)
    if provider == "plivo":
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Stream keepCallAlive="true" bidirectional="true" '
            f'contentType="audio/x-l16;rate=8000">{escape(ws_url)}</Stream></Response>'
        )
        return ConnectInstructions("application/xml", xml)
    if provider == "exotel":
        # Exotel Voicebot applet expects the WS URL as plain text/JSON.
        return ConnectInstructions("application/json", f'{{"url": "{ws_url}"}}')
    if provider == "vaani":
        # Vaani Telephony connects to the bot's bidirectional media WebSocket.
        return ConnectInstructions("application/json", f'{{"url": "{ws_url}"}}')
    if provider == "telnyx":
        # Returned to the TeXML/Call Control handler that answers with stream start.
        return ConnectInstructions(
            "application/json",
            f'{{"stream_url": "{ws_url}", "stream_track": "inbound_track"}}',
        )
    if provider == "freeswitch":
        # Consumed by our dialplan helper: mod_audio_fork target.
        return ConnectInstructions("application/json", f'{{"audio_fork_url": "{ws_url}"}}')
    raise ApiError(f"Unsupported telephony provider '{provider}'", 400)
