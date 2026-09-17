"""An LLM 'question' label must not park a literal answer off-script.

Live Demo Bot calls (bot_24edba195cd8, wf_94a697661141 v37, 2026-09-16/17):
"मैं टैली यूज़ करता हूँ।", "I use Tally" and "Tally" were transcribed correctly,
the intent classifier labelled them ``question`` (confidence 0.0), and the
n_qualify hub answered its unmatched reply up to six times in one call
(vs_o9Th_dw7qZgJjimwMOhdYtdx). The same text labelled ``affirm`` took the
Tally edge (vs_xd6wz0J_4ba4VhEV8wDXV_GH, 37 s vs 51 s).
"""

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as we
from shared.orchestration.router import looks_like_question

DEFINITION = {
    "id": "wf_qual", "name": "qualification", "version": 1,
    "nodes": [
        {"id": "n_start", "kind": "start", "config": {}},
        {"id": "n_qualify", "kind": "intent", "label": "software question",
         "config": {"prompt": "Aap kaunsa accounting software use karte hain?",
                    "unmatchedReply": "Maaf kijiye, samajh nahi paaya."}},
        {"id": "n_tally", "kind": "message", "config": {"text": "Tally demo offer."}},
        {"id": "n_other", "kind": "message", "config": {"text": "Not supported."}},
        {"id": "n_end", "kind": "end", "config": {}},
    ],
    "edges": [
        {"id": "e0", "from": "n_start", "to": "n_qualify"},
        {"id": "e1", "from": "n_qualify", "to": "n_tally",
         "label": "tally/tally prime/busy use/टैली/बिज़ी"},
        {"id": "e2", "from": "n_qualify", "to": "n_other", "label": "zoho/excel/manual"},
        {"id": "e3", "from": "n_tally", "to": "n_end"},
        {"id": "e4", "from": "n_other", "to": "n_end"},
    ],
}


@pytest.fixture()
def engine(monkeypatch):
    monkeypatch.setattr(we, "load_workflow_definition", lambda t, b, n: DEFINITION)
    wf = we.WorkflowEngine()

    async def _memory():
        if wf._checkpointer is None:
            wf._checkpointer = MemorySaver()
        return wf._checkpointer

    monkeypatch.setattr(wf, "_get_checkpointer", _memory)
    return wf


async def _answer(engine, session, text, signal):
    entered = await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn", bot_id="bot", workflow_name="wf_qual",
        user_text="haan bol raha hoon", signal="affirm", language="hi-IN", reset_state=True,
    )
    assert "n_qualify" in entered["trace"]
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn", bot_id="bot", workflow_name="wf_qual",
        user_text=text, signal=signal, language="hi-IN",
    )


class TestQuestionLabelYieldsToLiteralAnswer:
    @pytest.mark.parametrize("text", [
        "मैं टैली यूज़ करता हूँ।", "मैं बिज़ी यूज़ करता हूँ।", "I use Tally", "Tally",
        "Hello मैं बिज़ी यूज़ करता हूँ।",
    ])
    async def test_statement_labelled_question_takes_the_edge(self, engine, text):
        result = await _answer(engine, f"s-{hash(text)}", text, "question")
        assert "n_tally" in result["trace"], result
        assert not result.get("offScript")

    async def test_same_text_without_label_agrees(self, engine):
        labelled = await _answer(engine, "s-a", "मैं टैली यूज़ करता हूँ।", "question")
        unlabelled = await _answer(engine, "s-b", "मैं टैली यूज़ करता हूँ।", None)
        assert labelled["trace"] == unlabelled["trace"]

    @pytest.mark.parametrize("text", [
        "Tally mein kya hota hai?", "kya aap Tally support karte ho", "Tally कैसे काम करता है",
    ])
    async def test_real_question_naming_an_option_stays_off_script(self, engine, text):
        result = await _answer(engine, f"s-q-{hash(text)}", text, "question")
        # The hub is not advanced; its authored unmatched reply is spoken
        # (a hub in llm_grounded mode would hand the question to the LLM).
        assert result["trace"] == ["n_qualify"], result
        assert "n_tally" not in result["trace"]
        assert result["reply"] == "Maaf kijiye, samajh nahi paaya."

    async def test_other_non_flow_labels_still_park_the_turn(self, engine):
        result = await _answer(engine, "s-c", "मैं टैली यूज़ करता हूँ।", "complaint")
        assert result["trace"] == ["n_qualify"]
        assert result["reply"] == "Maaf kijiye, samajh nahi paaya."

    async def test_generic_answer_under_question_label_does_not_advance(self, engine):
        result = await _answer(engine, "s-g", "haan", "question")
        assert "n_tally" not in result["trace"]


class TestLooksLikeQuestion:
    @pytest.mark.parametrize("text,expected", [
        ("मैं टैली यूज़ करता हूँ।", False), ("I use Tally", False), ("Tally", False),
        ("Tally mein kya hota hai?", True), ("kya aap Tally use karte ho", True),
        ("what is Tally", True), ("Tally कैसे काम करता है", True), ("Busy", False),
    ])
    def test_shape(self, text, expected):
        assert looks_like_question(text) is expected
