"""Behaviour v3: a free-text ask stores a statement the LLM labelled 'complaint'.

Live Zepto MDND calls on 2026-09-23 (cv_7c792739697e and three sibling calls,
bot_59a84478f155, wf_7e4cf166c7bd v17): at "बताइए — क्या हुआ था?" the partner
said "हाँ, मैंने प्रोडक्ट डिलीवर कर दिया, फिर भी MDND मार्क्ड हुआ है।". The
intent classifier labelled the story ``question`` on some calls and
``complaint`` on others. Under behaviour v1 both labels parked the ask: the
question label handed the turn to the LLM (which re-asked the readout), the
complaint label spoke the authored ``unmatchedReply`` ("समझ नहीं पाया").
Behaviour v2 only covered the question label; v3 treats a complaint-labelled
statement at a free-text ask the same way — the words ARE the answer.
"""

import copy

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as we

DEFINITION = {
    "id": "wf_mdnd_v3", "name": "mdnd3", "version": 1,
    "nodes": [
        {"id": "n_start", "kind": "start", "config": {"behavior": {"version": 3}}},
        {"id": "n_ask_issue", "kind": "ask", "label": "what happened",
         "config": {"question": "बताइए — क्या हुआ था?", "variable": "m_issue_description",
                    "entityType": "text", "responseMode": "llm_grounded",
                    "unmatchedReply": "माफ़ कीजिए, समझ नहीं पाया। क्या हुआ था?"}},
        {"id": "n_ask_reached", "kind": "ask", "label": "reached?",
         "config": {"question": "क्या आप location पर पहुंचे थे?", "variable": "m_reached",
                    "entityType": "text",
                    "synonyms": {"yes": ["haan", "pahuncha tha"], "no": ["nahi"]},
                    "unmatchedReply": "माफ़ कीजिए, समझ नहीं पाया। पहुंचे थे?"}},
        {"id": "n_end", "kind": "end", "config": {}},
    ],
    "edges": [
        {"id": "e0", "from": "n_start", "to": "n_ask_issue"},
        {"id": "e1", "from": "n_ask_issue", "to": "n_ask_reached"},
        {"id": "e2", "from": "n_ask_reached", "to": "n_end"},
    ],
}

V2_DEFINITION = copy.deepcopy(DEFINITION)
V2_DEFINITION["id"] = "wf_mdnd_v2"
V2_DEFINITION["nodes"][0]["config"] = {"behavior": {"version": 2}}

STORY_HI = "हाँ, मैंने प्रोडक्ट डिलीवर कर दिया, फिर भी एमडीएनटी मार्क्ड हुआ है।"
STORY_HINGLISH = "maine product deliver kar diya phir bhi mera MDND mark ho gaya"


def _engine_for(monkeypatch, definition):
    monkeypatch.setattr(we, "load_workflow_definition", lambda t, b, n: definition)
    wf = we.WorkflowEngine()

    async def _memory():
        if wf._checkpointer is None:
            wf._checkpointer = MemorySaver()
        return wf._checkpointer

    monkeypatch.setattr(wf, "_get_checkpointer", _memory)
    return wf


@pytest.fixture()
def engine(monkeypatch):
    return _engine_for(monkeypatch, DEFINITION)


@pytest.fixture()
def v2_engine(monkeypatch):
    return _engine_for(monkeypatch, V2_DEFINITION)


async def _turn(engine, session, text, signal, reset=False):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn", bot_id="bot", workflow_name="mdnd3",
        user_text=text, signal=signal, language="hi-IN", reset_state=reset,
    )


async def _at_issue_ask(engine, session):
    entered = await _turn(engine, session, "haan boliye", "affirm", reset=True)
    assert entered["trace"] == ["n_start", "n_ask_issue"], entered
    return entered


class TestComplaintLabelledStatementIsTheAnswer:
    @pytest.mark.parametrize("text", [STORY_HI, STORY_HINGLISH,
                                      "I delivered the order but still got marked MDND"])
    async def test_complaint_narrative_fills_the_free_text_slot(self, engine, text):
        session = f"c-{hash(text)}"
        await _at_issue_ask(engine, session)
        result = await _turn(engine, session, text, "complaint")
        assert not result.get("offScript"), result
        assert result["slots"]["m_issue_description"] == text
        assert result["trace"][-1] == "n_ask_reached"
        assert "पहुंचे थे" in result["reply"]
        assert result["behaviorVersion"] == 3

    async def test_question_label_still_yields_under_v3(self, engine):
        await _at_issue_ask(engine, "c-q")
        result = await _turn(engine, "c-q", STORY_HI, "question")
        assert not result.get("offScript"), result
        assert result["slots"]["m_issue_description"] == STORY_HI

    async def test_labelled_and_unlabelled_turns_agree(self, engine):
        await _at_issue_ask(engine, "c-a")
        labelled = await _turn(engine, "c-a", STORY_HI, "complaint")
        await _at_issue_ask(engine, "c-b")
        unlabelled = await _turn(engine, "c-b", STORY_HI, None)
        assert labelled["trace"] == unlabelled["trace"]
        assert labelled["slots"] == unlabelled["slots"]

    @pytest.mark.parametrize("text", [
        "mera paisa kyun kata?", "ye deduction kyun hua", "kya aap refund kar doge",
    ])
    async def test_complaint_shaped_as_a_question_keeps_the_guard(self, engine, text):
        session = f"c-qs-{hash(text)}"
        await _at_issue_ask(engine, session)
        result = await _turn(engine, session, text, "complaint")
        assert result["trace"] == ["n_ask_issue"]
        assert result["reply"] == "माफ़ कीजिए, समझ नहीं पाया। क्या हुआ था?"
        assert "m_issue_description" not in result["slots"]

    @pytest.mark.parametrize("text", ["galat hai", "bahut bura", "nahi"])
    async def test_short_complaints_do_not_fill(self, engine, text):
        session = f"c-s-{hash(text)}"
        await _at_issue_ask(engine, session)
        result = await _turn(engine, session, text, "complaint")
        assert result["trace"] == ["n_ask_issue"]
        assert "m_issue_description" not in result["slots"]

    @pytest.mark.parametrize("signal", ["clarify", "hold", "agent_request"])
    async def test_flow_control_labels_still_park_the_turn(self, engine, signal):
        session = f"c-fc-{signal}"
        await _at_issue_ask(engine, session)
        result = await _turn(engine, session, STORY_HI, signal)
        assert result["trace"] == ["n_ask_issue"]
        assert "m_issue_description" not in result["slots"]

    async def test_matcher_ask_is_unaffected(self, engine):
        await _at_issue_ask(engine, "c-m")
        await _turn(engine, "c-m", STORY_HI, "complaint")
        result = await _turn(engine, "c-m", STORY_HI, "complaint")
        assert result["trace"] == ["n_ask_reached"]
        assert result["reply"] == "माफ़ कीजिए, समझ नहीं पाया। पहुंचे थे?"
        assert "m_reached" not in result["slots"]


class TestBehaviorVersionTwoIsFrozen:
    async def test_v2_definition_still_parks_a_complaint(self, v2_engine):
        await _at_issue_ask(v2_engine, "v2-c")
        result = await _turn(v2_engine, "v2-c", STORY_HI, "complaint")
        assert result["trace"] == ["n_ask_issue"]
        assert result["reply"] == "माफ़ कीजिए, समझ नहीं पाया। क्या हुआ था?"
        assert "m_issue_description" not in result["slots"]
        assert result["behaviorVersion"] == 2
