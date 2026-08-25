"""Workflow response-delivery modes (fixed | exact | llm_grounded).

The deterministic engine still owns state, branching, slots and tool
execution; a node's configured mode decides only how its authored reply is
delivered. Pinned here:
- absent/unknown configuration resolves to fixed → existing workflows keep
  byte-identical behavior;
- exact wins any mixed turn (approved wording is never paraphrased);
- grounded nodes carry their response directives/must-include literals to
  the caller (brain/testing) while the authored text stays the fallback;
- handover confirmations stay deterministic whatever the config says;
- an API failure edge can never reach a grounded success node — the success
  claim exists only on the deterministically-guaranteed branch;
- the structural validation gate for generated grounded replies.
"""

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as wfe
from shared.orchestration.phrases import canned
from shared.orchestration.response_modes import (
    RESPONSE_MODE_EXACT,
    RESPONSE_MODE_FIXED,
    RESPONSE_MODE_GROUNDED,
    aggregate_response_mode,
    grounded_delivery_instruction,
    node_response_mode,
    validate_grounded_reply,
)


class TestModeResolution:
    def test_absent_and_unknown_configs_are_fixed(self):
        assert node_response_mode(None) == RESPONSE_MODE_FIXED
        assert node_response_mode({}) == RESPONSE_MODE_FIXED
        assert node_response_mode({"responseMode": "creative"}) == RESPONSE_MODE_FIXED
        assert node_response_mode({"responseMode": 42}) == RESPONSE_MODE_FIXED

    def test_declared_modes_resolve(self):
        assert node_response_mode({"responseMode": "exact"}) == RESPONSE_MODE_EXACT
        assert node_response_mode({"responseMode": "LLM_Grounded"}) == RESPONSE_MODE_GROUNDED
        assert node_response_mode({"responseMode": " fixed "}) == RESPONSE_MODE_FIXED

    def test_aggregation_exact_beats_grounded_beats_fixed(self):
        assert aggregate_response_mode([]) == RESPONSE_MODE_FIXED
        assert aggregate_response_mode(["fixed", "fixed"]) == RESPONSE_MODE_FIXED
        assert aggregate_response_mode(["fixed", "llm_grounded"]) == RESPONSE_MODE_GROUNDED
        assert aggregate_response_mode(["llm_grounded", "exact"]) == RESPONSE_MODE_EXACT


class TestValidateGroundedReply:
    SCRIPT = "Done! I've emailed your booking voucher. Anything else I can help with?"

    def test_empty_or_bloated_fails(self):
        assert not validate_grounded_reply(self.SCRIPT, "")
        assert not validate_grounded_reply(self.SCRIPT, "word " * 200)

    def test_pending_question_must_survive(self):
        assert not validate_grounded_reply(
            self.SCRIPT, "Your voucher has been emailed.", require_question=True
        )
        assert validate_grounded_reply(
            self.SCRIPT, "Your voucher is on its way — anything else?",
            require_question=True,
        )

    def test_script_digits_must_survive(self):
        script = "Your booking 601001 is confirmed."
        assert not validate_grounded_reply(script, "Booking 601002 is confirmed.")
        assert validate_grounded_reply(script, "Great news — booking 601001 is confirmed.")

    def test_must_include_literals_case_insensitive(self):
        assert validate_grounded_reply(
            self.SCRIPT, "I have emailed your Booking Voucher just now.",
            must_include=["booking voucher"],
        )
        assert not validate_grounded_reply(
            self.SCRIPT, "It has been sent.", must_include=["voucher"]
        )

    def test_markdown_and_menus_rejected(self):
        assert not validate_grounded_reply(self.SCRIPT, "Here you go:\n- hotel\n- dates")
        assert not validate_grounded_reply(self.SCRIPT, "# Booking\nAll confirmed.")
        assert not validate_grounded_reply(self.SCRIPT, "Your **voucher** was sent.")
        assert not validate_grounded_reply(self.SCRIPT, "Options:\n1. details\n2. voucher")

    def test_language_check_is_honored_and_fails_closed(self):
        assert validate_grounded_reply(
            self.SCRIPT, "वाउचर भेज दिया गया है।", "hi-IN",
            language_check=lambda text, lang: True,
        )
        assert not validate_grounded_reply(
            self.SCRIPT, "Sent.", "hi-IN", language_check=lambda text, lang: False,
        )
        def _broken(text, lang):
            raise ValueError("boom")
        assert not validate_grounded_reply(
            self.SCRIPT, "Sent it.", "hi-IN", language_check=_broken,
        )

    def test_instruction_carries_goals_script_and_pending_question(self):
        instruction = grounded_delivery_instruction(
            directives=["Tell the caller the voucher was emailed."],
            script=self.SCRIPT,
            pending_question="Is there anything else I can help you with?",
        )
        assert "Tell the caller the voucher was emailed." in instruction
        assert self.SCRIPT in instruction
        assert "MUST end by asking" in instruction
        assert "Never claim an action succeeded" in instruction


