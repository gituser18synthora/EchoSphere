"""FreeSWITCH hangup lifecycle regression tests.

Covers the call-cut handling fix:
- normalize_disconnect: raw Hangup-Cause (+ SIP disposition) → the platform's
  disconnect vocabulary (caller/app/provider/media/transferred/unknown);
- HangupStateMachine: CHANNEL_HANGUP → CHANNEL_HANGUP_COMPLETE →
  CHANNEL_DESTROY forward-only ordering, duplicate/replay idempotency,
  foreign-leg isolation;
- FreeSwitchHangupMonitor: exactly-one finalization signal per call however
  many duplicate events FreeSWITCH delivers, transfer stand-down (an expected
  original-leg hangup during transfer is never a call failure), failure-cause
  normalization, fail-open startup;
- SessionRecorder: finalize is single-shot (billing/summary/post-call can
  never run twice) and the hangup verdict is first-writer-wins.
"""

import voice_runtime.freeswitch as fs
from shared.providers.base import ProviderError
from voice_runtime.freeswitch import (
    FreeSwitchHangupMonitor,
    HangupStateMachine,
    normalize_disconnect,
)

CALL_UUID = "1a2b3c4d-0000-1111-2222-333344445555"
AGENT_UUID = "9f8e7d6c-aaaa-bbbb-cccc-ddddeeee0000"


def _ev(name, uuid=CALL_UUID, seq=None, cause="NORMAL_CLEARING", **headers):
    event = {"Event-Name": name, "Unique-ID": uuid, **headers}
    if cause is not None:
        event["Hangup-Cause"] = cause
    if seq is not None:
        event["Event-Sequence"] = str(seq)
    return event


def _hangup(cause="NORMAL_CLEARING", seq=None, **headers):
    return _ev("CHANNEL_HANGUP", seq=seq, cause=cause, **headers)


def _complete(cause="NORMAL_CLEARING", seq=None, **headers):
    return _ev("CHANNEL_HANGUP_COMPLETE", seq=seq, cause=cause, **headers)


def _destroy(cause="NORMAL_CLEARING", seq=None, **headers):
    return _ev("CHANNEL_DESTROY", seq=seq, cause=cause, **headers)


class TestNormalizeDisconnect:
    def test_caller_sent_the_bye(self):
        assert normalize_disconnect(
            "NORMAL_CLEARING", sip_disposition="recv_bye"
        ) == "caller_hangup"

    def test_caller_cancel_while_ringing(self):
        assert normalize_disconnect(
            "ORIGINATOR_CANCEL", sip_disposition="recv_cancel"
        ) == "caller_hangup"

    def test_platform_sent_the_bye(self):
        assert normalize_disconnect(
            "NORMAL_CLEARING", sip_disposition="send_bye"
        ) == "app_hangup"

    def test_no_disposition_defaults_by_who_ended_the_session(self):
        assert normalize_disconnect("NORMAL_CLEARING") == "caller_hangup"
        assert normalize_disconnect(
            "NORMAL_CLEARING", bot_ended=True
        ) == "app_hangup"

    def test_admin_causes_are_app_hangups(self):
        assert normalize_disconnect("MANAGER_REQUEST") == "app_hangup"
        assert normalize_disconnect("SYSTEM_SHUTDOWN") == "app_hangup"

    def test_media_failures(self):
        assert normalize_disconnect("MEDIA_TIMEOUT") == "media_failure"

    def test_sip_and_platform_failures(self):
        assert normalize_disconnect("RECOVERY_ON_TIMER_EXPIRE") == "provider_failure"
        assert normalize_disconnect("USER_BUSY") == "provider_failure"
        # voicebot.lua's teardown cause when the webhook/media path broke.
        assert normalize_disconnect("NORMAL_TEMPORARY_FAILURE") == "provider_failure"

    def test_transfer_causes_and_flag(self):
        assert normalize_disconnect("BLIND_TRANSFER") == "transferred"
        # A transferred call's later hangup is never reinterpreted, whatever
        # the cause says.
        assert normalize_disconnect(
            "NORMAL_CLEARING", sip_disposition="recv_bye", transferred=True
        ) == "transferred"

    def test_missing_cause_is_unknown(self):
        assert normalize_disconnect("") == "unknown"
        assert normalize_disconnect(None) == "unknown"


