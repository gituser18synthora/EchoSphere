"""Definition interpreter: DB-authored node/edge graphs executed by the
WorkflowEngine — branching, slot collection, multi-turn state, tracing,
unknown-workflow handling and the step-budget guard."""

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as wfe


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


PAYMENT_PLAN = {
    "id": "wf_test", "version": 3, "name": "Payment plan journey",
    "nodes": [
        {"id": "n1", "kind": "start", "label": "Call starts", "x": 0, "y": 0},
        {"id": "n2", "kind": "message", "label": "Greeting", "x": 0, "y": 0,
         "config": {"text": "I can help you set up a payment plan."}},
        {"id": "n3", "kind": "ask", "label": "Ask amount", "x": 0, "y": 0,
         "config": {"question": "How much can you pay today, in rupees?",
                    "variable": "amount", "entityType": "number"}},
        {"id": "n4", "kind": "condition", "label": "Enough?", "x": 0, "y": 0,
         "config": {"variable": "amount", "operator": "gte", "value": 500}},
        {"id": "n5", "kind": "message", "label": "Accept", "x": 0, "y": 0,
         "config": {"text": "Great, we can register that partial payment."}},
        {"id": "n6", "kind": "handover", "label": "Agent", "x": 0, "y": 0,
         "config": {"text": "That amount needs an agent's approval.", "queue": "billing"}},
        {"id": "n7", "kind": "end", "label": "End call", "x": 0, "y": 0,
         "config": {"text": "Thank you, goodbye!"}},
    ],
    "edges": [
        {"id": "e1", "from": "n1", "to": "n2"},
        {"id": "e2", "from": "n2", "to": "n3"},
        {"id": "e3", "from": "n3", "to": "n4"},
        {"id": "e4", "from": "n4", "to": "n5", "label": "true"},
        {"id": "e5", "from": "n4", "to": "n6", "label": "false"},
        {"id": "e6", "from": "n5", "to": "n7"},
    ],
}


async def _turn(engine, text, session="sess-1", name="payment_plan_journey"):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_x", bot_id="bot_x",
        workflow_name=name, user_text=text,
    )


class TestDefinitionExecution:
    async def test_first_turn_walks_start_message_ask(self, engine, monkeypatch):
        _use_definition(monkeypatch, PAYMENT_PLAN)
        result = await _turn(engine, "I need more time to pay")
        assert result["source"] == "definition"
        assert result["done"] is False
        assert result["trace"] == ["n1", "n2", "n3"]
        assert "payment plan" in result["reply"]
        assert "How much can you pay today" in result["reply"]

    async def test_true_branch_reaches_end(self, engine, monkeypatch):
        _use_definition(monkeypatch, PAYMENT_PLAN)
        await _turn(engine, "start", session="s-true")
        result = await _turn(engine, "I can pay 2000 rupees", session="s-true")
        assert result["done"] is True and result["status"] == "done"
        assert result["trace"] == ["n3", "n4", "n5", "n7"]
        assert result["slots"]["amount"] == "2000"
        assert "partial payment" in result["reply"]
        assert "goodbye" in result["reply"].lower()

    async def test_false_branch_hands_off(self, engine, monkeypatch):
        _use_definition(monkeypatch, PAYMENT_PLAN)
        await _turn(engine, "start", session="s-false")
        result = await _turn(engine, "only 100", session="s-false")
        assert result["status"] == "handoff" and result["done"] is True
        assert result["trace"] == ["n3", "n4", "n6"]
        assert "agent's approval" in result["reply"]
        # The handover node's configured queue reaches the caller (telephony
        # transfer events carry it as transfer_queue).
        assert result["handoffQueue"] == "billing"

    async def test_non_handoff_turns_carry_no_queue(self, engine, monkeypatch):
        _use_definition(monkeypatch, PAYMENT_PLAN)
        result = await _turn(engine, "start", session="s-noq")
        assert result["handoffQueue"] is None

    async def test_ask_retries_then_handoff(self, engine, monkeypatch):
        _use_definition(monkeypatch, PAYMENT_PLAN)
        await _turn(engine, "start", session="s-retry")
        first = await _turn(engine, "I am not sure", session="s-retry")
        assert first["done"] is False and "didn't catch that" in first["reply"]
        await _turn(engine, "still no number", session="s-retry")
        final = await _turn(engine, "nothing numeric here", session="s-retry")
        assert final["status"] == "handoff" and final["done"] is True

    async def test_state_isolated_between_sessions(self, engine, monkeypatch):
        _use_definition(monkeypatch, PAYMENT_PLAN)
        await _turn(engine, "start", session="s-a")
        await _turn(engine, "800", session="s-a")
        fresh = await _turn(engine, "hello", session="s-b")
        assert fresh["trace"][0] == "n1"  # a new session starts at the top
        assert fresh["slots"] == {}

    async def test_unknown_workflow_is_explicit_not_silent(self, engine, monkeypatch):
        _use_definition(monkeypatch, None)
        result = await _turn(engine, "hello", name="does_not_exist")
        assert result["source"] == "missing" and result["done"] is True
        # Never the appointment-booking greeting (the old silent fallback).
        assert "full name" not in result["reply"].lower()

    async def test_builtin_fallback_still_works(self, engine, monkeypatch):
        _use_definition(monkeypatch, None)
        result = await _turn(engine, "hi", name="appointment_booking")
        assert result["source"] == "builtin"
        assert "name" in result["reply"].lower()


