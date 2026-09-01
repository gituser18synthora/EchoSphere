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

    async def test_verified_slots_skip_repeated_asks_and_delegate_answer(
        self, engine, monkeypatch,
    ):
        definition = {
            "id": "wf_context", "version": 1, "name": "Context flow",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "booking", "kind": "ask", "label": "Booking ID",
                 "config": {"question": "What is your booking ID?",
                            "variable": "booking_id"}},
                {"id": "details", "kind": "message", "label": "Details",
                 "config": {"respondFromContext": True,
                            "text": "Answer from verified context."}},
                {"id": "end", "kind": "end", "label": "End"},
            ],
            "edges": [
                {"id": "e1", "from": "start", "to": "booking"},
                {"id": "e2", "from": "booking", "to": "details"},
                {"id": "e3", "from": "details", "to": "end"},
            ],
        }
        _use_definition(monkeypatch, definition)

        result = await engine.handle_turn_detailed(
            session_id="s-context", tenant_id="tn_x", bot_id="bot_x",
            workflow_name="context_flow", user_text="share my details",
            initial_slots={"booking_id": "601001", "customer_verified": True},
        )

        assert result["done"] is True
        assert result["offScript"] is True
        assert result["contextResponse"] is True
        assert result["reply"] == ""
        assert result["slots"]["booking_id"] == "601001"
        assert result["trace"] == ["start", "booking", "details", "end"]

    async def test_verified_reentry_consumes_action_at_intent_hub(
        self, engine, monkeypatch,
    ):
        definition = {
            "id": "wf_reentry", "version": 1, "name": "Reentry flow",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "booking", "kind": "ask", "label": "Booking ID",
                 "config": {"question": "Booking ID?", "variable": "booking_id"}},
                {"id": "hub", "kind": "intent", "label": "Action",
                 "config": {"prompt": "Details or voucher?"}},
                {"id": "voucher", "kind": "message", "label": "Voucher",
                 "config": {"text": "Voucher branch selected."}},
                {"id": "details", "kind": "message", "label": "Details",
                 "config": {"text": "Details branch selected."}},
                {"id": "end", "kind": "end", "label": "End"},
            ],
            "edges": [
                {"id": "e1", "from": "start", "to": "booking"},
                {"id": "e2", "from": "booking", "to": "hub"},
                {"id": "e3", "from": "hub", "to": "details", "label": "details"},
                {"id": "e4", "from": "hub", "to": "voucher", "label": "voucher/email"},
                {"id": "e5", "from": "voucher", "to": "end"},
                {"id": "e6", "from": "details", "to": "end"},
            ],
        }
        _use_definition(monkeypatch, definition)

        result = await engine.handle_turn_detailed(
            session_id="s-reentry", tenant_id="tn_x", bot_id="bot_x",
            workflow_name="reentry_flow", user_text="please email my voucher",
            initial_slots={"booking_id": "601001", "customer_verified": True},
        )

        assert result["done"] is True
        assert "Voucher branch selected" in result["reply"]
        assert result["trace"] == ["start", "booking", "hub", "voucher", "end"]


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

    async def test_fixed_unmatched_reply_never_follows_else_or_calls_llm(
        self, engine, monkeypatch,
    ):
        graph = {
            **self.GRAPH,
            "nodes": [
                self.GRAPH["nodes"][0],
                {"id": "b", "kind": "intent", "label": "Verify choice",
                 "config": {
                     "prompt": "Say billing or delivery.",
                     "unmatchedReply": "No verified choice yet. Say billing or delivery.",
                 }},
                *self.GRAPH["nodes"][2:],
            ],
            "edges": [
                *self.GRAPH["edges"],
                {"id": "else", "from": "b", "to": "d", "label": "else"},
            ],
        }
        _use_definition(monkeypatch, graph)
        await _turn(engine, "hello", session="i-fixed", name="support_triage")

        for _ in range(3):
            result = await _turn(
                engine, "what choice did I give?", session="i-fixed",
                name="support_triage",
            )
            assert result["done"] is False
            assert result["offScript"] is False
            assert result["trace"] == ["b"]
            assert result["reply"] == (
                "No verified choice yet. Say billing or delivery."
            )

        routed = await _turn(
            engine, "billing", session="i-fixed", name="support_triage",
        )
        assert routed["done"] is True
        assert routed["trace"] == ["b", "c", "e"]

    async def test_identifier_correction_routes_back_through_verification(
        self, engine, monkeypatch,
    ):
        pattern = r"(?<![0-9])([0-9]{10}|[0-9]{7})(?![0-9])"
        graph = {
            "id": "wf_correction", "version": 1, "name": "Correction flow",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "offer", "kind": "intent", "label": "Agent offer",
                 "config": {
                     "prompt": "Do you want an agent?",
                     "unmatchedReply": "No order is verified.",
                     "identifierCorrection": {
                         "variable": "order_ref2", "entityType": "text",
                         "pattern": pattern, "target": "verify",
                     },
                 }},
                {"id": "verify", "kind": "message", "label": "Verify",
                 "config": {"text": "Verification ran."}},
                {"id": "handoff", "kind": "handover", "label": "Agent"},
                {"id": "end", "kind": "end", "label": "End"},
            ],
            "edges": [
                {"id": "e1", "from": "start", "to": "offer"},
                {"id": "e2", "from": "offer", "to": "handoff",
                 "label": "yes/agent"},
                {"id": "e3", "from": "verify", "to": "end"},
            ],
        }
        _use_definition(monkeypatch, graph)
        await _turn(engine, "hello", session="i-correct", name="correction_flow")
        result = await _turn(
            engine, "sorry, it is seven zero zero one zero zero three",
            session="i-correct", name="correction_flow",
        )

        assert result["done"] is True
        assert result["offScript"] is False
        assert result["slots"]["order_ref2"] == "7001003"
        assert result["trace"] == ["offer", "verify", "end"]
        assert "Verification ran" in result["reply"]

        await _turn(engine, "hello", session="i-agent", name="correction_flow")
        agent = await _turn(
            engine, "yes, connect an agent; the ID is 7001003",
            session="i-agent", name="correction_flow",
        )
        assert agent["status"] == "handoff"
        assert "order_ref2" not in agent["slots"]


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