class TestHangupStateMachine:
    def test_full_teardown_lifecycle(self):
        machine = HangupStateMachine(CALL_UUID)
        records = machine.handle(_hangup(
            seq=1, **{"variable_sip_hangup_disposition": "recv_bye"}
        ))
        assert [r["kind"] for r in records] == ["channel_hangup"]
        assert records[0]["cause"] == "NORMAL_CLEARING"
        assert records[0]["sip_disposition"] == "recv_bye"
        assert machine.ended and not machine.destroyed
        assert [r["kind"] for r in machine.handle(_complete(seq=2))] \
            == ["channel_hangup_complete"]
        assert [r["kind"] for r in machine.handle(_destroy(seq=3))] \
            == ["channel_destroyed"]
        assert machine.destroyed

    def test_duplicate_event_sequence_is_dropped(self):
        machine = HangupStateMachine(CALL_UUID)
        assert machine.handle(_hangup(seq=7)) != []
        assert machine.handle(_hangup(seq=7)) == []

    def test_replayed_hangup_with_new_sequence_is_a_noop(self):
        machine = HangupStateMachine(CALL_UUID)
        assert machine.handle(_hangup(seq=1)) != []
        assert machine.handle(_hangup(seq=2)) == []
        assert machine.state == "hung_up"

    def test_out_of_order_earlier_stage_is_ignored(self):
        machine = HangupStateMachine(CALL_UUID)
        assert machine.handle(_complete(seq=1)) != []
        assert machine.handle(_hangup(seq=2)) == []
        assert machine.state == "hangup_complete"

    def test_foreign_uuid_events_are_ignored(self):
        machine = HangupStateMachine(CALL_UUID)
        assert machine.handle(_hangup(uuid=AGENT_UUID, seq=1)) == []
        assert not machine.ended

    def test_destroy_alone_still_ends_the_call(self):
        # A monitor that subscribed late (or missed events) must still
        # terminate; CHANNEL_DESTROY carries the cause as a variable.
        machine = HangupStateMachine(CALL_UUID)
        records = machine.handle(_ev(
            "CHANNEL_DESTROY", seq=1, cause=None,
            **{"variable_hangup_cause": "NORMAL_CLEARING"},
        ))
        assert [r["kind"] for r in records] == ["channel_destroyed"]
        assert machine.ended and machine.destroyed
        assert machine.cause == "NORMAL_CLEARING"

    def test_first_cause_wins_but_each_record_keeps_its_own(self):
        machine = HangupStateMachine(CALL_UUID)
        machine.handle(_hangup(cause="ORIGINATOR_CANCEL", seq=1))
        records = machine.handle(_complete(cause="NORMAL_CLEARING", seq=2))
        assert machine.cause == "ORIGINATOR_CANCEL"
        assert records[0]["cause"] == "NORMAL_CLEARING"

    def test_bridge_uuid_is_captured_when_available(self):
        machine = HangupStateMachine(CALL_UUID)
        records = machine.handle(_hangup(
            seq=1, **{"Other-Leg-Unique-ID": AGENT_UUID}
        ))
        assert records[0]["other_leg_uuid"] == AGENT_UUID.lower()
        assert machine.other_leg_uuid == AGENT_UUID.lower()

    def test_fs_timestamp_rides_along(self):
        machine = HangupStateMachine(CALL_UUID)
        records = machine.handle(_hangup(
            seq=1, **{"Event-Date-Timestamp": "1765000000000000"}
        ))
        assert records[0]["fs_timestamp_us"] == "1765000000000000"


class _StubRecorder:
    def __init__(self):
        self.events = []
        self.end_reason = None
        self.transferred = False
        self.hangup = None
        self.set_hangup_calls = 0

        class _Config:
            tenant_id = "tn_test"
            bot_id = "bot_test"

        self.config = _Config()

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))

    def flush_event_soon(self, kind, **data):
        self.events.append((kind, data))

    def set_hangup(self, info):
        self.set_hangup_calls += 1
        if self.hangup is not None:
            return False
        self.hangup = dict(info)
        return True

    def kinds(self):
        return [kind for kind, _ in self.events]


class _ScriptedESL:
    """Feeds a fixed event script; idle (None) once exhausted."""

    def __init__(self, events, *, then_disconnect=False):
        self._events = list(events)
        self._then_disconnect = then_disconnect

    async def next_event(self, timeout):
        if self._events:
            return self._events.pop(0)
        if self._then_disconnect:
            raise ProviderError("freeswitch", "upstream", "event socket closed")
        return None


