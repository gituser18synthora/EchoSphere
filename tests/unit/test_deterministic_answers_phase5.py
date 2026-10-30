"""Phase 5 Change 3: deterministic coverage — choice asks answered with an
authored synonym, and intent hubs answered with one edge's own token
("aaj", "wireline", "pata nahi"). The engine reports a choice ask's synonyms
(awaitingAsk) and a hub's edges (awaitingEdges); the brain reads them.

Replay gate (recorded demo decisions, 175 workflow turns): 35 taken, 34
agree (97.1 %), 0 yes/no flips; the extension took 5 more turns than the
Phase 4 rule, all agreeing.
"""

import pytest

from shared.orchestration.ask_resolution import ask_answer_shape
from shared.orchestration.deterministic_answers import (
    ASK_KIND_HUB,
    RESOLVER_CHOICE,
    RESOLVER_EDGE,
    RESOLVER_YES_NO,
    resolve_short_answer,
)
from shared.orchestration.workflow_engine import _awaiting_edges

from tests.unit.test_deterministic_answers import (
    _AgenticLLMStub,
    _Workflows,
    _brain,
    _events,
    _heard,
    _pause_at,
    _result,
    turn,
)

EDGES = [
    {"tokens": ["aaj", "आज", "aaj hi", "आज ही"], "signals": []},
    {"tokens": ["kal", "कल"], "signals": []},
    {"tokens": ["haan", "yes"], "signals": ["affirm"]},
    {"tokens": ["nahi", "no"], "signals": ["refusal"]},
]


class TestHubEdges:
    @pytest.mark.parametrize("text,value", [("आज", "आज"), ("aaj.", "aaj"), ("haan aaj", "aaj"),
                                            ("ji, kal", "kal"), ("Aaj hi", "aaj")])   # "hi" is a filler
    def test_one_edges_token_said_alone(self, text, value):
        answer = resolve_short_answer(text, ASK_KIND_HUB, edges=EDGES)
        assert (answer.resolver, answer.value, answer.signal) == (RESOLVER_EDGE, value, None)

    def test_a_yes_or_no_needs_an_edge_with_that_signal(self):
        assert resolve_short_answer("हाँ", ASK_KIND_HUB, edges=EDGES).signal == "affirm"
        assert resolve_short_answer("nahi ji", ASK_KIND_HUB, edges=EDGES).resolver == RESOLVER_YES_NO
        assert resolve_short_answer("हाँ", ASK_KIND_HUB, edges=EDGES[:2]) is None

    @pytest.mark.parametrize("text", [
        "aaj sham",           # two edges' tokens would compete; the engine's longest-token rule decides
        "aaj nahi kal",       # more than one edge named
        "parson",             # no edge
        "aaj kab aaoge?",     # a question
        "aaj ruko",           # a hold
    ])
    def test_anything_else_stays_with_the_model(self, text):
        assert resolve_short_answer(text, ASK_KIND_HUB, edges=EDGES) is None

    def test_a_token_shared_by_two_edges_is_ambiguous(self):
        edges = [{"tokens": ["correction"], "signals": []}, {"tokens": ["correction", "galat"], "signals": []}]
        assert resolve_short_answer("correction", ASK_KIND_HUB, edges=edges) is None


class TestChoiceSynonyms:
    OPTIONS = ("guard / security", "father", "left at door")
    SYNONYMS = {"guard / security": ["guard ko", "guard", "security ko"], "father": ["papa", "papa ko"],
                "left at door": ["darwaze par", "door par"]}

    @pytest.mark.parametrize("text,value", [("guard ko", "guard / security"), ("Papa.", "father"),
                                            ("haan, darwaze par", "left at door"), ("ji security ko", "guard / security")])
    def test_a_synonym_names_the_option(self, text, value):
        answer = resolve_short_answer(text, "choice", options=self.OPTIONS, synonyms=self.SYNONYMS)
        assert (answer.resolver, answer.value) == (RESOLVER_CHOICE, value)

    @pytest.mark.parametrize("text", ["guard ko diya tha", "papa ya mummy", "nahi"])
    def test_more_than_a_synonym_stays_with_the_model(self, text):
        assert resolve_short_answer(text, "choice", options=self.OPTIONS, synonyms=self.SYNONYMS) is None

    def test_a_surface_shared_by_two_options_is_ambiguous(self):
        synonyms = {"father": ["ghar wale"], "left at door": ["ghar wale"]}
        assert resolve_short_answer("ghar wale", "choice", options=self.OPTIONS, synonyms=synonyms) is None


