"""The configuration rollout works before and after the shared platform gate."""
import copy
import re

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as we
from tenant.Wordline.setup.identifier_confirmation import ASKS, YES, repair_confirmation_nodes, upgrade


def original():
    nodes = [{"id": "start", "kind": "start", "config": {}},
             {"id": "end", "kind": "end", "config": {}}]
    edges = [{"from": "start", "to": ASKS[0]}]
    for ask in ASKS:
        variable = "spoken_terminal_id_retry" if "retry" in ask else "spoken_terminal_id"
        nodes.extend([
            {"id": ask, "kind": "ask", "config": {"variable": variable, "question": "Terminal ID?",
                "entity": {"dataType": "text", "regexPattern": r"(\d{7,8})"}}},
            {"id": ask + "_lookup", "kind": "api", "config": {"connection": "lookup"}},
        ])
        edges.extend([{"id": ask + "_next", "from": ask, "to": ask + "_lookup"},
                      {"from": ask + "_lookup", "to": "end", "label": "success"}])
    return {"id": "wf_wordline_confirm", "version": 1, "name": "confirm", "nodes": nodes, "edges": edges}


def test_transform_is_idempotent_and_does_not_mutate_input():
    before = original()
    frozen = copy.deepcopy(before)
    after = upgrade(before, ["1132986A"])
    assert before == frozen and upgrade(after, ["1132986A"]) == after
    for ask in ASKS:
        assert next(e for e in after["edges"] if e.get("from") == ask)["to"] == ask + "__confirm"


LEGACY_YES = (r"(?i)^\s*(?=.*(?:yes|correct|right|haan|han|sahi|हाँ|हां|हा|जी|सही))"
              r"(?:(?:yes|correct|right|haan|han|haa|ha|haanji|hanji|ji|sahi|hai|bilkul|sir|"
              r"हाँ|हां|हा|हाँजी|हांजी|जी|सही|है|बिल्कुल|सर)[\s,.!।]*)+$")


def legacy_definition():
    """The old saved gate remains unchanged: a runtime fix cannot require a
    tenant to rebuild/publish its workflow before natural answers work."""
    doc = upgrade(original(), ["1132986A", "3139511A"])
    for n in doc["nodes"]:
        config = n.get("config", {})
        if config.get("confirmsIdentifier"):
            config["entity"]["synonymPatterns"]["yes"] = [LEGACY_YES]
            for localized in config.get("readback", {}).values():
                for field in localized.get("fields", []):
                    field.pop("format", None)
    return doc


def runtime(monkeypatch, doc=None):
    doc = doc or legacy_definition()
    monkeypatch.setattr(we, "load_workflow_definition", lambda *args: doc)
    wf = we.WorkflowEngine()
    saver = MemorySaver()

    async def memory():
        return saver

    monkeypatch.setattr(wf, "_get_checkpointer", memory)

    async def turn(text, *, signal=None):
        return await wf.handle_turn_detailed(
            session_id="legacy", tenant_id="tenant", bot_id="bot", workflow_name=doc["id"],
            user_text=text, signal=signal, language="en-IN", mock_tool_results={"lookup": {"ok": True}})

    return turn


def test_refresh_repairs_existing_gates_without_changing_edges_or_other_node_settings():
    doc = legacy_definition()
    doc["nodes"][2]["config"]["custom"] = {"ordered": ["a", "b"]}
    frozen = copy.deepcopy(doc)
    repaired = repair_confirmation_nodes(doc)
    assert doc == frozen
    assert repaired["edges"] == doc["edges"]
    assert [n["id"] for n in repaired["nodes"]] == [n["id"] for n in doc["nodes"]]
    assert repair_confirmation_nodes(repaired) == repaired
    assert upgrade(repaired) == repaired
    for old, new in zip(doc["nodes"], repaired["nodes"]):
        if not old.get("config", {}).get("confirmsIdentifier"):
            assert new == old
            continue
        assert new["config"]["alsoCapture"] == old["config"]["alsoCapture"]
        for localized in new["config"]["readback"].values():
            assert len(localized["question"]) < 25
            assert "format" not in localized["fields"][0]  # Compatible with the current Demo runtime.


@pytest.mark.parametrize("text", ["Okay Yes, it's correct.", "It's correct.", "Yes, that is correct.", "हाँ, सही है। हाँ।", "Confirmed", "Yes, confirmed"])
def test_refreshed_config_accepts_natural_affirmatives_before_runtime_rollout(text):
    assert re.search(YES, text)


