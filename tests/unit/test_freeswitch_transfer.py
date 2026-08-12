"""FreeSWITCH transfer regression tests.

Covers the three legs of the transfer fix:
- the fork serializer actually puts the module's ``transfer`` message on the
  wire (the pre-fix serializers dropped telephony_control frames, so
  voicebot.lua's mod_audio_fork::transfer event never fired);
- the ESL event parsing used by the transfer monitor;
- the transfer lifecycle state machine — initiated/connected/completed/
  failed mapping, duplicate-event idempotency, foreign-leg isolation, and
  "a normal hangup during transfer is not a failure".
"""

import json

from pipecat.frames.frames import (
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)

import voice_runtime.freeswitch as fs
from voice_runtime.freeswitch import (
    TransferStateMachine,
    parse_event,
    valid_call_uuid,
)
from voice_runtime.telephony import (
    FreeSwitchAudioForkSerializer,
    FreeSwitchAudioStreamSerializer,
)

CALL_UUID = "1a2b3c4d-0000-1111-2222-333344445555"
AGENT_UUID = "9f8e7d6c-aaaa-bbbb-cccc-ddddeeee0000"


def _transfer_frame(urgent: bool = True, **extra):
    message = {
        "type": "telephony_control", "event": "transfer",
        "reason": "handoff", **extra,
    }
    frame_cls = (
        OutputTransportMessageUrgentFrame if urgent
        else OutputTransportMessageFrame
    )
    return frame_cls(message=message)


class TestForkTransferControl:
    async def test_transfer_control_reaches_the_wire(self):
        serializer = FreeSwitchAudioForkSerializer()
        wire = await serializer.serialize(_transfer_frame())
        assert json.loads(wire) == {
            "type": "transfer",
            "data": {"reason": "handoff"},
        }

    async def test_plain_message_frame_variant_is_also_accepted(self):
        serializer = FreeSwitchAudioForkSerializer()
        wire = await serializer.serialize(_transfer_frame(urgent=False))
        assert json.loads(wire)["type"] == "transfer"

    async def test_queue_and_agent_ride_along(self):
        serializer = FreeSwitchAudioForkSerializer()
        wire = await serializer.serialize(
            _transfer_frame(transfer_queue="collections", agent_id="a42")
        )
        data = json.loads(wire)["data"]
        assert data["transfer_queue"] == "collections"
        assert data["agent_id"] == "a42"

    async def test_duplicate_transfer_control_is_sent_once(self):
        # A retried handoff decision must not re-trigger the dialplan.
        serializer = FreeSwitchAudioForkSerializer()
        assert await serializer.serialize(_transfer_frame()) is not None
        assert await serializer.serialize(_transfer_frame()) is None

    async def test_control_hook_observes_the_transfer(self):
        serializer = FreeSwitchAudioForkSerializer()
        seen = []

        async def hook(message):
            seen.append(message)

        serializer.on_telephony_control = hook
        await serializer.serialize(_transfer_frame())
        assert len(seen) == 1 and seen[0]["event"] == "transfer"

    async def test_hook_failure_never_blocks_the_wire_message(self):
        serializer = FreeSwitchAudioForkSerializer()

        async def hook(message):
            raise RuntimeError("bookkeeping down")

        serializer.on_telephony_control = hook
        assert await serializer.serialize(_transfer_frame()) is not None

    async def test_non_control_transport_messages_are_ignored(self):
        serializer = FreeSwitchAudioForkSerializer()
        frame = OutputTransportMessageUrgentFrame(
            message={"type": "bot_text", "text": "hello"}
        )
        assert await serializer.serialize(frame) is None


class TestStreamTransferControl:
    async def test_stream_transport_records_but_cannot_wire_transfer(self):
        # mod_audio_stream has no transfer message: the control must still be
        # observable (event stream) but nothing goes on the wire.
        serializer = FreeSwitchAudioStreamSerializer()
        seen = []

        async def hook(message):
            seen.append(message)

        serializer.on_telephony_control = hook
        assert await serializer.serialize(_transfer_frame()) is None
        assert len(seen) == 1