# ── collection-ladder semantics (the eDAS loan-recovery bug class) ──────────
# A ladder workflow where every rung is an intent node whose generic tokens
# ("nahi") used to swallow hardship statements and complaints, and whose
# forced first-edge fallback used to walk the script sequentially no matter
# what the caller said.

LADDER = {
    "id": "wf_ladder", "version": 1, "name": "Collection ladder",
    "nodes": [
        {"id": "n_start", "kind": "start", "label": "Call connected"},
        {"id": "n_push", "kind": "intent", "label": "Rung 1",
         "config": {"prompt": "कृपया अभी UPI से payment कर दीजिए। "
                              "क्या आप अभी payment करेंगे?"}},
        {"id": "n_benefits", "kind": "intent", "label": "Rung 2",
         "config": {"prompt": "अभी payment करने पर cashback मिल सकता है। "
                              "क्या आप payment करेंगे?"}},
        {"id": "n_hardship", "kind": "intent", "label": "Hardship",
         "config": {"prompt": "मैं समझ रहा हूँ, अफ़सोस है। "
                              "क्या हम बाद में बात करें?"}},
        {"id": "n_ptype", "kind": "ask", "label": "Full or partial",
         "config": {"question": "पूरा payment करेंगे या partial?",
                    "variable": "payment_type", "entityType": "list",
                    "synonyms": {"full": ["poora", "पूरा", "full"],
                                 "partial": ["partial", "आधा", "aadha"]}}},
        {"id": "n_refuse_end", "kind": "end", "label": "Refusal close",
         "config": {"text": "ठीक है, धन्यवाद।"}},
        {"id": "n_callback_end", "kind": "end", "label": "Callback close",
         "config": {"text": "हम बाद में कॉल करेंगे।"}},
        {"id": "n_done", "kind": "end", "label": "Close",
         "config": {"text": "धन्यवाद, शुभ दिन!"}},
    ],
    "edges": [
        {"from": "n_start", "to": "n_push"},
        {"from": "n_push", "to": "n_hardship",
         "label": "paise nahi,no money,पैसे नहीं,पेमेंट नहीं कर"},
        {"from": "n_push", "to": "n_benefits", "label": "nahi,नहीं,baad,कल"},
        {"from": "n_push", "to": "n_ptype",
         "label": "haan,हाँ,kar dunga,करूंगा,upi,यूपीआई"},
        {"from": "n_push", "to": "n_benefits", "label": "else"},
        {"from": "n_benefits", "to": "n_hardship",
         "label": "paise nahi,no money,पैसे नहीं"},
        {"from": "n_benefits", "to": "n_refuse_end", "label": "nahi,नहीं"},
        {"from": "n_benefits", "to": "n_ptype", "label": "haan,हाँ,kar dunga,करूंगा"},
        {"from": "n_hardship", "to": "n_callback_end",
         "label": "haan,बाद में,baad mein"},
        {"from": "n_hardship", "to": "n_refuse_end", "label": "else"},
        {"from": "n_ptype", "to": "n_done", "label": "next"},
    ],
}