def _monitor(recorder, on_hangup=None):
    return FreeSwitchHangupMonitor(
        session_id="vs_test_hangup", call_uuid=CALL_UUID,
        recorder=recorder, on_hangup=on_hangup,
    )


class TestHangupMonitor:
    async def test_caller_hangup_ends_the_call_once(self):
        recorder = _StubRecorder()
        seen = []

        async def on_hangup(info):
            seen.append(info)

        monitor = _monitor(recorder, on_hangup)
        await monitor._watch(_ScriptedESL([
            _hangup(seq=1, **{"variable_sip_hangup_disposition": "recv_bye"}),
            _complete(seq=2),
            _destroy(seq=3),
        ]))
        assert recorder.kinds() == [
            "channel_hangup", "freeswitch_hangup",
            "channel_hangup_complete", "channel_destroyed",
        ]
        assert len(seen) == 1
        assert seen[0]["reason"] == "caller_hangup"
        assert recorder.hangup["cause"] == "NORMAL_CLEARING"
        assert recorder.hangup["reason"] == "caller_hangup"

    async def test_duplicate_events_never_finalize_twice(self):
        recorder = _StubRecorder()
        seen = []

        async def on_hangup(info):
            seen.append(info)

        monitor = _monitor(recorder, on_hangup)
        await monitor._watch(_ScriptedESL([
            _hangup(seq=1),
            _hangup(seq=1),           # exact duplicate delivery
            _hangup(seq=2),           # replay with a new sequence
            _complete(seq=3),
            _hangup(seq=4),           # out-of-order replay after complete
            _destroy(seq=5),
        ]))
        assert len(seen) == 1
        assert recorder.set_hangup_calls == 1
        assert recorder.kinds().count("channel_hangup") == 1
        assert recorder.kinds().count("freeswitch_hangup") == 1

    async def test_application_hangup_is_classified(self):
        # The bot ended the call: the session finalized first, then the
        # platform's uuid_kill cleared the channel (FS sends the BYE).
        recorder = _StubRecorder()
        recorder.end_reason = "completed"
        seen = []

        async def on_hangup(info):
            seen.append(info)

        monitor = _monitor(recorder, on_hangup)
        await monitor._watch(_ScriptedESL([
            _hangup(seq=1, **{"variable_sip_hangup_disposition": "send_bye"}),
            _complete(seq=2),
            _destroy(seq=3),
        ]))
        assert recorder.hangup["reason"] == "app_hangup"
        assert len(seen) == 1  # the host's end-call hook no-ops on its own

    async def test_provider_failure_is_classified(self):
        recorder = _StubRecorder()
        monitor = _monitor(recorder)
        await monitor._watch(_ScriptedESL([
            _hangup(cause="RECOVERY_ON_TIMER_EXPIRE", seq=1),
            _destroy(cause="RECOVERY_ON_TIMER_EXPIRE", seq=2),
        ]))
        assert recorder.hangup["reason"] == "provider_failure"
        assert recorder.hangup["cause"] == "RECOVERY_ON_TIMER_EXPIRE"

    async def test_media_failure_is_classified(self):
        recorder = _StubRecorder()
        monitor = _monitor(recorder)
        await monitor._watch(_ScriptedESL([
            _hangup(cause="MEDIA_TIMEOUT", seq=1),
            _destroy(cause="MEDIA_TIMEOUT", seq=2),
        ]))
        assert recorder.hangup["reason"] == "media_failure"

    async def test_monitor_stands_down_on_transfer(self):
        # Once the transfer control reached the wire the transfer monitor
        # owns the lifecycle; the original leg's later hangup must not be
        # reported as this call failing.
        recorder = _StubRecorder()
        recorder.transferred = True
        seen = []

        async def on_hangup(info):
            seen.append(info)

        monitor = _monitor(recorder, on_hangup)
        await monitor._watch(_ScriptedESL([_hangup(seq=1)]))
        assert recorder.kinds() == ["hangup_monitor_stood_down"]
        assert seen == []
        assert recorder.hangup is None

    async def test_transfer_cause_hangup_is_not_a_failure(self):
        # A leg terminated BY a transfer (uuid_transfer race) normalizes to
        # "transferred" even when the recorder flag never got set.
        recorder = _StubRecorder()
        monitor = _monitor(recorder)
        await monitor._watch(_ScriptedESL([
            _hangup(cause="BLIND_TRANSFER", seq=1),
            _destroy(cause="BLIND_TRANSFER", seq=2),
        ]))
        assert recorder.hangup["reason"] == "transferred"

    async def test_esl_disconnect_is_recorded_and_fail_open(self):
        recorder = _StubRecorder()
        monitor = _monitor(recorder)
        await monitor._watch(_ScriptedESL([], then_disconnect=True))
        assert recorder.kinds() == ["hangup_monitor_disconnected"]

    async def test_hangup_after_finalize_still_reaches_the_record(self):
        # Bot-ended call: finalize already ran when uuid_kill's events land.
        # The verdict is still captured (recorder.set_hangup + direct Mongo
        # writes — the latter fail open without a Mongo connection here).
        recorder = _StubRecorder()
        recorder.end_reason = "completed"
        monitor = _monitor(recorder)
        await monitor._watch(_ScriptedESL([
            _hangup(seq=1, **{"variable_sip_hangup_disposition": "send_bye"}),
            _destroy(seq=2),
        ]))
        assert recorder.hangup["reason"] == "app_hangup"
        # end_reason was already set, so events bypass the recorder — the
        # in-memory stream stays clean of post-finalize noise.
        assert recorder.events == []