class TestEventParsing:
    def test_url_encoded_headers_are_decoded(self):
        body = (
            "Event-Name: CHANNEL_BRIDGE\n"
            f"Unique-ID: {CALL_UUID}\n"
            "Caller-Caller-ID-Name: Ravi%20Kumar\n"
        )
        event = parse_event(body)
        assert event["Event-Name"] == "CHANNEL_BRIDGE"
        assert event["Unique-ID"] == CALL_UUID
        assert event["Caller-Caller-ID-Name"] == "Ravi Kumar"

    def test_event_body_is_preserved(self):
        body = (
            "Event-Name: CUSTOM\n"
            "Event-Subclass: mod_audio_fork::transfer\n"
            f"Unique-ID: {CALL_UUID}\n"
            "Content-Length: 19\n"
            "\n"
            '{"reason":"agent"}'
        )
        event = parse_event(body)
        assert event["Event-Subclass"] == "mod_audio_fork::transfer"
        assert event["_event_body"] == '{"reason":"agent"}'


def _ev(name, uuid=CALL_UUID, seq=None, **headers):
    event = {"Event-Name": name, "Unique-ID": uuid, **headers}
    if seq is not None:
        event["Event-Sequence"] = str(seq)
    return event


def _bridge(seq=None):
    return _ev("CHANNEL_BRIDGE", seq=seq, **{"Other-Leg-Unique-ID": AGENT_UUID})


def _hangup(cause, uuid=CALL_UUID, seq=None, **headers):
    return _ev(
        "CHANNEL_HANGUP_COMPLETE", uuid=uuid, seq=seq,
        **{"Hangup-Cause": cause, **headers},
    )


def _lua_transfer(status, seq=None, detail=None):
    headers = {
        "Event-Subclass": "echosphere::transfer",
        "Transfer-Status": status,
        "Transfer-Destination": "3001",
    }
    if detail:
        headers["Transfer-Detail"] = detail
    return _ev("CUSTOM", seq=seq, **headers)


class TestTransferStateMachine:
    def test_happy_path_initiated_connected_completed(self):
        machine = TransferStateMachine(CALL_UUID)
        assert [r["kind"] for r in machine.handle(_lua_transfer("initiated", seq=1))] \
            == ["transfer_initiated"]
        assert [r["kind"] for r in machine.handle(_bridge(seq=2))] \
            == ["transfer_connected"]
        assert machine.agent_uuid == AGENT_UUID
        assert [r["kind"] for r in machine.handle(_hangup("NORMAL_CLEARING", seq=3))] \
            == ["transfer_completed"]
        assert machine.state == "completed" and machine.terminal

    def test_module_transfer_event_marks_initiated(self):
        machine = TransferStateMachine(CALL_UUID)
        records = machine.handle(
            _ev("CUSTOM", seq=1, **{"Event-Subclass": "mod_audio_fork::transfer"})
        )
        assert [r["kind"] for r in records] == ["transfer_initiated"]
        assert machine.state == "initiated"

    def test_any_hangup_after_connect_is_completed_not_failed(self):
        machine = TransferStateMachine(CALL_UUID)
        machine.handle(_bridge(seq=1))
        records = machine.handle(_hangup("MANAGER_REQUEST", seq=2))
        assert [r["kind"] for r in records] == ["transfer_completed"]
        assert machine.state == "completed"

    def test_failure_cause_before_bridge_is_transfer_failed(self):
        machine = TransferStateMachine(CALL_UUID)
        machine.handle(_lua_transfer("initiated", seq=1))
        records = machine.handle(_hangup("NO_ANSWER", seq=2))
        assert [r["kind"] for r in records] == ["transfer_failed"]
        assert machine.state == "failed"
        assert machine.hangup_cause == "NO_ANSWER"

    def test_caller_abandon_before_bridge_is_not_a_failure(self):
        # A normal hangup caused by the caller giving up while ringing the
        # agent must never be reported as a transfer failure.
        machine = TransferStateMachine(CALL_UUID)
        machine.handle(_lua_transfer("initiated", seq=1))
        records = machine.handle(_hangup("NORMAL_CLEARING", seq=2))
        assert [r["kind"] for r in records] == ["transfer_abandoned"]
        assert machine.state == "abandoned"

    def test_dialplan_failure_event(self):
        machine = TransferStateMachine(CALL_UUID)
        machine.handle(_lua_transfer("initiated", seq=1))
        records = machine.handle(_lua_transfer("failed", seq=2, detail="-ERR no route"))
        assert records == [{
            "kind": "transfer_failed",
            "destination": "3001",
            "detail": "-ERR no route",
        }]
        assert machine.state == "failed"

    def test_duplicate_event_sequence_is_dropped(self):
        machine = TransferStateMachine(CALL_UUID)
        assert machine.handle(_bridge(seq=7)) != []
        assert machine.handle(_bridge(seq=7)) == []

    def test_replayed_bridge_with_new_sequence_is_idempotent(self):
        machine = TransferStateMachine(CALL_UUID)
        assert machine.handle(_bridge(seq=1)) != []
        assert machine.handle(_bridge(seq=2)) == []
        assert machine.state == "connected"

    def test_foreign_uuid_events_are_ignored(self):
        machine = TransferStateMachine(CALL_UUID)
        foreign = "eeeeffff-1234-5678-9abc-def012345678"
        assert machine.handle(_bridge(seq=1) | {"Unique-ID": foreign,
                                                "Other-Leg-Unique-ID": foreign}) == []
        assert machine.handle(_hangup("NO_ANSWER", uuid=foreign, seq=2)) == []
        assert machine.state == "requested"

    def test_agent_leg_hangup_is_informational_then_caller_decides(self):
        machine = TransferStateMachine(CALL_UUID)
        machine.handle(_bridge(seq=1))
        records = machine.handle(_hangup(
            "NORMAL_CLEARING", uuid=AGENT_UUID, seq=2,
            **{"Other-Leg-Unique-ID": CALL_UUID},
        ))
        assert [r["kind"] for r in records] == ["transfer_agent_leg_ended"]
        assert not machine.terminal
        records = machine.handle(_hangup("NORMAL_CLEARING", seq=3))
        assert [r["kind"] for r in records] == ["transfer_completed"]

    def test_unbridge_after_connect_is_informational(self):
        machine = TransferStateMachine(CALL_UUID)
        machine.handle(_bridge(seq=1))
        records = machine.handle(
            _ev("CHANNEL_UNBRIDGE", seq=2, **{"Other-Leg-Unique-ID": AGENT_UUID})
        )
        assert [r["kind"] for r in records] == ["transfer_unbridged"]
        assert machine.state == "connected"

    def test_terminal_state_ignores_later_events(self):
        machine = TransferStateMachine(CALL_UUID)
        machine.handle(_lua_transfer("failed", seq=1))
        assert machine.state == "failed"
        assert machine.handle(_bridge(seq=2)) == []
        assert machine.handle(_hangup("NORMAL_CLEARING", seq=3)) == []
        assert machine.state == "failed"