async def _ladder_turn(engine, text, session):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_x", bot_id="bot_x",
        workflow_name="collection_ladder", user_text=text, language="hi-IN",
    )


class TestLadderSignals:
    async def test_hardship_entry_skips_the_pitch(self, engine, monkeypatch):
        """The utterance that TRIGGERS the workflow must be evaluated — a
        caller entering with "no money" lands on the hardship branch instead
        of hearing rung one's UPI push."""
        _use_definition(monkeypatch, LADDER)
        result = await _ladder_turn(engine, "पर मेरे पास पैसे नहीं हैं।", "lh-1")
        assert result["trace"] == ["n_start", "n_push", "n_hardship"]
        assert "अफ़सोस" in result["reply"]
        assert "UPI से payment" not in result["reply"]

    async def test_positive_entry_skips_to_payment_type(self, engine, monkeypatch):
        _use_definition(monkeypatch, LADDER)
        result = await _ladder_turn(engine, "payment kar dunga abhi", "lp-1")
        assert result["trace"] == ["n_start", "n_push", "n_ptype"]
        assert "पूरा payment" in result["reply"]

    async def test_bare_confirmation_entry_still_hears_rung_one(
        self, engine, monkeypatch
    ):
        """"haan" answered the greeting, not rung one — no jump."""
        _use_definition(monkeypatch, LADDER)
        result = await _ladder_turn(engine, "haan ji", "lc-1")
        assert result["trace"] == ["n_start", "n_push"]
        assert "क्या आप अभी payment करेंगे" in result["reply"]

    async def test_hardship_beats_generic_nahi_edge(self, engine, monkeypatch):
        """"पैसे नहीं" (hardship edge) must win over the single generic token
        "नहीं" on the next-rung ladder edge."""
        _use_definition(monkeypatch, LADDER)
        await _ladder_turn(engine, "namaste", "lg-1")  # → awaiting rung 1
        result = await _ladder_turn(engine, "मेरे पास पैसे नहीं हैं", "lg-1")
        assert result["trace"] == ["n_push", "n_hardship"]
        assert "अफ़सोस" in result["reply"]

    async def test_complaint_is_off_script_and_keeps_the_node(
        self, engine, monkeypatch
    ):
        """"You are not listening" contains "nahi" but must NOT descend the
        ladder — the turn is off-script, the node unchanged, and the next
        real answer is still consumed by the SAME node."""
        _use_definition(monkeypatch, LADDER)
        await _ladder_turn(engine, "namaste", "lo-1")
        complaint = await _ladder_turn(engine, "aap meri baat sun nahi rahe ho", "lo-1")
        assert complaint["offScript"] is True
        assert complaint["reply"] == ""
        assert complaint["signal"] == "complaint"
        assert "क्या आप अभी payment करेंगे" in (complaint["nodePrompt"] or "")
        assert complaint["done"] is False
        followup = await _ladder_turn(engine, "haan theek hai", "lo-1")
        assert followup["trace"] == ["n_push", "n_ptype"]  # node was retained

    async def test_repeated_refusal_never_becomes_payment_intent(
        self, engine, monkeypatch
    ):
        """"nahi karunga" contains the positive-edge token "karunga" — the
        refusal signal must still route it down the refusal edge."""
        _use_definition(monkeypatch, LADDER)
        await _ladder_turn(engine, "namaste", "lr-1")
        first = await _ladder_turn(engine, "abhi nahi", "lr-1")
        assert first["trace"] == ["n_push", "n_benefits"]  # one authored rung
        second = await _ladder_turn(engine, "nahi karunga bola na", "lr-1")
        assert second["trace"] == ["n_benefits", "n_refuse_end"]
        assert second["done"] is True
        assert "पूरा payment" not in second["reply"]  # never the positive path

    async def test_gibberish_goes_off_script_then_takes_authored_else(
        self, engine, monkeypatch
    ):
        """First unmatched turn: off-script (the brain answers the caller's
        actual words — never a canned "didn't catch that" + repeated pitch).
        A SECOND unmatched turn on the same node takes the authored else edge
        so the flow still progresses."""
        _use_definition(monkeypatch, LADDER)
        await _ladder_turn(engine, "namaste", "lz-1")
        first = await _ladder_turn(engine, "ghar par sab jama hue", "lz-1")
        assert first["offScript"] is True
        assert first["trace"] == ["n_push"]  # no transition, no canned repeat
        assert first["reply"] == ""
        second = await _ladder_turn(engine, "ghar par sab jama hue", "lz-1")
        assert second["trace"][:2] == ["n_push", "n_benefits"]  # authored else

    async def test_gibberish_without_else_goes_off_script_not_first_edge(
        self, engine, monkeypatch
    ):
        """n_benefits has NO else edge: unmatched input must never blindly
        follow the first outgoing edge (the old sequential-script bug)."""
        _use_definition(monkeypatch, LADDER)
        await _ladder_turn(engine, "namaste", "lf-1")
        await _ladder_turn(engine, "abhi nahi", "lf-1")  # → awaiting n_benefits
        await _ladder_turn(engine, "ghar par sab jama hue", "lf-1")  # retry 1
        result = await _ladder_turn(engine, "ghar par sab jama hue", "lf-1")
        assert result["offScript"] is True
        assert result["done"] is False

    async def test_hardship_at_ask_node_is_off_script(self, engine, monkeypatch):
        """An ask node must not burn retries (or advance) on a hardship
        statement — off-script, and the ask still accepts the next answer."""
        _use_definition(monkeypatch, LADDER)
        await _ladder_turn(engine, "namaste", "la-1")
        await _ladder_turn(engine, "haan", "la-1")  # → awaiting n_ptype
        hardship = await _ladder_turn(engine, "mere paas paise nahi hain", "la-1")
        assert hardship["offScript"] is True and hardship["done"] is False
        answer = await _ladder_turn(engine, "poora kar dunga", "la-1")
        assert answer["slots"]["payment_type"] == "full"
        assert answer["done"] is True

    async def test_hardship_then_callback_closes_respectfully(
        self, engine, monkeypatch
    ):
        _use_definition(monkeypatch, LADDER)
        await _ladder_turn(engine, "पर मेरे पास पैसे नहीं हैं।", "lb-1")
        result = await _ladder_turn(engine, "haan baad mein baat karte hain", "lb-1")
        assert result["trace"] == ["n_hardship", "n_callback_end"]
        assert result["done"] is True

    async def test_ladder_state_isolated_per_session(self, engine, monkeypatch):
        """A new call starts at the top — no leakage from other sessions."""
        _use_definition(monkeypatch, LADDER)
        await _ladder_turn(engine, "namaste", "li-1")
        await _ladder_turn(engine, "abhi nahi", "li-1")  # session 1 at rung 2
        fresh = await _ladder_turn(engine, "namaste", "li-2")
        assert fresh["trace"] == ["n_start", "n_push"]
        assert "क्या आप अभी payment करेंगे" in fresh["reply"]


