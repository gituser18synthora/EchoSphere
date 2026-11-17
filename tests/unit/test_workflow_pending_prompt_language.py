"""The off-script resume must not repeat an English source question in Hindi."""
import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as we


@pytest.mark.parametrize("initial_language", ["hi-IN", "en-IN"])
async def test_context_question_exports_current_language_without_changing_capture(monkeypatch, initial_language):
    hindi = "कृपया अपनी पूरी Terminal ID, उसमें आने वाले letters के साथ बताइए।"
    english = "Please tell me your complete Terminal ID, including any letters."
    definition = {"id": "wf_pending_language", "version": 1, "nodes": [
        {"id": "start", "kind": "start"},
        {"id": "collect", "kind": "ask", "config": {
            "variable": "terminal_id", "question": english,
            "textByLanguage": {"en": english, "hi": hindi},
            "entity": {"dataType": "identifier", "regexPattern": r"^\d{7,8}[A-Z]?$"},
        }},
    ], "edges": [{"from": "start", "to": "collect"}]}
    monkeypatch.setattr(we, "load_workflow_definition", lambda *args: definition)
    engine, saver = we.WorkflowEngine(), MemorySaver()

    async def memory():
        return saver

    monkeypatch.setattr(engine, "_get_checkpointer", memory)
    args = dict(session_id="pending-language", tenant_id="test", bot_id="test", workflow_name=definition["id"])
    initial = await engine.handle_turn_detailed(**args, user_text="start", language=initial_language)
    assert initial["nodePrompt"] == (hindi if initial_language == "hi-IN" else english)
    result = await engine.handle_turn_detailed(**args, user_text="Why do you need the ID?",
                                               language="hi-IN", pause_for_context=True)
    assert result["nodePrompt"] == hindi
    assert result["offScript"] is True
    assert result["slots"] == initial["slots"]
    assert result["awaitingIdentifier"] == initial["awaitingIdentifier"]


@pytest.mark.parametrize("utterance", [
    "कौन सा नोट किया है फिर से बोलिए एक बार।",
    "क्या नोट किया है बताओ तो इसे",
])
async def test_partial_id_readback_outranks_classifier_context_pause(monkeypatch, utterance):
    definition = {"id": "wf_partial_readback", "version": 1, "nodes": [
        {"id": "start", "kind": "start"},
        {"id": "collect", "kind": "ask", "config": {
            "variable": "terminal_id", "question": "Please tell me your complete ID.",
            "textByLanguage": {"hi": "कृपया अपनी पूरी ID बताइए।"},
            "entity": {"dataType": "identifier", "regexPattern": r"^\d{7,8}[A-Z]?$"},
        }},
    ], "edges": [{"from": "start", "to": "collect"}]}
    monkeypatch.setattr(we, "load_workflow_definition", lambda *args: definition)
    engine, saver = we.WorkflowEngine(), MemorySaver()

    async def memory():
        return saver

    monkeypatch.setattr(engine, "_get_checkpointer", memory)
    args = dict(session_id="partial-readback", tenant_id="test", bot_id="test",
                workflow_name=definition["id"], language="hi-IN")
    await engine.handle_turn_detailed(**args, user_text="start")
    partial = await engine.handle_turn_detailed(**args, user_text="One Two Three Four B, B for ball.")
    assert partial["awaitingIdentifier"]["held_digits"] == "1234B"
    result = await engine.handle_turn_detailed(**args, user_text=utterance,
                                               signal="question", pause_for_context=True)
    assert result["offScript"] is False
    assert "one, two, three, four, letter B" in result["reply"]
    assert result["slots"] == partial["slots"]
    assert result["awaitingIdentifier"]["held_digits"] == "1234B"
    assert result["awaitingIdentifierConfirmation"] is False
