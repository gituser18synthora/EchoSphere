"""Intent hub opt-in ``optionHub`` (workflow_engine._choose_intent_edge_detailed).

An option hub asks the caller to pick one of several options whose nodes
explain that option.  There a question naming an option selects it, and an
option named next to a generic yes/no beats the yes/no edge.  Without the
flag every existing behaviour is unchanged (pinned below).
"""

from __future__ import annotations

import pytest

import shared.orchestration.workflow_engine as wfe

EDGES = [
    {"id": "e_sms", "to": "sms", "label": "sms/sms wala batao/sms process/tell me about sms"},
    {"id": "e_ivr", "to": "ivr", "label": "ivr/ivr wala batao/ivr process/tell me about ivr"},
    {"id": "e_both", "to": "both", "label": "dono/both/sms and ivr"},
    {"id": "e_close", "to": "end", "label": "no/nahi/nothing else/that's all/bas itna hi"},
]


@pytest.fixture(scope="module")
def meta():
    return wfe._edge_meta(EDGES)


def choose(meta, text, signal, option_hub):
    edge, why, token = wfe._choose_intent_edge_detailed(meta, text, signal, option_hub=option_hub)
    return (edge or {}).get("to"), why, token


class TestQuestionLabel:
    def test_default_keeps_question_shaped_option_off_script(self, meta):
        assert choose(meta, "Tell me the IVR process.", "question", False) == (None, "off_script", "")

    def test_option_hub_selects_the_named_option(self, meta):
        assert choose(meta, "Tell me the IVR process.", "question", True) == ("ivr", "token", "ivr process")
        assert choose(meta, "How does the SMS process work?", "question", True)[0] == "sms"

    def test_question_naming_no_option_stays_off_script(self, meta):
        for text in ("Who is the contact person?", "Which bank is my terminal with?"):
            assert choose(meta, text, "question", True) == (None, "off_script", "")

    def test_other_labels_keep_their_guard(self, meta):
        assert choose(meta, "IVR se complaint hai", "complaint", True) == (None, "off_script", "")


class TestGenericLabel:
    def test_bare_decline_takes_the_supporting_edge(self, meta):
        for flag in (False, True):
            assert choose(meta, "No", "refusal", flag)[:2] == ("end", "signal")

    def test_named_option_beats_the_decline_only_on_an_option_hub(self, meta):
        assert choose(meta, "nahi, IVR batao", "refusal", False)[0] == "end"   # unchanged default
        assert choose(meta, "nahi, IVR batao", "refusal", True) == ("ivr", "token", "ivr")
        assert choose(meta, "No, tell me about SMS", "refusal", True)[0] == "sms"

    def test_longer_specific_closing_token_beats_a_shorter_option(self, meta):
        # "that's all" (specific, 10) vs "sms" (3): the caller is closing.
        assert choose(meta, "That's all, I already know SMS", "refusal", True)[0] == "end"

    def test_closing_phrase_without_label_is_literal(self, meta):
        assert choose(meta, "Nothing else", None, True)[:2] == ("end", "token")


async def test_engine_reads_the_node_flag(monkeypatch):
    from langgraph.checkpoint.memory import MemorySaver

    def definition(flag):
        hub_config = {"prompt": "SMS or IVR — which would you like to know about?"}
        if flag:
            hub_config["optionHub"] = True
        return {"id": "wf_opt", "version": 1, "name": "Options", "nodes": [
            {"id": "n_start", "kind": "start", "label": "Start"},
            {"id": "n_hub", "kind": "intent", "label": "Options", "config": hub_config},
            {"id": "sms", "kind": "end", "label": "SMS", "config": {"text": "SMS steps."}},
            {"id": "ivr", "kind": "end", "label": "IVR", "config": {"text": "IVR steps."}},
            {"id": "both", "kind": "end", "label": "Both", "config": {"text": "Both."}},
            {"id": "end", "kind": "end", "label": "Bye", "config": {"text": "Goodbye."}},
        ], "edges": [{"id": "e0", "from": "n_start", "to": "n_hub"},
                     *[{**e, "from": "n_hub"} for e in EDGES]]}

    async def _mem(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem)
    for flag, expected in ((False, ["n_hub"]), (True, ["n_hub", "ivr"])):
        monkeypatch.setattr(wfe, "load_workflow_definition",
                            lambda tenant_id, bot_id, name, pinned_versions=None, f=flag: definition(f))
        engine = wfe.WorkflowEngine()
        session = f"opt-{flag}"
        await engine.handle_turn_detailed(session_id=session, tenant_id="t", bot_id="b",
                                          workflow_name="wf_opt", user_text="hello")
        r = await engine.handle_turn_detailed(session_id=session, tenant_id="t", bot_id="b",
                                              workflow_name="wf_opt", user_text="Tell me the IVR process.",
                                              signal="question")
        assert r["trace"] == expected
