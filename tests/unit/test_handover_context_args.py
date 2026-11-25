"""A workflow ``handover`` node can forward call-context values with the transfer.

Opt-in via the node's ``contextArgs`` (same idea as the api node passthrough):
the dialer's per-call values — e.g. the agent number list it sent on the
webhook — ride along on the FreeSWITCH ``transfer`` message so the telephony
side can place the call. Covered end to end: engine result → brain control →
wire message, plus the guarantees that nothing is sent by default, nothing is
invented, protocol fields cannot be shadowed and values stay out of the logs.
"""

import json
import logging

import pytest
from langgraph.checkpoint.memory import MemorySaver
from pipecat.frames.frames import OutputTransportMessageUrgentFrame

import shared.orchestration.workflow_engine as wfe
from shared.bot_config import ResolvedBotConfig
from shared.orchestration.router import RouteDecision, RouteKind
from shared.orchestration.workflow_engine import _handover_context
from voice_runtime.brain import ConversationBrain
from voice_runtime.telephony import FreeSwitchAudioForkSerializer

CONTEXT = {"bdList": "7738184155", "locality": "Gurugram", "leadId": "LEAD_1",
           "blank": "   "}


class TestHandoverContextSelection:
    def test_nothing_is_forwarded_without_the_opt_in(self):
        assert _handover_context({"queue": "pristyn_bd"}, CONTEXT) == {}

    def test_list_forwards_named_values_under_their_own_names(self):
        assert _handover_context({"contextArgs": ["bdList", "leadId"]}, CONTEXT) == {
            "bdList": "7738184155", "leadId": "LEAD_1",
        }

    def test_single_string_is_accepted(self):
        assert _handover_context({"contextArgs": "bdList"}, CONTEXT) == {
            "bdList": "7738184155",
        }

    def test_mapping_renames_for_the_telephony_side(self):
        assert _handover_context({"contextArgs": {"number": "bdList"}}, CONTEXT) == {
            "number": "7738184155",
        }

    def test_absent_blank_and_non_scalar_values_are_not_sent(self):
        context = {**CONTEXT, "nested": {"a": 1}, "items": ["x"]}
        config = {"contextArgs": ["missing", "blank", "nested", "items"]}
        assert _handover_context(config, context) == {}
        assert _handover_context({"contextArgs": ["bdList"]}, None) == {}

    def test_protocol_fields_cannot_be_shadowed(self):
        config = {"contextArgs": {
            "reason": "bdList", "transfer_queue": "bdList", "agent_id": "bdList",
            "type": "bdList", "bad key!": "bdList", "ok": "bdList",
        }}
        assert _handover_context(config, CONTEXT) == {"ok": "7738184155"}

    def test_values_are_bounded(self):
        out = _handover_context({"contextArgs": ["big"]}, {"big": "9" * 500})
        assert len(out["big"]) == 200

    def test_numbers_are_sent_as_text(self):
        assert _handover_context({"contextArgs": ["n"]}, {"n": 7738184155}) == {
            "n": "7738184155",
        }


@pytest.fixture()
def engine(monkeypatch):
    eng = wfe.WorkflowEngine()

    async def _mem_checkpointer(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem_checkpointer)
    return eng


def _definition(handover_config):
    return {
        "id": "wf_handover_ctx", "version": 1, "name": "Handover ctx",
        "nodes": [
            {"id": "s", "kind": "start"},
            {"id": "h", "kind": "handover", "config": {
                "text": "Connecting you now.", "queue": "pristyn_bd",
                **handover_config}},
        ],
        "edges": [{"id": "e1", "from": "s", "to": "h"}],
    }


async def _run(engine, monkeypatch, handover_config, context_values):
    definition = _definition(handover_config)
    monkeypatch.setattr(
        wfe, "load_workflow_definition", lambda *args, **kwargs: definition
    )
    return await engine.handle_turn_detailed(
        session_id=f"hc-{abs(hash(json.dumps(handover_config, sort_keys=True)))}",
        tenant_id="tn_x", bot_id="bot_x", workflow_name="handover_ctx",
        user_text="haan", context_values=context_values,
    )