def test_valid_call_uuid_shapes():
    assert valid_call_uuid(CALL_UUID)
    assert valid_call_uuid("abcd1234")  # loose uuid-ish ids still pass
    assert not valid_call_uuid(None)
    assert not valid_call_uuid("")
    assert not valid_call_uuid("bad uuid; uuid_kill all")
    assert not valid_call_uuid("x" * 40)
    assert not valid_call_uuid("a" * 100)


class _StubRecorder:
    def __init__(self):
        self.events = []
        self.end_reason = None

        class _Config:
            tenant_id = "tn_test"
            bot_id = "bot_test"

        self.config = _Config()

    def flush_event_soon(self, kind, **data):
        self.events.append((kind, data))


class TestMonitorStartup:
    async def test_monitor_skipped_without_call_uuid(self):
        recorder = _StubRecorder()
        monitor = fs.start_transfer_monitor(
            session_id="vs_test_nouuid", call_uuid=None, recorder=recorder
        )
        assert monitor is None
        assert recorder.events[0][0] == "transfer_monitor_unavailable"

    async def test_monitor_fail_open_when_esl_unconfigured(self, monkeypatch):
        monkeypatch.setattr(fs, "esl_configured", lambda: False)
        recorder = _StubRecorder()
        monitor = fs.start_transfer_monitor(
            session_id="vs_test_noesl", call_uuid=CALL_UUID, recorder=recorder
        )
        assert monitor is None
        assert recorder.events[0][1]["reason"] == "event socket not configured"

    async def test_monitor_started_once_per_session(self, monkeypatch):
        monkeypatch.setattr(fs, "esl_configured", lambda: True)

        async def _noop_run(self):
            return None

        monkeypatch.setattr(fs.FreeSwitchTransferMonitor, "run", _noop_run)
        recorder = _StubRecorder()
        first = fs.start_transfer_monitor(
            session_id="vs_test_once", call_uuid=CALL_UUID, recorder=recorder
        )
        second = fs.start_transfer_monitor(
            session_id="vs_test_once", call_uuid=CALL_UUID, recorder=recorder
        )
        assert first is not None and first is second
        await first.task
        assert "vs_test_once" not in fs._active_monitors
