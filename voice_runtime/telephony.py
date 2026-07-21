"""Telephony media-stream serializers for the voice worker's WebSocket.

Maps a provider name to the Pipecat frame serializer that speaks that
provider's media-stream wire format. Runtime-only code: the platform API
never touches media frames.
"""

from shared.errors import ApiError
from voice_runtime.serializer import RawPCMSerializer


def build_media_serializer(provider: str, *, start_message: dict | None = None):
    """Return the Pipecat frame serializer for a provider media stream.

    `start_message` is the provider's stream-start payload (already parsed),
    required by providers whose serializer needs stream identifiers.
    """
    if provider == "freeswitch":
        # mod_audio_fork ships raw L16 both ways — our RawPCM serializer fits.
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
