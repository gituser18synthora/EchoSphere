"""Appointment-booking LangGraph workflow: slot filling, retries, confirmation."""

import pytest
from langgraph.checkpoint.memory import MemorySaver

from shared.orchestration.workflow_engine import WorkflowEngine, build_appointment_graph


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