# ── engine integration: modes flow from node config to the turn result ──────


@pytest.fixture()
def engine(monkeypatch):
    eng = wfe.WorkflowEngine()

    async def _mem_checkpointer(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem_checkpointer)
    return eng


def _use_definition(monkeypatch, definition):
    monkeypatch.setattr(
        wfe, "load_workflow_definition", lambda tenant_id, bot_id, name: definition
    )


async def _turn(engine, text, session, name="modes_flow", **kwargs):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_x", bot_id="bot_x",
        workflow_name=name, user_text=text, **kwargs,
    )


GROUNDED_FLOW = {
    "id": "wf_modes", "version": 1, "name": "Modes flow",
    "nodes": [
        {"id": "start", "kind": "start", "label": "Start"},
        {"id": "ack", "kind": "message", "label": "Ack",
         "config": {"text": "Your booking is confirmed in our system.",
                    "responseMode": "llm_grounded",
                    "responseDirective": "Confirm the booking is confirmed.",
                    "responseMustInclude": ["confirmed"]}},
        {"id": "hub", "kind": "intent", "label": "Hub",
         "config": {"prompt": "Details or voucher?"}},
        {"id": "details", "kind": "message", "label": "Details",
         "config": {"text": "Here are your details."}},
        {"id": "end", "kind": "end", "label": "End"},
    ],
    "edges": [
        {"id": "e1", "from": "start", "to": "ack"},
        {"id": "e2", "from": "ack", "to": "hub"},
        {"id": "e3", "from": "hub", "to": "details", "label": "details"},
        {"id": "e4", "from": "details", "to": "end"},
    ],
}