class TestEngineHandoverResult:
    async def test_default_handover_carries_only_the_queue(self, engine, monkeypatch):
        result = await _run(engine, monkeypatch, {}, CONTEXT)
        assert result["status"] == "handoff"
        assert result["handoffQueue"] == "pristyn_bd"
        assert result["handoffData"] is None

    async def test_context_args_reach_the_result(self, engine, monkeypatch):
        result = await _run(engine, monkeypatch, {"contextArgs": ["bdList"]}, CONTEXT)
        assert result["status"] == "handoff"
        assert result["handoffQueue"] == "pristyn_bd"
        assert result["handoffData"] == {"bdList": "7738184155"}
        assert result["reply"] == "Connecting you now."

    async def test_missing_value_sends_nothing(self, engine, monkeypatch):
        result = await _run(engine, monkeypatch, {"contextArgs": ["bdList"]},
                            {"locality": "Gurugram"})
        assert result["status"] == "handoff"
        assert result["handoffData"] is None


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-test"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0}

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        pass

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))

    def flush_event_soon(self, kind, **data):
        self.events.append((kind, data))


class _WorkflowStub:
    def __init__(self, result):
        self.result = result

    async def handle_turn_detailed(self, **kwargs):
        return self.result


def _brain(result) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(),
        workflow_engine=_WorkflowStub(result),
    )

    async def _push(frame, direction=None):
        pass

    async def _notify(payload):
        pass

    brain.push_frame = _push
    brain._notify_client = _notify
    return brain


def _handoff_result(**extra):
    return {
        "reply": "Connecting you now.", "done": True, "status": "handoff",
        "source": "definition", "workflowId": "wf_1", "trace": ["h"],
        "slots": {}, "handoffQueue": "pristyn_bd", **extra,
    }


class TestBrainTransferControl:
    async def test_handoff_data_rides_on_the_control_message(self):
        brain = _brain(_handoff_result(handoffData={"bdList": "7738184155"}))
        await brain._handle_workflow(
            RouteDecision(kind=RouteKind.WORKFLOW, action="handover_ctx"),
            "haan", 0.0,
        )
        assert brain._pending_controls == [{
            "type": "telephony_control", "event": "transfer",
            "reason": "workflow_handover", "transfer_queue": "pristyn_bd",
            "transfer_data": {"bdList": "7738184155"},
        }]

    async def test_no_handoff_data_keeps_the_original_control(self):
        brain = _brain(_handoff_result(handoffData=None))
        await brain._handle_workflow(
            RouteDecision(kind=RouteKind.WORKFLOW, action="handover_ctx"),
            "haan", 0.0,
        )
        assert brain._pending_controls == [{
            "type": "telephony_control", "event": "transfer",
            "reason": "workflow_handover", "transfer_queue": "pristyn_bd",
        }]


def _frame(**extra):
    return OutputTransportMessageUrgentFrame(message={
        "type": "telephony_control", "event": "transfer",
        "reason": "workflow_handover", **extra,
    })


class TestForkWireMessage:
    async def test_extra_values_are_sent_as_data_keys(self):
        wire = await FreeSwitchAudioForkSerializer().serialize(_frame(
            transfer_queue="pristyn_bd", transfer_data={"bdList": "7738184155"},
        ))
        assert json.loads(wire) == {"type": "transfer", "data": {
            "reason": "workflow_handover", "transfer_queue": "pristyn_bd",
            "bdList": "7738184155",
        }}

    async def test_message_without_extras_is_unchanged(self):
        wire = await FreeSwitchAudioForkSerializer().serialize(
            _frame(transfer_queue="pristyn_bd")
        )
        assert json.loads(wire) == {"type": "transfer", "data": {
            "reason": "workflow_handover", "transfer_queue": "pristyn_bd",
        }}

    async def test_extras_never_replace_protocol_fields(self):
        wire = await FreeSwitchAudioForkSerializer().serialize(_frame(
            transfer_queue="pristyn_bd",
            transfer_data={"reason": "x", "transfer_queue": "y", "number": "1"},
        ))
        assert json.loads(wire)["data"] == {
            "reason": "workflow_handover", "transfer_queue": "pristyn_bd",
            "number": "1",
        }

    async def test_extra_values_stay_out_of_the_logs(self, caplog):
        with caplog.at_level(logging.INFO, logger="voice_runtime.telephony"):
            wire = await FreeSwitchAudioForkSerializer().serialize(_frame(
                transfer_queue="pristyn_bd", transfer_data={"bdList": "7738184155"},
            ))
        assert "7738184155" in wire
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "freeswitch outbound transfer" in logged
        assert "bdList" in logged and "7738184155" not in logged