class TestTheEngineReports:
    def test_a_choice_asks_synonyms(self):
        node = {"id": "ask", "kind": "ask", "config": {
            "variable": "handoff", "question": "किसको दिया?",
            "entity": {"dataType": "text", "allowedValues": ["guard", "father"],
                       "synonyms": {"guard": ["guard ko", ""], "father": ["papa"], "stranger": ["koi aur"]}},
        }}
        shape = ask_answer_shape(node, "handoff")
        assert shape["kind"] == "choice"
        # Every canonical with surfaces is an option; blank surfaces are dropped.
        assert shape["synonyms"] == {"guard": ["guard ko"], "father": ["papa"], "stranger": ["koi aur"]}

    def test_a_hubs_edges(self):
        definition = {
            "nodes": [{"id": "hub", "kind": "intent"}, {"id": "a", "kind": "end"}, {"id": "b", "kind": "end"},
                      {"id": "c", "kind": "end"}],
            "edges": [{"from": "hub", "to": "a", "label": "aaj / आज"},
                      {"from": "hub", "to": "b", "label": "haan / yes"},
                      {"from": "hub", "to": "c", "label": "else"}],
        }
        assert _awaiting_edges(definition, definition["nodes"][0]) == [
            {"tokens": ["aaj", "आज"], "signals": []}, {"tokens": ["haan", "yes"], "signals": ["affirm"]},
        ]
        assert _awaiting_edges(definition, definition["nodes"][1]) is None


class TestTheBrain:
    async def test_an_edge_token_takes_the_hubs_edge_without_the_decision(self):
        llm, workflows = _AgenticLLMStub([]), _Workflows([_result()])
        brain = _brain(llm, workflows)
        _pause_at(brain)
        brain._pending_hub_edges = EDGES
        await turn(brain, "आज।")
        assert llm.generate_calls == []
        assert workflows.calls[0]["signal"] is None           # the engine matches the token itself
        assert _events(brain, "decision_skipped_deterministic") == [{
            "resolver": RESOLVER_EDGE, "ask_kind": ASK_KIND_HUB, "signal": None, "value": "आज",
        }]

    async def test_the_next_hubs_edges_come_from_the_engine(self):
        llm = _AgenticLLMStub([])
        workflows = _Workflows([_result(signals=["affirm", "refusal"]) | {"awaitingEdges": EDGES}, _result()])
        brain = _brain(llm, workflows)
        _pause_at(brain, ask={"kind": "yes_no", "options": []})
        await turn(brain, "हाँ।")
        assert brain._pending_hub_edges == EDGES
        assert brain._deterministic_answer("kal").value == "kal"

    async def test_a_synonym_at_a_choice_ask(self):
        ask = {"kind": "choice", "options": ["guard / security", "father"],
               "synonyms": {"guard / security": ["guard ko"], "father": ["papa"]}}
        llm = _AgenticLLMStub([{"signal": None, "scope": "in_scope", "next_action": "continue_workflow"}])
        workflows = _Workflows([_result(ask=ask), _result()])
        brain = _brain(llm, workflows)
        _pause_at(brain, ask=ask)
        await turn(brain, "papa ko diya tha")
        assert len(llm.generate_calls) == 1                   # more than the synonym: the model decides
        await _heard(brain)
        await turn(brain, "papa")
        assert len(llm.generate_calls) == 1                   # the synonym alone: no decision call
        assert workflows.calls[1]["user_text"] == "papa"

    async def test_a_rewound_turn_restores_the_edges(self):
        class _Rewindable(_Workflows):
            async def rollback_last_turn(self, **kwargs):
                return True

        workflows = _Rewindable([_result(ask={"kind": "number", "options": []})])
        brain = _brain(_AgenticLLMStub([]), workflows)
        _pause_at(brain)
        brain._pending_hub_edges = EDGES
        await turn(brain, "आज।")
        assert brain._pending_hub_edges is None
        await brain._rollback_open_turn(("आज।", None, ("wf_1", "wf_1", "आज।")))
        assert brain._pending_hub_edges == EDGES
