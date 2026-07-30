"""Control-plane telephony: webhook signature verification and replay
protection for inbound-call routing (number → tenant → bot → session).

The provider catalog/connect-instruction contract lives in
``shared/telephony.py``; media-stream serializers are runtime-only and live
in ``voice_runtime/telephony.py``.
"""
