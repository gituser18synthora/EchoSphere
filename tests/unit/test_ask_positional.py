"""Ordinals and deictics at a choice ask ("pehla", "dusra wala", "wo wala",
"वही") resolve against the options in the order the bot spoke them."""
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.ask_resolution as ask
import shared.orchestration.workflow_engine as we

ENTITY = {"name": "slot", "dataType": "list",
          "synonyms": {"morning": ["subah", "सुबह"], "evening": ["sham", "शाम"], "night": ["raat", "रात"]}}
PROMPT = "Aap sham ka slot lenge, subah ka, ya raat ka?"   # spoken order: evening, morning, night


class TestPositionalChoice:
    def test_ordinals_follow_the_spoken_order(self):
        assert ask.positional_choice("pehla", ENTITY, PROMPT) == "evening"
        assert ask.positional_choice("dusra", ENTITY, PROMPT) == "morning"
        assert ask.positional_choice("teesra", ENTITY, PROMPT) == "night"
        assert ask.positional_choice("aakhri", ENTITY, PROMPT) == "night"
        assert ask.positional_choice("ek number", ENTITY, PROMPT) == "evening"

    def test_wala_suffix_leading_yes_and_punctuation_are_stripped(self):
        for text in ("pehla wala", "Pehla wala.", "haan, pehla wala", "ji pehli wali", "pahla"):
            assert ask.positional_choice(text, ENTITY, PROMPT) == "evening", text
        assert ask.positional_choice("last wala", ENTITY, PROMPT) == "night"
        assert ask.positional_choice("aakhri wala", ENTITY, PROMPT) == "night"

    def test_devanagari_ordinals(self):
        assert ask.positional_choice("पहला", ENTITY, PROMPT) == "evening"
        assert ask.positional_choice("दूसरा वाला", ENTITY, PROMPT) == "morning"
        assert ask.positional_choice("तीसरा", ENTITY, PROMPT) == "night"
        assert ask.positional_choice("आखिरी वाला", ENTITY, PROMPT) == "night"

    def test_deictic_takes_the_option_mentioned_last(self):
        for text in ("wo wala", "woh wala", "ye wala", "wahi", "वो वाला", "वही", "Wo wala."):
            assert ask.positional_choice(text, ENTITY, PROMPT) == "night", text
        # mentioned last in a different prompt → different option
        assert ask.positional_choice("wo wala", ENTITY, "Subah ka ya sham ka?") == "evening"

    def test_no_answer_when_it_cannot_be_determined(self):
        assert ask.positional_choice("wo wala", ENTITY, "Kaunsa slot chahiye?") is None
        assert ask.positional_choice("chautha", ENTITY, PROMPT) is None   # out of range
        assert ask.positional_choice("pehla", {"name": "n", "dataType": "text"}, "Naam?") is None
        yes_no = {"name": "ok", "synonyms": {"yes (called)": ["haan"], "no": ["nahi"]}}
        assert ask.positional_choice("pehla", yes_no, "Call kiya tha?") is None
        assert ask.positional_choice("mujhe nahi pata", ENTITY, PROMPT) is None

    def test_options_in_spoken_order_fall_back_to_declaration_order(self):
        entity = {"name": "pay", "allowedValues": ["cash", "upi", "card"]}
        assert ask.positional_choice("dusra", entity, "Kaise pay karenge?") == "upi"
        assert ask.positional_choice("pehla", entity, "UPI ya cash?") == "upi"


# ── "hmm" at a yes/no ask ────────────────────────────────────────────────────

class TestHmmAtAsks:
    def test_bare_yes_no_reads_hmm_as_yes(self):
        assert ask._bare_yes_no("hmm") == "yes"
        assert ask._bare_yes_no("हम्म") == "yes"
        assert ask._bare_yes_no("hmm nahi") == "no"
        assert ask._bare_yes_no("hmm batao") is None

    async def test_hmm_fills_a_yes_no_ask_but_not_a_choice_ask(self, monkeypatch):
        yes_no = _definition({"entityType": "list", "synonyms": {"yes (called)": ["haan", "call kiya"],
                                                                  "no": ["nahi"]}},
                             question="Customer ko call kiya tha?")
        result = await _run(_engine(monkeypatch, yes_no), "h1", "start", "hmm")
        assert result["slots"]["slot"] == "yes (called)"
        choice = _definition({"entityType": "list", "synonyms": ENTITY["synonyms"]})
        result = await _run(_engine(monkeypatch, choice), "h2", "start", "hmm")
        assert "slot" not in result["slots"] and result["done"] is False


# ── through the workflow engine ──────────────────────────────────────────────

def _definition(ask_config: dict, question="Aap sham ka slot lenge, subah ka, ya raat ka?"):
    return {"id": "wf_pos", "name": "pos_wf", "version": 1, "nodes": [
        {"id": "n_start", "kind": "start", "config": {}},
        {"id": "n_ask", "kind": "ask", "config": {"question": question, "variable": "slot", **ask_config}},
        {"id": "n_end", "kind": "end", "config": {"text": "Done."}},
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


async def _run(engine, session, *texts):
    result = None
    for i, text in enumerate(texts):
        result = await engine.handle_turn_detailed(
            session_id=session, tenant_id="tn", bot_id="bot", workflow_name="pos_wf",
            user_text=text, language="hi-IN", reset_state=(i == 0))
    return result


class TestPositionalAnswersFillTheAsk:
    async def test_ordinal_fills_the_slot(self, monkeypatch):
        engine = _engine(monkeypatch, _definition({"entityType": "list", "synonyms": ENTITY["synonyms"]}))
        result = await _run(engine, "s1", "start", "dusra wala")
        assert result["slots"]["slot"] == "morning"
        assert result["done"] is True

    async def test_deictic_fills_the_slot_with_the_last_mentioned_option(self, monkeypatch):
        engine = _engine(monkeypatch, _definition({"entityType": "list", "synonyms": ENTITY["synonyms"]}))
        result = await _run(engine, "s2", "start", "wo wala")
        assert result["slots"]["slot"] == "night"

    async def test_free_text_ask_is_not_affected(self, monkeypatch):
        engine = _engine(monkeypatch, _definition({"entityType": "text"}, question="Aapka naam?"))
        result = await _run(engine, "s3", "start", "pehla")
        assert result["slots"]["slot"] == "pehla"   # verbatim free text, as before

    async def test_unresolvable_deictic_re_asks(self, monkeypatch):
        engine = _engine(monkeypatch, _definition(
            {"entityType": "list", "synonyms": ENTITY["synonyms"]}, question="Kaunsa slot chahiye?"))
        result = await _run(engine, "s4", "start", "wo wala")
        assert "slot" not in result["slots"]
        assert result["done"] is False and result["nodePrompt"] == "Kaunsa slot chahiye?"