# ── definition lookup cache: one DB scan per TTL window ─────────────────────


class _FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def first(self):
        return self._scalar


class _FakeWorkflowRow:
    id = "wf_db"
    version = 2
    name = "Payment Plan"
    nodes = [{"id": "n1", "kind": "start"}]
    edges = []


class _FakeDefinitionSession:
    """Serves the narrowed two-phase lookup: a cheap (id, version, name)
    projection first, then the single matching row's full definition."""

    def __init__(self, counters):
        self._counters = counters

    def execute(self, stmt):
        self._counters["queries"] += 1
        if len(stmt.selected_columns) == 3:  # projection phase
            return _FakeResult(rows=[("wf_db", 2, "Payment Plan"),
                                     ("wf_old", 1, "Payment Plan")])
        return _FakeResult(scalar=_FakeWorkflowRow())  # full-row phase

    def close(self):
        pass


class TestDefinitionLookupCache:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        wfe._definition_cache.clear()
        yield
        wfe._definition_cache.clear()

    def _patch_db(self, monkeypatch):
        import shared.db.mysql as mysql_db

        counters = {"sessions": 0, "queries": 0}

        def factory():
            counters["sessions"] += 1
            return _FakeDefinitionSession(counters)

        monkeypatch.setattr(mysql_db, "get_sessionmaker", lambda: factory)
        return counters

    def test_second_lookup_within_ttl_never_hits_the_db(self, monkeypatch):
        counters = self._patch_db(monkeypatch)
        first = wfe.load_workflow_definition("tn_x", "bot_x", "payment_plan")
        assert first == {
            "id": "wf_db", "version": 2, "name": "Payment Plan",
            "nodes": [{"id": "n1", "kind": "start"}], "edges": [],
        }
        assert counters["sessions"] == 1
        second = wfe.load_workflow_definition("tn_x", "bot_x", "payment_plan")
        assert second == first
        assert counters["sessions"] == 1  # served from the cache

    def test_no_match_is_cached_too(self, monkeypatch):
        counters = self._patch_db(monkeypatch)
        assert wfe.load_workflow_definition("tn_x", "bot_x", "other_flow") is None
        assert wfe.load_workflow_definition("tn_x", "bot_x", "other_flow") is None
        assert counters["sessions"] == 1

    def test_expired_entry_requeries(self, monkeypatch):
        counters = self._patch_db(monkeypatch)
        monkeypatch.setattr(wfe, "_DEFINITION_CACHE_TTL_SECONDS", 0.0)
        wfe.load_workflow_definition("tn_x", "bot_x", "payment_plan")
        wfe.load_workflow_definition("tn_x", "bot_x", "payment_plan")
        assert counters["sessions"] == 2

    def test_only_the_matching_row_loads_its_definition(self, monkeypatch):
        counters = self._patch_db(monkeypatch)
        wfe.load_workflow_definition("tn_x", "bot_x", "payment_plan")
        # One projection query + ONE full-row load, never one per version.
        assert counters["queries"] == 2


