"""Ask-node resolution pipeline: default order, node-level resolver selection,
extension-registered resolvers, and the context's derived flags."""
import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.ask_resolution as ask
import shared.orchestration.workflow_engine as we


def _definition(ask_config: dict):
    return {"id": "wf_ask", "name": "ask_wf", "version": 1, "nodes": [
        {"id": "n_start", "kind": "start", "config": {}},
        {"id": "n_ask", "kind": "ask", "config": {"question": "Kaunsa city?", "variable": "city",
                                                  "entityType": "text", **ask_config}},
        {"id": "n_end", "kind": "end", "config": {}},
    ], "edges": [{"id": "e0", "from": "n_start", "to": "n_ask"}, {"id": "e1", "from": "n_ask", "to": "n_end"}]}


def _engine(monkeypatch, definition):
    monkeypatch.setattr(we, "load_workflow_definition", lambda t, b, n: definition)
    wf = we.WorkflowEngine()

    async def _memory():
        if wf._checkpointer is None:
            wf._checkpointer = MemorySaver()
        return wf._checkpointer

    monkeypatch.setattr(wf, "_get_checkpointer", _memory)
    return wf


async def _run(engine, *texts, signal=None):
    result = None
    for i, text in enumerate(texts):
        result = await engine.handle_turn_detailed(
            session_id="s", tenant_id="tn", bot_id="bot", workflow_name="ask_wf",
            user_text=text, signal=signal if i else None, language="hi-IN", reset_state=(i == 0))
    return result


class TestDefaults:
    def test_default_resolver_order_is_the_historical_ladder(self):
        assert ask.DEFAULT_RESOLVERS == ("joint_yes_no", "semantic_slots", "narrative_guard", "standard")
        assert [o.__name__ for o in ask.OUTCOMES] == [
            "outcome_handled", "outcome_filled", "outcome_joint_partial", "outcome_digits_overflow",
            "outcome_digits_partial", "outcome_semantic_reask", "outcome_signal_unmatched",
            "outcome_captured_other_field", "outcome_retry"]

    def test_node_without_selection_uses_defaults(self):
        stages = ask.resolvers_for({"config": {}})
        assert [s.__name__ for s in stages] == ["resolve_joint_yes_no", "resolve_semantic_slots",
                                                 "resolve_narrative_guard", "resolve_standard"]

    def test_unknown_names_are_ignored_and_empty_selection_falls_back(self):
        assert [s.__name__ for s in ask.resolvers_for({"config": {"askResolvers": ["standard", "nope"]}})] == ["resolve_standard"]
        assert len(ask.resolvers_for({"config": {"askResolvers": ["nope"]}})) == 4


class TestNodeLevelSelection:
    async def test_free_text_ask_fills_with_standard_resolver(self, monkeypatch):
        engine = _engine(monkeypatch, _definition({}))
        result = await _run(engine, "haan", "Pune se bol raha hoon")
        assert result["slots"]["city"] == "Pune se bol raha hoon"
        assert result["done"] is True

    async def test_extension_resolver_registered_by_name_runs_first(self, monkeypatch):
        seen = []

        def uppercase_city(ctx):
            seen.append(ctx.text)
            ctx.value = ctx.text.strip().upper()
            return True  # claim: standard never runs

        ask.register_resolver("test_upper", uppercase_city)
        try:
            engine = _engine(monkeypatch, _definition({"askResolvers": ["test_upper", "standard"]}))
            result = await _run(engine, "haan", "pune")
        finally:
            ask._RESOLVERS.pop("test_upper", None)
        assert seen == ["pune"]
        assert result["slots"]["city"] == "PUNE"

    async def test_off_script_signal_still_parks_the_turn(self, monkeypatch):
        engine = _engine(monkeypatch, _definition({}))
        result = await _run(engine, "haan", "kitna charge lagega?", signal="question")
        assert result["offScript"] is True
        assert "city" not in result["slots"]


class TestContextFlags:
    def test_semantic_flags_off_without_provider(self):
        ctx = ask.AskContext(
            node={"id": "n_ask", "config": {"variable": "city"}}, node_id="n_ask", text="x", signal="hold",
            lang="hi-IN", slots={}, audit=[], pending_digits={}, node_retries={}, replies=[],
            behavior=we.WorkflowBehavior(), ask_question=lambda n, r, l: "q", unmatched_reply=lambda n, s, l: "",
            next_of=lambda n: None, fallback_target=lambda n: None,
        )
        assert ctx.variable == "city" and ctx.guarded is True
        assert not ctx.semantic_active and not ctx.semantic_ask and not ctx.narrative_ask
        assert ctx.awaiting == "n_ask" and ctx.current is None