@pytest.mark.parametrize("text", ["It's not correct.", "yes but the last letter is A", "Is that correct?", "yes 3139511A"])
def test_refreshed_config_never_treats_questions_or_corrections_as_yes(text):
    assert not re.search(YES, text)


@pytest.mark.parametrize("text", ["Okay Yes, it's correct.", "It's correct.", "Yes, that is correct.", "हाँ, सही है। हाँ।", "Confirmed", "Yes, confirmed"])
async def test_old_saved_gate_accepts_natural_affirmation_without_configuration_changes(monkeypatch, text):
    doc = legacy_definition()
    frozen = copy.deepcopy(doc)
    turn = runtime(monkeypatch, doc)
    pending = await turn("1132986A")
    assert "lookup" not in pending["trace"] and not pending["done"]
    final = await turn(text, signal="affirm")
    assert final["done"] and final["trace"].count(ASKS[0] + "_lookup") == 1
    assert final["slots"]["spoken_terminal_id"] == "1132986A"
    assert doc == frozen


@pytest.mark.parametrize("suffix,expected", [
    ("A for Alpha", "1132986A"), ("and B, B for Boy", "1132986B"),
    ("MM for mango", "1132986M"), ("ए फोर अल्फा", "1132986A"),
])
async def test_old_gate_amends_a_valid_suffix_and_requires_a_separate_confirmation(monkeypatch, suffix, expected):
    turn = runtime(monkeypatch)
    await turn("1132986")
    corrected = await turn(suffix, signal="affirm")
    assert not corrected["done"] and corrected["awaitingIdentifierConfirmation"]
    assert "lookup" not in corrected["trace"]
    assert corrected["slots"]["spoken_terminal_id"] == expected
    assert "letter " + expected[-1] in corrected["reply"]
    assert "n_ask_tid__confirmation_answer" not in corrected["slots"]
    final = await turn("It's correct.", signal="affirm")
    assert final["done"] and final["slots"]["spoken_terminal_id"] == expected


@pytest.mark.parametrize("text", ["no, 3139511A", "yes, actually 3139511A", "3139511A"])
async def test_full_correction_outranks_an_affirm_signal_and_uses_existing_no_branch(monkeypatch, text):
    turn = runtime(monkeypatch)
    await turn("1132986A")
    corrected = await turn(text, signal="affirm")
    assert not corrected["done"] and "lookup" not in corrected["trace"]
    assert "n_ask_tid__reset" in corrected["trace"]
    assert corrected["slots"]["spoken_terminal_id"] == "3139511A"
    final = await turn("yes")
    assert final["done"] and final["slots"]["spoken_terminal_id"] == "3139511A"


@pytest.mark.parametrize("text", ["Is that correct?", "why do you need it?", "yes but the last letter is A", "B or C", "B for Alpha", "double B", "okay"])
async def test_questions_and_invalid_partials_cannot_confirm_or_replace_old_candidate(monkeypatch, text):
    turn = runtime(monkeypatch)
    await turn("1132986")
    result = await turn(text, signal="affirm")
    assert not result["done"] and "lookup" not in result["trace"]
    assert result["slots"]["spoken_terminal_id"] == "1132986"
    assert result["trace"][-1] == "n_ask_tid__confirm"
    assert result["awaitingIdentifierConfirmation"]


@pytest.mark.parametrize("text", ["no", "It's not correct.", "wrong"])
async def test_negative_answer_still_clears_candidate_before_recollection(monkeypatch, text):
    turn = runtime(monkeypatch)
    await turn("1132986A")
    result = await turn(text, signal="affirm")
    assert not result["done"] and "lookup" not in result["trace"]
    assert not result["slots"].get("spoken_terminal_id")
    assert result["trace"][-1] == "n_ask_tid"
    assert not result["awaitingIdentifierConfirmation"]


async def test_suffix_must_validate_against_the_actual_identifier_schema(monkeypatch):
    doc = legacy_definition()
    for n in doc["nodes"]:
        if n["id"] == "n_ask_tid":
            n["config"]["entity"]["regexPattern"] = r"^(\d{7})$"
        if n["id"] == "n_ask_tid__confirm":
            n["config"]["alsoCapture"][1]["entity"]["regexPattern"] = r"^(\d{7})$"
    turn = runtime(monkeypatch, doc)
    await turn("1132986")
    invalid = await turn("B for Boy", signal="affirm")
    assert not invalid["done"] and invalid["slots"]["spoken_terminal_id"] == "1132986"
    assert "lookup" not in invalid["trace"]