MULTI_ANSWER = {
    "id": "wf_multi", "version": 1, "name": "Multi answer journey",
    # Canonical values are deliberately phrases that never occur verbatim in
    # speech ("guard / security"): the extractor auto-adds every canonical as
    # a surface, so a speakable canonical like "customer" would out-match
    # shorter true surfaces inside utterances such as "customer ke guard ko".
    "nodes": [
        {"id": "m1", "kind": "start", "label": "Start", "x": 0, "y": 0},
        {"id": "m2", "kind": "ask", "label": "Story", "x": 0, "y": 0,
         "config": {"question": "What happened?",
                    "variable": "story", "entityType": "text",
                    "alsoCapture": [
                        {"variable": "called",
                         "entity": {"dataType": "text",
                                    "synonyms": {"yes (called)": ["call kiya"],
                                                 "no (did not call)": ["call nahi"]}}},
                        {"variable": "recipient",
                         "entity": {"dataType": "text",
                                    "synonyms": {"guard / security": ["guard ko de diya"],
                                                 "customer (direct)": ["customer ko de diya"]}}},
                    ]}},
        {"id": "m3", "kind": "ask", "label": "Called?", "x": 0, "y": 0,
         "config": {"question": "Did you call the customer?",
                    "variable": "called", "entityType": "text",
                    "entity": {"dataType": "text",
                               "synonyms": {"yes (called)": ["haan", "yes"],
                                            "no (did not call)": ["nahi", "no"]}},
                    "alsoCapture": [
                        {"variable": "recipient",
                         "entity": {"dataType": "text",
                                    "synonyms": {"guard / security": ["guard ko"],
                                                 "customer (direct)": ["customer ko"]}}},
                    ]}},
        {"id": "m4", "kind": "ask", "label": "Recipient", "x": 0, "y": 0,
         "config": {"question": "Who received the order?",
                    "variable": "recipient", "entityType": "text",
                    "entity": {"dataType": "text",
                               "synonyms": {"guard / security": ["guard"],
                                            "customer (direct)": ["customer ko",
                                                                  "customer ke haath"]}}}},
        {"id": "m5", "kind": "end", "label": "End", "x": 0, "y": 0,
         "config": {"text": "Noted, thank you."}},
    ],
    "edges": [
        {"id": "me1", "from": "m1", "to": "m2"},
        {"id": "me2", "from": "m2", "to": "m3"},
        {"id": "me3", "from": "m3", "to": "m4"},
        {"id": "me4", "from": "m4", "to": "m5"},
    ],
}


