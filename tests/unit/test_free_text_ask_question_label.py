"""A free-text ask must store a statement even when the LLM labels it 'question'.

Live Zepto MDND call cv_7786bc42deca (bot_59a84478f155, wf_7e4cf166c7bd v15,
2026-09-17): at "बताइए — क्या हुआ था?" the partner said "Are maine product
deliver kar diya phir bhi mera MDND mark do hai" (twice). The intent classifier
labelled both turns ``question``; the engine parked the ask off-script and the
LLM, told a question was pending, answered "माफ़ कीजिए, मैं आपकी बात ठीक से समझ
नहीं पाया … बताइए — क्या हुआ था?" both times. A bare "Haan" was then accepted as
the issue description.
"""

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as we
from shared.orchestration.router import looks_like_question

DEFINITION = {
    "id": "wf_mdnd", "name": "mdnd", "version": 1,
    "nodes": [
        {"id": "n_start", "kind": "start", "config": {}},
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

NARRATIVE = "Are maine product deliver kar diya phir bhi mera MDND mark do hai"


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


async def _turn(engine, session, text, signal, reset=False):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn", bot_id="bot", workflow_name="mdnd",
        user_text=text, signal=signal, language="hi-IN", reset_state=reset,
    )


async def _at_issue_ask(engine, session):
    entered = await _turn(engine, session, "haan boliye", "affirm", reset=True)
    assert entered["trace"] == ["n_start", "n_ask_issue"], entered
    return entered


class TestStatementLabelledQuestionIsTheAnswer:
    @pytest.mark.parametrize("text", [
        NARRATIVE,
        "Are maine product deliver kar diya hai tab bhi mera MD MD mark hua hai",
        "maine order customer ko de diya tha",
        "I delivered the order but still got marked MDND",
    ])
    async def test_narrative_fills_the_free_text_slot(self, engine, text):
        await _at_issue_ask(engine, f"s-{hash(text)}")
        result = await _turn(engine, f"s-{hash(text)}", text, "question")
        assert not result.get("offScript"), result
        assert result["slots"]["m_issue_description"] == text
        assert result["trace"][-1] == "n_ask_reached"
        assert "पहुंचे थे" in result["reply"]

    async def test_same_text_without_label_agrees(self, engine):
        await _at_issue_ask(engine, "s-a")
        labelled = await _turn(engine, "s-a", NARRATIVE, "question")
        await _at_issue_ask(engine, "s-b")
        unlabelled = await _turn(engine, "s-b", NARRATIVE, None)
        assert labelled["trace"] == unlabelled["trace"]
        assert labelled["slots"] == unlabelled["slots"]

    @pytest.mark.parametrize("text", [
        "MDND kya hota hai?", "ye deduction kyun hua", "kya aap refund kar doge",
        "is it refunded", "do you have my ticket details",
    ])
    async def test_real_question_stays_off_script(self, engine, text):
        await _at_issue_ask(engine, f"s-q-{hash(text)}")
        result = await _turn(engine, f"s-q-{hash(text)}", text, "question")
        assert result.get("offScript") is True, result
        assert result["trace"] == ["n_ask_issue"]
        assert "m_issue_description" not in result["slots"]

    @pytest.mark.parametrize("text", ["haan", "theek hai", "ji"])
    async def test_short_generic_words_under_the_label_do_not_fill(self, engine, text):
        await _at_issue_ask(engine, f"s-s-{hash(text)}")
        result = await _turn(engine, f"s-s-{hash(text)}", text, "question")
        assert result.get("offScript") is True
        assert "m_issue_description" not in result["slots"]

    async def test_complaint_label_still_parks_the_turn(self, engine):
        await _at_issue_ask(engine, "s-c")
        result = await _turn(engine, "s-c", NARRATIVE, "complaint")
        # Only the 'question' label yields; other off-script labels keep the
        # ask's authored guard (fixed reply here, off-script without one).
        assert result["trace"] == ["n_ask_issue"]
        assert result["reply"] == "माफ़ कीजिए, समझ नहीं पाया। क्या हुआ था?"
        assert "m_issue_description" not in result["slots"]

    async def test_matcher_ask_is_unaffected(self, engine):
        # A yes/no ask keeps its authored unmatched reply for a statement that
        # does not answer it (deliver ≠ reached, user decision cv_b800).
        await _at_issue_ask(engine, "s-m")
        await _turn(engine, "s-m", NARRATIVE, "question")
        result = await _turn(engine, "s-m", NARRATIVE, "question")
        assert result["trace"] == ["n_ask_reached"]
        assert result["reply"] == "माफ़ कीजिए, समझ नहीं पाया। पहुंचे थे?"
        assert "m_reached" not in result["slots"]


class TestLooksLikeQuestionHinglishAuxiliaries:
    @pytest.mark.parametrize("text,expected", [
        (NARRATIVE, False),                          # "Are" = अरे, "do" = STT slip
        ("is baar maine deliver kar diya", False),   # "is" = यह
        ("customer ne bola bahar rakh do", False),   # "do" = दो
        ("maine bola will call you later", False),
        ("Are you there", True),
        ("is it refunded", True),
        ("do you support Tally", True),
        ("Delivered it. Can I get a refund", True),  # clause-initial after a stop
        ("kya hua tha", True),
        ("MDND क्या होता है", True),
        ("kuch nahi?", True),
    ])
    def test_shape(self, text, expected):
        assert looks_like_question(text) is expected