@pytest.mark.parametrize("ask", ASKS)
async def test_explicit_gate_is_not_duplicated_by_new_runtime(monkeypatch, ask):
    doc = original()
    doc["edges"][0]["to"] = ask
    doc = upgrade(doc, ["1132986A", "3139511A"])
    monkeypatch.setattr(we, "load_workflow_definition", lambda *args: doc)
    wf = we.WorkflowEngine()
    saver = MemorySaver()
    async def memory():
        return saver
    monkeypatch.setattr(wf, "_get_checkpointer", memory)
    async def turn(text):
        return await wf.handle_turn_detailed(
            session_id=ask, tenant_id="tenant", bot_id="bot", workflow_name=doc["id"],
            user_text=text, language="en-IN", mock_tool_results={"lookup": {"ok": True}})
    result = await turn("1132986A")
    assert ask + "__confirm" in result["trace"]
    assert result["awaitingIdentifierConfirmation"]
    assert result["awaitingAsk"]["kind"] == "yes_no"
    assert result["trace"].count(ask + "__confirm") == 1
    rejected = await turn("no")
    assert "Reset" not in rejected["reply"] and "lookup" not in rejected["trace"]
    await turn("1132986A")
    result = await turn("yes, actually 3139511A")
    assert ask + "_lookup" not in result["trace"]
    assert "letter A" in result["reply"]
    final = await turn("yes")
    assert final["done"] and ask + "_lookup" in final["trace"]
    variable = "spoken_terminal_id_retry" if "retry" in ask else "spoken_terminal_id"
    assert final["slots"][variable] == "3139511A"


@pytest.mark.parametrize("rejection", ["नहीं, ये सही नहीं है।", "It's not correct.", "no"])
async def test_rejecting_an_incomplete_id_clears_it_before_the_next_dictation(monkeypatch, rejection):
    """The cv_6af0286fd750 rejection occurs before a valid ID/readback gate."""
    turn = runtime(monkeypatch)
    await turn("Please help me")
    partial = await turn("मेरा आईडी टर्मिनल आईडी है १ २ ३ ४ and B, B for boy.")
    assert partial["awaitingIdentifier"]["held_digits"] == "1234B"
    assert "4 digits" in partial["reply"]  # The letter is retained, not counted as a digit.
    assert not partial["awaitingIdentifierConfirmation"] and "lookup" not in partial["trace"]

    rejected = await turn(rejection, signal="affirm")
    assert rejected["awaitingIdentifier"]["held_digits"] == ""
    assert not rejected["slots"].get("spoken_terminal_id")
    assert not rejected["awaitingIdentifierConfirmation"] and "lookup" not in rejected["trace"]

    fresh = await turn("एक दो तीन चार पाँच")
    assert fresh["awaitingIdentifier"]["held_digits"] == "12345"
    assert "5 digits" in fresh["reply"]
    assert not fresh["slots"].get("spoken_terminal_id") and "lookup" not in fresh["trace"]
    # A suffix cannot invent the two digits still missing from this attempt.
    suffix = await turn("and B, B for Boy")
    assert not suffix["slots"].get("spoken_terminal_id")
    assert not suffix["awaitingIdentifierConfirmation"] and "lookup" not in suffix["trace"]


@pytest.mark.parametrize("question", ["Is that correct?", "Isn't it correct?", "Is it not correct?"])
async def test_a_question_or_negative_classifier_does_not_discard_a_partial_id(monkeypatch, question):
    turn = runtime(monkeypatch)
    await turn("Please help me")
    await turn("one two three four")
    pending = await turn(question, signal="deny")
    assert pending["awaitingIdentifier"]["held_digits"] == "1234"
    assert not pending["awaitingIdentifierConfirmation"] and "lookup" not in pending["trace"]
    completed = await turn("five six seven")
    assert completed["slots"]["spoken_terminal_id"] == "1234567"
    assert completed["awaitingIdentifierConfirmation"] and "lookup" not in completed["trace"]
    confirmed = await turn("yes")
    assert confirmed["done"] and confirmed["slots"]["spoken_terminal_id"] == "1234567"


async def test_partial_id_readback_counts_numeric_characters_and_retains_letters(monkeypatch):
    turn = runtime(monkeypatch)
    await turn("Please help me")
    await turn("one two three four B for Boy")
    readback = await turn("what did you note?")
    assert readback["awaitingIdentifier"]["held_digits"] == "1234B"
    assert "4 digits" in readback["reply"]
    assert "one, two, three, four, letter B" in readback["reply"]
    assert not readback["awaitingIdentifierConfirmation"] and "lookup" not in readback["trace"]