class TestEngineModes:
    async def test_unconfigured_nodes_keep_fixed_mode(self, engine, monkeypatch):
        definition = {
            "id": "wf_plain", "version": 1, "name": "Plain flow",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "msg", "kind": "message", "label": "Msg",
                 "config": {"text": "Authored text."}},
                {"id": "end", "kind": "end", "label": "End",
                 "config": {"text": "Bye!"}},
            ],
            "edges": [
                {"id": "e1", "from": "start", "to": "msg"},
                {"id": "e2", "from": "msg", "to": "end"},
            ],
        }
        _use_definition(monkeypatch, definition)
        result = await _turn(engine, "hi", "m-fixed", name="plain_flow")
        assert result["responseMode"] == "fixed"
        assert result["responseDirectives"] == []
        assert result["reply"] == "Authored text. Bye!"

    async def test_grounded_segment_makes_the_turn_grounded(self, engine, monkeypatch):
        """A grounded message followed by a fixed intent prompt: the turn is
        grounded, the pending question is reported, and the authored
        concatenation stays available as the spoken fallback."""
        _use_definition(monkeypatch, GROUNDED_FLOW)
        result = await _turn(engine, "hello", "m-grounded")
        assert result["responseMode"] == "llm_grounded"
        assert result["responseDirectives"] == ["Confirm the booking is confirmed."]
        assert result["responseMustInclude"] == ["confirmed"]
        assert result["nodePrompt"] == "Details or voucher?"
        assert result["reply"] == (
            "Your booking is confirmed in our system. Details or voucher?"
        )

    async def test_followup_turn_does_not_inherit_grounded_mode(
        self, engine, monkeypatch
    ):
        """Modes are per-turn: the next (fixed) step must not inherit the
        previous turn's grounded contract from the checkpointed state."""
        _use_definition(monkeypatch, GROUNDED_FLOW)
        await _turn(engine, "hello", "m-next")
        result = await _turn(engine, "details please", "m-next")
        assert result["reply"] == "Here are your details."
        assert result["responseMode"] == "fixed"
        assert result["responseDirectives"] == []

    async def test_exact_wins_a_mixed_turn(self, engine, monkeypatch):
        definition = {
            "id": "wf_exact", "version": 1, "name": "Exact flow",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "ack", "kind": "message", "label": "Ack",
                 "config": {"text": "One moment.",
                            "responseMode": "llm_grounded"}},
                {"id": "legal", "kind": "message", "label": "Legal",
                 "config": {"text": "Never share your OTP or PIN on this call.",
                            "responseMode": "exact"}},
                {"id": "end", "kind": "end", "label": "End"},
            ],
            "edges": [
                {"id": "e1", "from": "start", "to": "ack"},
                {"id": "e2", "from": "ack", "to": "legal"},
                {"id": "e3", "from": "legal", "to": "end"},
            ],
        }
        _use_definition(monkeypatch, definition)
        result = await _turn(engine, "hi", "m-exact", name="exact_flow")
        assert result["responseMode"] == "exact"
        assert "Never share your OTP" in result["reply"]

    async def test_handover_ignores_grounded_config(self, engine, monkeypatch):
        definition = {
            "id": "wf_handover", "version": 1, "name": "Handover flow",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "agent", "kind": "handover", "label": "Agent",
                 "config": {"text": "Transferring you now — please hold.",
                            "queue": "support",
                            "responseMode": "llm_grounded",
                            "responseDirective": "Say goodbye creatively."}},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "agent"}],
        }
        _use_definition(monkeypatch, definition)
        result = await _turn(engine, "agent please", "m-handover",
                             name="handover_flow")
        assert result["status"] == "handoff"
        assert result["responseMode"] == "fixed"
        assert result["responseDirectives"] == []
        assert result["reply"] == "Transferring you now — please hold."

    async def test_api_failure_can_never_reach_the_grounded_success_claim(
        self, engine, monkeypatch
    ):
        """The success wording exists ONLY on the success edge: a failed tool
        result routes to the fixed failure message, so no generated reply can
        ever claim the voucher was sent."""
        definition = {
            "id": "wf_api", "version": 1, "name": "Api flow",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "send", "kind": "api", "label": "Voucher API",
                 "config": {"connection": "Voucher Sender"}},
                {"id": "ok", "kind": "message", "label": "Sent",
                 "config": {"text": "Your voucher has been emailed.",
                            "responseMode": "llm_grounded",
                            "responseDirective": "Confirm the voucher email succeeded."}},
                {"id": "fail", "kind": "message", "label": "Failed",
                 "config": {"text": "I couldn't send the voucher right now."}},
                {"id": "end", "kind": "end", "label": "End"},
            ],
            "edges": [
                {"id": "e1", "from": "start", "to": "send"},
                {"id": "e2", "from": "send", "to": "ok", "label": "success"},
                {"id": "e3", "from": "send", "to": "fail", "label": "failure"},
                {"id": "e4", "from": "ok", "to": "end"},
                {"id": "e5", "from": "fail", "to": "end"},
            ],
        }
        _use_definition(monkeypatch, definition)

        class _Result:
            def __init__(self, ok):
                self.ok = ok
                self.mapped = {}
                self.status = "ok" if ok else "error"
                self.mocked = True

        class _Executor:
            def __init__(self, ok):
                self.ok = ok

            async def execute(self, **kwargs):
                return _Result(self.ok)

        import shared.orchestration.tool_executor as te

        monkeypatch.setattr(te, "get_tool_executor", lambda: _Executor(ok=False))
        failed = await _turn(engine, "send voucher", "m-api-fail", name="api_flow")
        assert failed["responseMode"] == "fixed"
        assert failed["reply"] == "I couldn't send the voucher right now."
        assert failed["responseDirectives"] == []

        monkeypatch.setattr(te, "get_tool_executor", lambda: _Executor(ok=True))
        sent = await _turn(engine, "send voucher", "m-api-ok", name="api_flow")
        assert sent["responseMode"] == "llm_grounded"
        assert sent["reply"] == "Your voucher has been emailed."
        assert sent["responseDirectives"] == ["Confirm the voucher email succeeded."]

    async def test_legacy_respond_from_context_is_unchanged(self, engine, monkeypatch):
        definition = {
            "id": "wf_legacy", "version": 1, "name": "Legacy flow",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "ctx", "kind": "message", "label": "Context",
                 "config": {"respondFromContext": True,
                            "text": "Answer from verified context."}},
                {"id": "end", "kind": "end", "label": "End"},
            ],
            "edges": [
                {"id": "e1", "from": "start", "to": "ctx"},
                {"id": "e2", "from": "ctx", "to": "end"},
            ],
        }
        _use_definition(monkeypatch, definition)
        result = await _turn(engine, "my details?", "m-legacy", name="legacy_flow")
        assert result["offScript"] is True
        assert result["contextResponse"] is True
        assert result["reply"] == ""

    async def test_engine_default_prompts_are_localized_not_hardcoded(
        self, engine, monkeypatch
    ):
        """An intent node with no authored prompt asks the localized generic
        question — the old hardcoded English string is gone from the engine."""
        definition = {
            "id": "wf_noprompt", "version": 1, "name": "Noprompt flow",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "hub", "kind": "intent", "label": "Hub", "config": {}},
                {"id": "done", "kind": "end", "label": "End"},
            ],
            "edges": [
                {"id": "e1", "from": "start", "to": "hub"},
                {"id": "e2", "from": "hub", "to": "done", "label": "anything"},
            ],
        }
        _use_definition(monkeypatch, definition)
        english = await _turn(engine, "hi", "m-loc-en", name="noprompt_flow")
        assert english["reply"] == canned("wf_how_help", "en")
        hindi = await _turn(engine, "hi", "m-loc-hi", name="noprompt_flow",
                            language="hi-IN")
        assert hindi["reply"] == canned("wf_how_help", "hi-IN")
