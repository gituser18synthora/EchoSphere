"""Realtime voice worker — a separate service from the platform API.

Owns everything that happens *during* a call: WebSocket audio transport,
the Pipecat pipeline (VAD → turn taking → STT → brain → TTS), barge-in and
cancellation, telephony media serializers, call recording and FreeSWITCH
call control. It holds no authority over authentication or tenancy — it
only accepts session ids the API has already written to Redis.

Run: env/bin/python -m voice_runtime.app
"""
