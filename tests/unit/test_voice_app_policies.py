"""Call-level policies in the voice worker app.

- Telephony output rate: every telephony serializer speaks fixed L16@8k
  (the FreeSWITCH streamAudio envelope literally declares sampleRate 8000),
  so a bot configured with any other telephony rate would play at the wrong
  speed on the caller's phone. The app must force 8 kHz.
- Session timeout: pipecat's session_timeout is an ABSOLUTE one-shot timer,
  not an inactivity timeout — it must never drop a live conversation.
"""

import logging

from voice_runtime.app import (
    TELEPHONY_SAMPLE_RATE,
    resolve_telephony_sample_rate,
    session_timeout_should_cancel,
)


class _RecorderStub:
    def __init__(self, turns):
        self.turns = turns


class TestTelephonySampleRate:
    def test_default_is_8k(self):
        assert resolve_telephony_sample_rate({}) == TELEPHONY_SAMPLE_RATE == 8000

    def test_configured_8k_is_kept(self):
        assert resolve_telephony_sample_rate({"sampleRate": 8000}) == 8000

    def test_other_rates_are_clamped_with_warning(self, caplog):
        # 16k config + 8k envelope = half-speed playback; must be impossible.
        with caplog.at_level(logging.WARNING, logger="voice_runtime.app"):
            assert resolve_telephony_sample_rate(
                {"sampleRate": 16000}, bot_id="bot-x", provider="freeswitch"
            ) == 8000
        assert any("16000" in r.getMessage() for r in caplog.records)

    def test_string_rate_from_json_is_handled(self):
        assert resolve_telephony_sample_rate({"sampleRate": "24000"}) == 8000


class TestSessionTimeoutPolicy:
    def test_dead_session_without_turns_is_cancelled(self):
        assert session_timeout_should_cancel(_RecorderStub([])) is True

    def test_live_call_with_turns_is_never_cancelled(self):
        assert session_timeout_should_cancel(_RecorderStub([object()])) is False
