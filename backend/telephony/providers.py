"""Telephony provider adapters: config schemas, credential validation and the
media-serializer factory used by the voice worker's telephony WebSocket.

No fake integrations: each adapter validates configuration and produces the
provider's real connect instructions; live traffic additionally requires the
provider account credentials referenced in the config.
"""

from dataclasses import dataclass
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field

from backend.core.errors import ApiError


class TelephonyProviderConfig(BaseModel):
    """Typed per-tenant/bot telephony configuration (secrets are references)."""

    provider: str  # freeswitch | twilio | telnyx | plivo | exotel
    account_sid_reference: str = ""
    auth_token_reference: str = ""
    public_ws_base: str = ""  # wss host the provider streams media to
    sample_rate: int = Field(default=8000, ge=8000, le=48000)
    extra: dict = Field(default_factory=dict)

    def validate_for(self, provider: str) -> None:
        if self.provider != provider:
            raise ApiError(f"Configuration is for '{self.provider}', not '{provider}'", 400)
        if provider in ("twilio", "telnyx", "plivo", "exotel") and not self.public_ws_base:
            raise ApiError("public_ws_base (wss://…) is required for media streaming", 400)
        if provider == "twilio" and not self.auth_token_reference:
            raise ApiError("auth_token_reference is required for Twilio signature checks", 400)


@dataclass
class ConnectInstructions:
    """What we answer an inbound-call webhook with."""

    content_type: str
    body: str


SUPPORTED_PROVIDERS = ("freeswitch", "twilio", "telnyx", "plivo", "exotel")


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


def build_media_serializer(provider: str, *, start_message: dict | None = None):
    """Return the Pipecat frame serializer for a provider media stream.

    `start_message` is the provider's stream-start payload (already parsed),
    required by providers whose serializer needs stream identifiers.
    """
    if provider == "freeswitch":
        # mod_audio_fork ships raw L16 both ways — our RawPCM serializer fits.
        from backend.voice_runtime.serializer import RawPCMSerializer

        return RawPCMSerializer(input_sample_rate=8000)
    if provider == "twilio":
        from pipecat.serializers.twilio import TwilioFrameSerializer

        start = (start_message or {}).get("start", {})
        stream_sid = start.get("streamSid") or (start_message or {}).get("streamSid")
        if not stream_sid:
            raise ApiError("Twilio stream start message missing streamSid", 400)
        return TwilioFrameSerializer(
            stream_sid=stream_sid,
            call_sid=start.get("callSid"),
        )
    if provider == "telnyx":
        from pipecat.serializers.telnyx import TelnyxFrameSerializer

        start = (start_message or {}).get("start", {})
        stream_id = start.get("stream_id") or (start_message or {}).get("stream_id")
        if not stream_id:
            raise ApiError("Telnyx stream start message missing stream_id", 400)
        return TelnyxFrameSerializer(
            stream_id=stream_id,
            call_control_id=start.get("call_control_id"),
            outbound_encoding=start.get("media_format", {}).get("encoding", "PCMU"),
        )
    if provider == "plivo":
        from pipecat.serializers.plivo import PlivoFrameSerializer

        start = (start_message or {}).get("start", {})
        stream_id = start.get("streamId") or (start_message or {}).get("streamId")
        if not stream_id:
            raise ApiError("Plivo stream start message missing streamId", 400)
        return PlivoFrameSerializer(stream_id=stream_id, call_id=start.get("callId"))
    if provider == "exotel":
        from pipecat.serializers.exotel import ExotelFrameSerializer

        start = (start_message or {}).get("start", {})
        stream_sid = start.get("stream_sid") or (start_message or {}).get("stream_sid")
        if not stream_sid:
            raise ApiError("Exotel stream start message missing stream_sid", 400)
        return ExotelFrameSerializer(stream_sid=stream_sid)
    raise ApiError(f"Unsupported telephony provider '{provider}'", 400)