class TestAlsoCapture:
    """Opt-in multi-answer capture: one utterance answering several upcoming
    asks fills their slots, and the flow continues at the next UNANSWERED
    question instead of mechanically re-asking."""

    async def test_narrative_fills_later_asks_and_skips_them(
        self, engine, monkeypatch
    ):
        _use_definition(monkeypatch, MULTI_ANSWER)
        first = await _turn(engine, "", session="ac-1", name="multi_answer_journey")
        assert "What happened" in first["reply"]
        # One narrative answers the story AND both later questions.
        r = await _turn(
            engine,
            "maine call kiya tha aur guard ko de diya tha order",
            session="ac-1", name="multi_answer_journey",
        )
        assert r["slots"]["called"] == "yes (called)"
        assert r["slots"]["recipient"] == "guard / security"
        assert r["status"] == "done"          # both asks were skipped
        assert "Noted" in r["reply"]

    async def test_partial_answer_asks_only_the_missing_question(
        self, engine, monkeypatch
    ):
        _use_definition(monkeypatch, MULTI_ANSWER)
        await _turn(engine, "", session="ac-2", name="multi_answer_journey")
        # The narrative answers only the call question.
        r = await _turn(engine, "maine call kiya tha bas",
                        session="ac-2", name="multi_answer_journey")
        assert r["slots"]["called"] == "yes (called)"
        assert "recipient" not in r["slots"]
        assert "Who received" in r["reply"]   # only the missing ask remains
        r = await _turn(engine, "guard ko diya tha",
                        session="ac-2", name="multi_answer_journey")
        assert r["slots"]["recipient"] == "guard / security"
        assert r["status"] == "done"

    async def test_also_capture_never_overwrites_an_earlier_answer(
        self, engine, monkeypatch
    ):
        _use_definition(monkeypatch, MULTI_ANSWER)
        await _turn(engine, "", session="ac-3", name="multi_answer_journey")
        r = await _turn(engine, "call nahi ho paya tha",
                        session="ac-3", name="multi_answer_journey")
        assert r["slots"]["called"] == "no (did not call)"
        # The recipient answer mentions "customer ko" via alsoCapture on m3 —
        # but m3's own slot is already "no" and must stay untouched.
        r = await _turn(engine, "customer ke guard ko diya",
                        session="ac-3", name="multi_answer_journey")
        assert r["slots"]["called"] == "no (did not call)"
        assert r["slots"]["recipient"] == "guard / security"
        assert r["status"] == "done"
