"""LangGraph workflows: slot filling, retries, confirmation (appointment
booking and the mPokket payment-collection MOP flow)."""

import pytest
from langgraph.checkpoint.memory import MemorySaver

from shared.orchestration.workflow_engine import (
    WorkflowEngine,
    build_appointment_graph,
)


@pytest.fixture()
def engine(monkeypatch):
    wf = WorkflowEngine()

    async def _memory_checkpointer():
        if wf._checkpointer is None:
            wf._checkpointer = MemorySaver()
        return wf._checkpointer

    monkeypatch.setattr(wf, "_get_checkpointer", _memory_checkpointer)
    return wf


async def turn(engine: WorkflowEngine, session: str, text: str):
    return await engine.handle_turn(
        session_id=session, tenant_id="tn-x", bot_id="bot-x",
        workflow_name="appointment_booking", user_text=text,
    )


class TestHappyPath:
    async def test_full_booking(self, engine):
        session = "s-happy"
        reply, done = await turn(engine, session, "I want to book an appointment")
        assert "name" in reply.lower() and not done
        reply, done = await turn(engine, session, "My name is Asha Verma")
        assert "phone" in reply.lower() and not done
        reply, done = await turn(engine, session, "9876543210")
        assert "date" in reply.lower() and not done
        reply, done = await turn(engine, session, "tomorrow")
        assert "time" in reply.lower() and not done
        reply, done = await turn(engine, session, "10:30 am")
        assert "confirm" in reply.lower() and not done
        reply, done = await turn(engine, session, "yes please")
        assert done and "booked" in reply.lower()


class TestRetriesAndHandoff:
    async def test_invalid_phone_reasks_then_hands_off(self, engine):
        session = "s-retry"
        await turn(engine, session, "book")
        await turn(engine, session, "Asha Verma")  # name ok → asks phone
        reply, done = await turn(engine, session, "uh")  # invalid
        assert "phone" in reply.lower() and not done
        reply, done = await turn(engine, session, "hmm")  # invalid again
        assert not done
        reply, done = await turn(engine, session, "eh")  # third failure → handoff
        assert done
        assert "colleague" in reply.lower() or "connect" in reply.lower()


async def pay_turn(engine: WorkflowEngine, session: str, text: str):
    return await engine.handle_turn(
        session_id=session, tenant_id="tn_22a809aecf66", bot_id="bot_47e52822c803",
        workflow_name="payment_collection", user_text=text,
    )


class TestPaymentCollection:
    async def test_full_flow_with_summary_before_confirmation(self, engine):
        session = "pay-happy"
        reply, done = await pay_turn(engine, session, "mujhe payment karna hai")
        assert "poora" in reply.lower() and not done  # asks full vs partial
        reply, done = await pay_turn(engine, session, "poora amount")
        assert "debit card ya upi" in reply.lower() and not done  # script MOP question
        reply, done = await pay_turn(engine, session, "UPI se")
        # The summary must be spoken before the yes/no is interpreted.
        assert "confirm" in reply.lower() and "upi" in reply.lower() and not done
        reply, done = await pay_turn(engine, session, "haan sahi hai")
        assert done
        # Guidance only — the bot must never claim the payment completed.
        assert "complete kar dijiye" in reply.lower()
        assert "shubh ho" in reply.lower()  # script closing line

    async def test_upfront_details_skip_questions(self, engine):
        session = "pay-upfront"
        reply, done = await pay_turn(engine, session, "main poora payment UPI se karna chahta hun")
        assert "confirm" in reply.lower() and not done
        reply, done = await pay_turn(engine, session, "bilkul sahi")
        assert done and "upi" in reply.lower()

    async def test_upi_reply_mentions_script_benefits(self, engine):
        session = "pay-benefit"
        await pay_turn(engine, session, "poora payment UPI se")
        reply, done = await pay_turn(engine, session, "haan")
        assert done and ("cashback" in reply.lower() or "discount" in reply.lower())

    async def test_debit_card_reply_has_no_upi_benefits(self, engine):
        session = "pay-card"
        await pay_turn(engine, session, "poora payment debit card se karunga")
        reply, done = await pay_turn(engine, session, "haan")
        assert done and "cashback" not in reply.lower()

    async def test_devanagari_flow(self, engine):
        """Saaras transcribes Hindi speech in Devanagari — slots must fill."""
        session = "pay-hindi"
        reply, done = await pay_turn(engine, session, "मुझे पेमेंट करना है")
        assert not done
        reply, done = await pay_turn(engine, session, "पूरा अमाउंट यूपीआई से")
        assert "confirm" in reply.lower() and not done
        reply, done = await pay_turn(engine, session, "हाँ सही है")
        assert done and "shubh ho" in reply.lower()

    async def test_rejection_restarts_collection(self, engine):
        session = "pay-restart"
        await pay_turn(engine, session, "partial payment UPI se")
        reply, done = await pay_turn(engine, session, "nahi galat hai")
        assert not done and "poora" in reply.lower()  # back to first question

    async def test_repeated_confusion_hands_off_with_simpler_retry(self, engine):
        session = "pay-handoff"
        await pay_turn(engine, session, "payment")
        reply, done = await pay_turn(engine, session, "kya?")  # retry 1 → simpler wording
        assert "kripya boliye" in reply.lower() and not done
        reply, done = await pay_turn(engine, session, "hmm")  # retry 2
        assert not done
        reply, done = await pay_turn(engine, session, "pata nahi")  # → handoff
        assert done and "agent" in reply.lower()


class TestStatePersistence:
    async def test_state_survives_across_graph_instances(self):
        saver = MemorySaver()
        graph_a = build_appointment_graph(saver)
        thread = {"configurable": {"thread_id": "persist-1"}}
        await graph_a.ainvoke(
            {"tenant_id": "t", "bot_id": "b", "session_id": "persist-1",
             "workflow": "appointment_booking", "user_text": "book"},
            config=thread,
        )
        state_a = await graph_a.ainvoke(
            {"user_text": "Asha Verma"}, config=thread
        )
        # New compiled graph, same checkpointer → same accumulated slots.
        graph_b = build_appointment_graph(saver)
        state_b = await graph_b.aget_state(thread)
        assert state_b.values.get("slots", {}).get("name") == state_a["slots"]["name"]

    async def test_no_restart_confusion_between_sessions(self, engine):
        await turn(engine, "s-a", "book")
        reply_a, _ = await turn(engine, "s-a", "Asha Verma")
        reply_b, _ = await turn(engine, "s-b", "book")
        assert "phone" in reply_a.lower()
        assert "name" in reply_b.lower()  # fresh session starts from name
