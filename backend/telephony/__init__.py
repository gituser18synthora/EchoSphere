"""Telephony integration: webhook verification, inbound call routing and the
provider media-stream bridges (FreeSWITCH, Twilio, Telnyx, Plivo, Exotel).

Media serializers come from Pipecat; this package adds EchoSphere's routing
(number → tenant → bot → published config), signature verification and
session issuance. Providers that need external accounts are structurally
complete and covered by provider-mocked tests.
"""