class TestIntentNode:
    GRAPH = {
        "id": "wf_intent", "version": 1, "name": "Support triage",
        "nodes": [
            {"id": "a", "kind": "start", "label": "Start"},
            {"id": "b", "kind": "intent", "label": "Detect intent",
             "config": {"prompt": "Are you calling about billing or delivery?"}},
            {"id": "c", "kind": "message", "label": "Billing",
             "config": {"text": "Billing team route."}},
            {"id": "d", "kind": "message", "label": "Delivery",
             "config": {"text": "Delivery team route."}},
            {"id": "e", "kind": "end", "label": "End"},
        ],
        "edges": [
            {"id": "e1", "from": "a", "to": "b"},
            {"id": "e2", "from": "b", "to": "c", "label": "billing / payment"},
            {"id": "e3", "from": "b", "to": "d", "label": "delivery"},
            {"id": "e4", "from": "c", "to": "e"},
            {"id": "e5", "from": "d", "to": "e"},
        ],
    }

    async def test_intent_edge_labels_route_the_next_utterance(self, engine, monkeypatch):
        _use_definition(monkeypatch, self.GRAPH)
        opening = await _turn(engine, "hello", session="i-1", name="support_triage")
        assert "billing or delivery" in opening["reply"]
        routed = await _turn(engine, "it's about my payment", session="i-1",
                             name="support_triage")
        assert routed["trace"] == ["b", "c", "e"]
        assert "Billing team" in routed["reply"] and routed["done"] is True


class TestLoopGuard:
    LOOP = {
        "id": "wf_loop", "version": 1, "name": "Loop",
        "nodes": [
            {"id": "a", "kind": "start", "label": "Start"},
            {"id": "b", "kind": "message", "label": "M1", "config": {"text": "one"}},
            {"id": "c", "kind": "message", "label": "M2", "config": {"text": "two"}},
        ],
        "edges": [
            {"id": "e1", "from": "a", "to": "b"},
            {"id": "e2", "from": "b", "to": "c"},
            {"id": "e3", "from": "c", "to": "b"},  # infinite cycle
        ],
    }

    async def test_cycle_hits_step_budget_and_errors_out(self, engine, monkeypatch):
        _use_definition(monkeypatch, self.LOOP)
        result = await _turn(engine, "hi", session="loop-1", name="loop")
        assert result["status"] == "error" and result["done"] is True


class TestSlugify:
    def test_names_slugify_to_route_form(self):
        assert wfe.slugify_workflow_name("Payment plan journey") == "payment_plan_journey"
        assert wfe.slugify_workflow_name("Billing – Support!! journey") == "billing_support_journey"


class TestLocalizedEngineStrings:
    """Generic interpreter strings (retry prefix, handover/error fallbacks)
    follow the caller's conversation language; node-authored text is spoken
    exactly as authored."""

    async def _hi_turn(self, engine, text, session="s-hi"):
        return await engine.handle_turn_detailed(
            session_id=session, tenant_id="tn_x", bot_id="bot_x",
            workflow_name="payment_plan_journey", user_text=text,
            language="hi-IN",
        )

    async def test_ask_retry_prefix_is_hindi_for_hindi_calls(
        self, engine, monkeypatch
    ):
        from shared.orchestration.phrases import canned

        _use_definition(monkeypatch, PAYMENT_PLAN)
        await self._hi_turn(engine, "shuru karo")
        result = await self._hi_turn(engine, "पता नहीं")  # not a number → retry
        assert result["reply"].startswith(canned("wf_retry_prefix", "hi-IN"))
        # The authored question itself is untouched.
        assert "How much can you pay today" in result["reply"]

    async def test_ask_retry_prefix_stays_english_by_default(
        self, engine, monkeypatch
    ):
        _use_definition(monkeypatch, PAYMENT_PLAN)
        await _turn(engine, "start", session="s-en")
        result = await _turn(engine, "no idea", session="s-en")
        assert result["reply"].startswith("Sorry, I didn't catch that.")

    async def test_unknown_workflow_reply_is_localized(self, engine, monkeypatch):
        from shared.orchestration.phrases import canned

        _use_definition(monkeypatch, None)
        result = await engine.handle_turn_detailed(
            session_id="s-miss", tenant_id="tn_x", bot_id="bot_x",
            workflow_name="does_not_exist", user_text="haan",
            language="hi-IN",
        )
        assert result["reply"] == canned("wf_missing", "hi-IN")
        assert result["status"] == "error"