class TestMonitorStartup:
    async def test_monitor_skipped_without_call_uuid(self):
        recorder = _StubRecorder()
        monitor = fs.start_hangup_monitor(
            session_id="vs_hangup_nouuid", call_uuid=None, recorder=recorder
        )
        assert monitor is None
        assert recorder.events[0][0] == "hangup_monitor_unavailable"

    async def test_monitor_fail_open_when_esl_unconfigured(self, monkeypatch):
        monkeypatch.setattr(fs, "esl_configured", lambda: False)
        recorder = _StubRecorder()
        monitor = fs.start_hangup_monitor(
            session_id="vs_hangup_noesl", call_uuid=CALL_UUID, recorder=recorder
        )
        assert monitor is None
        assert recorder.events[0][1]["reason"] == "event socket not configured"

    async def test_monitor_started_once_per_session(self, monkeypatch):
        monkeypatch.setattr(fs, "esl_configured", lambda: True)

        async def _noop_run(self):
            return None

        monkeypatch.setattr(fs.FreeSwitchHangupMonitor, "run", _noop_run)
        recorder = _StubRecorder()
        first = fs.start_hangup_monitor(
            session_id="vs_hangup_once", call_uuid=CALL_UUID, recorder=recorder
        )
        second = fs.start_hangup_monitor(
            session_id="vs_hangup_once", call_uuid=CALL_UUID, recorder=recorder
        )
        assert first is not None and first is second
        await first.task
        assert "vs_hangup_once" not in fs._active_hangup_monitors


class TestSingleFinalization:
    def _recorder(self):
        from voice_runtime.recording import SessionRecorder

        class _Config:
            tenant_id = "tn_test"
            bot_id = "bot_test"
            language = "hi-IN"
            version = 1
            prompt_id = None
            prompt_version = None
            prompt_mode = None

        return SessionRecorder("vs_hangup_final", _Config(), channel="freeswitch")

    async def test_finalize_runs_exactly_once(self, monkeypatch):
        recorder = self._recorder()
        control_plane_writes = []
        monkeypatch.setattr(
            recorder, "_write_control_plane_row",
            lambda duration, reason: control_plane_writes.append(reason),
        )
        monkeypatch.setattr(
            "shared.post_call.processor.enqueue_post_call", lambda r: False
        )
        await recorder.finalize(reason="caller_hangup")
        await recorder.finalize(reason="worker_shutdown")  # duplicate teardown
        assert control_plane_writes == ["caller_hangup"]
        assert recorder.end_reason == "caller_hangup"

    async def test_hangup_verdict_is_first_writer_wins(self):
        recorder = self._recorder()
        assert recorder.set_hangup({"cause": "NORMAL_CLEARING",
                                    "reason": "caller_hangup"}) is True
        assert recorder.set_hangup({"cause": "MEDIA_TIMEOUT",
                                    "reason": "media_failure"}) is False
        assert recorder.hangup["reason"] == "caller_hangup"
