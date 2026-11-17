"""Replay the exact four demo transcripts through real workflow/readback code.

The saved workflow's ID gate is reproduced here without customer data, database
dependencies or ignored audit files. Business tools are explicitly fake. This
does not simulate STT, audio timing, provider auto-language detection or an LLM.
"""
from __future__ import annotations

import copy

import pytest
from langgraph.checkpoint.memory import MemorySaver
from pipecat.frames.frames import TextFrame

import shared.orchestration.tool_executor as executor_module
import shared.orchestration.workflow_engine as we
from shared.orchestration.identifier_confirmation import spoken_readback
from shared.orchestration.tool_executor import ToolResult
from tenant.Wordline.setup.identifier_confirmation import NO, TID_PATTERN, YES
from tests.unit.test_identifier_capture_runtime import make_brain


CASES = [
    {
        "conversation_id": "cv_411751e0bb12", "ask": "n_ask_tid", "expected_id": "1432421A",
        "source_turns": [2, 4, 6, 8, 10, 12, 14, 16, 18],
        "merchant": {"outlet_name": "New Baby Stores", "bank_name": "SODEXO", "asset_type": "Linura 7220 Handheld GPRS"},
        "utterances": [
            "मनन, मेरे को पेपर रोल चाहिए।", "1 4 3 2 4 2 1 A for Apple", "Okay Yes, it's correct.",
            "क्या? Yes लगा दीजिए।", "हाँ, सही है। हाँ।", "हाँ।", "हाँ।", "Yes हाँ।", "और। हाँ हाँ सर सही चलता है।",
        ],
    },
    {
        "conversation_id": "cv_6af0286fd750", "ask": "n_bat_tid", "expected_id": None, "merchant": {},
        "source_turns": [2, 3, 5, 7, 9, 11],
        "utterances": [
            "हमन", "मेरा बैटरी जो है ना उसमें प्रॉब्लम है।",
            "मेरा आईडी टर्मिनल आईडी है १ २ ३ ४ and B, B for boy.", "नहीं, ये सही नहीं है।",
            "एक दो तीन चार पाँच", "and B, B for Boy",
        ],
        "chat_only": {0},
    },
    {
        "conversation_id": "cv_8fececf625d0", "ask": "n_bat_tid", "expected_id": "1132986A",
        "source_turns": [2, 4, 6, 8, 10],
        "merchant": {"outlet_name": "Care Corner Pharmacy", "bank_name": "YES BANK", "asset_type": "Antera AL9220U Handheld GPRS"},
        "utterances": [
            "मनन, मेरी बैटरी चार्ज नहीं हो रही है।",
            "मेरा टर्मिनल आई टी है वन वन थ्री टू नाइन एट सिक्स ए फोर अल्फा।",
            "हाँ। Permanent ID hai 1132986 A for Apple.", "It's correct.", "Yes",
        ],
    },
    {
        "conversation_id": "cv_e6bcef78b811", "ask": "n_bat_tid", "expected_id": "4166725M",
        "source_turns": [2, 4, 6, 8],
        "merchant": {"outlet_name": "Mukilans Lab", "bank_name": "TMB BANK", "asset_type": "Paytivo 7210 Handheld GPRS"},
        "utterances": [
            "मनन, मेरे बैटरी में इशू आ रहा है, चार्ज नहीं हो रहा है।", "416672 5 MM for mango",
            "4166725 M for Mango", "Yes",
        ],
    },
]


def replay_definition(case: dict) -> dict:
    """Small equivalent of the v51 collection/explicit-confirmation subgraph."""
    ask = case["ask"]
    variable, answer = "spoken_terminal_id", ask + "__confirmation_answer"
    entity = {"dataType": "identifier", "preserveAlphanumeric": True, "regexPattern": TID_PATTERN,
              "synonymPatterns": {"no_tid": [r"I do not have it", r"पता नहीं"]}}
    yes_no = {"dataType": "text", "synonymPatterns": {"no": [NO], "yes": [YES]}}
    question = "Please tell me your complete Terminal ID, including any letters."
    hindi = "कृपया अपनी पूरी Terminal ID, उसमें आने वाले letters के साथ बताइए। Letter हो तो A for alpha जैसे बोलिए।"
    nodes = [
        {"id": "start", "kind": "start", "config": {}},
        {"id": ask, "kind": "ask", "config": {
            "question": question, "variable": variable, "entity": entity, "responseMode": "fixed",
            "textByLanguage": {"en": question, "hi": hindi}, "identifierConfirmationNode": ask + "__confirm"}},
        {"id": ask + "__confirm", "kind": "ask", "config": {
            "question": "Is the Terminal ID I read back correct?", "variable": answer,
            "confirmsIdentifier": variable, "entity": yes_no, "responseMode": "fixed",
            # Old v51 fields deliberately omit `format`: current runtime must
            # format even an existing saved gate before configuration repair.
            "readback": {lang: {"fields": [{"variable": variable, "template": "Terminal ID {value}."}],
                                "question": tail} for lang, tail in (("en", "Is that correct?"), ("hi", "क्या यह सही है?"))},
            "alsoCapture": [
                {"variable": variable, "clear": True, "entity": {"dataType": "text", "synonymPatterns": {"clear": [NO]}}},
                {"variable": variable, "overwrite": True, "entity": copy.deepcopy(entity)},
            ],
            "unmatchedReply": "Please say yes if that ID is correct, or repeat the complete corrected ID.",
            "unmatchedReplyByLanguage": {"hi": "ID सही हो तो हाँ बोलिए, वरना पूरी सही ID दोबारा बताइए।"}}},
        {"id": ask + "__confirmed", "kind": "condition", "config": {"variable": answer, "operator": "equals", "value": "yes"}},
        {"id": ask + "__reset", "kind": "message", "config": {"text": "", "setSlots": {answer: None}, "responseMode": "fixed"}},
        {"id": "lookup", "kind": "api", "config": {"connection": "lookup"}},
    ]
    edges = [
        {"from": "start", "to": ask}, {"from": ask, "to": ask + "__confirm"},
        {"from": ask + "__confirm", "to": ask + "__confirmed"},
        {"from": ask + "__confirmed", "to": "lookup", "label": "true"},
        {"from": ask + "__confirmed", "to": ask + "__reset", "label": "false"},
        {"from": ask + "__reset", "to": ask}, {"from": "lookup", "to": "outlet", "label": "success"},
    ]
    # These downstream readback templates are copied verbatim from the
    # archived graph; only their surrounding business flow is minimized.
    templates = {
        "outlet_name": (("Sir, as per our records, your Outlet Name is {value}.", "Could you please confirm if this is correct?"),
                        ("सर, हमारे records के अनुसार, आपके outlet का नाम {value} है।", "क्या आप confirm कर सकते हैं कि ये सही है या नहीं?")),
        "bank_name": (("Is your bank name {value}?", "Please answer yes or no."),
                      ("क्या आपके bank का नाम {value} है?", "कृपया हाँ या ना में बताइए।")),
        "asset_type": (("Is your terminal model {value}?", "Please answer yes or no."),
                       ("क्या आपके terminal का model {value} है?", "कृपया हाँ या ना में बताइए।")),
    }
    for node_id, field, following in (("outlet", "outlet_name", "bank"), ("bank", "bank_name", "asset"), ("asset", "asset_type", "options")):
        nodes.append({"id": node_id, "kind": "ask", "config": {
            "variable": node_id + "_confirmed", "entity": copy.deepcopy(yes_no),
            "question": "Is this detail correct?", "responseMode": "fixed",
            "readback": {lang: {"fields": [{"variable": field, "template": template}], "question": tail}
                         for lang, (template, tail) in zip(("en", "hi"), templates[field])}}})
        edges.append({"from": node_id, "to": following})
    nodes.append({"id": "options", "kind": "intent", "config": {"prompt": "Which support option do you need?", "responseMode": "fixed"}})
    return {"id": "wf_demo_readback_replay", "version": 51, "name": "Readback replay", "nodes": nodes, "edges": edges}


async def replay_case(monkeypatch, case: dict) -> dict:
    """Return final TextFrame payloads, with fake tools explicitly identified."""
    definition = replay_definition(case)
    frozen = copy.deepcopy(definition)
    monkeypatch.setattr(we, "load_workflow_definition", lambda *args: definition)
    workflow = we.WorkflowEngine()
    saver, calls = MemorySaver(), []

    async def memory():
        return saver

    monkeypatch.setattr(workflow, "_get_checkpointer", memory)

    class FakeTools:
        async def execute(self, **kwargs):
            calls.append({"tool": kwargs["tool"], "args": copy.deepcopy(kwargs["args"]), "mocked": True})
            mapped = {"merchant_found": True, "customer_verified": True,
                      "terminal_id": kwargs["args"]["spoken_terminal_id"], **case["merchant"]}
            return ToolResult(tool="lookup", ok=True, status="ok", mocked=True, mapped=mapped, data=mapped)

    monkeypatch.setattr(executor_module, "get_tool_executor", lambda: FakeTools())
    brain = make_brain(workflow_engine=workflow)
    brain._conversation_language = "hi-IN"
    rows, started = [], False
    try:
        for index, utterance in enumerate(case["utterances"]):
            if index in case.get("chat_only", set()):
                rows.append({"user_turn": index + 1, "original_transcript_turn": case["source_turns"][index],
                             "utterance": utterance, "scope": "original chat route before workflow",
                             "workflow_reply": None, "tts_text_frames": [], "note": "LLM chat is outside this deterministic replay; no reply invented."})
                continue
            previous_calls = len(calls)
            result = await workflow.handle_turn_detailed(
                session_id=case["conversation_id"], tenant_id="test-tenant", bot_id="test-bot", workflow_name=definition["id"],
                user_text=utterance, reset_state=not started, language="hi-IN")
            started = True
            before = len(brain._pushed)
            await brain._say(result["reply"])
            tts = [frame.text for frame in brain._pushed[before:] if isinstance(frame, TextFrame)]
            rows.append({"user_turn": index + 1, "original_transcript_turn": case["source_turns"][index],
                         "utterance": utterance, "scope": "real workflow and final _say TextFrame path",
                         "workflow_reply": result["reply"], "tts_text_frames": tts,
                         "slots": copy.deepcopy(result["slots"]), "trace": result["trace"],
                         "awaiting_identifier_confirmation": result.get("awaitingIdentifierConfirmation", False),
                         "awaiting_identifier": result.get("awaitingIdentifier"),
                         "awaiting_ask": result.get("awaitingAsk"), "status": result.get("status"),
                         "off_script": result.get("offScript", False),
                         "tool_calls_this_turn": copy.deepcopy(calls[previous_calls:])})
    finally:
        await brain.cleanup()
    assert definition == frozen
    return {"conversation_id": case["conversation_id"], "expected_valid_id": case["expected_id"],
            "language": "hi-IN", "tool_mode": "fake lookup only; no external requests", "rows": rows, "tool_calls": calls}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["conversation_id"])
async def test_exact_demo_user_sequences_reach_tts_without_corrupt_id_or_early_lookup(monkeypatch, case):
    report = await replay_case(monkeypatch, case)
    expected = case["expected_id"]
    if expected is None:
        assert report["tool_calls"] == []
        assert all(not row.get("slots", {}).get("spoken_terminal_id") for row in report["rows"])
        assert all(not row.get("awaiting_identifier_confirmation") for row in report["rows"])
        # The explicit rejection belongs to the incomplete collection, too:
        # the next attempt must start fresh rather than append to 1234B.
        assert not (report["rows"][3].get("awaiting_identifier") or {}).get("held_digits")
        assert (report["rows"][4].get("awaiting_identifier") or {}).get("held_digits") == "12345"
        return
    assert len(report["tool_calls"]) == 1
    assert report["rows"][1]["awaiting_identifier_confirmation"]  # first exact ID utterance already works
    assert report["tool_calls"][0]["args"]["spoken_terminal_id"] == expected
    readbacks = [row for row in report["rows"] if row.get("awaiting_identifier_confirmation")]
    assert readbacks
    for row in readbacks:
        assert row["slots"]["spoken_terminal_id"] == expected
        final_text = "".join(row["tts_text_frames"])
        assert spoken_readback(expected) in final_text
        assert expected not in final_text  # raw dense IDs never reach TTS
        assert not row["tool_calls_this_turn"]
    assert "1132986A4" not in str(report)
    if case["conversation_id"] == "cv_411751e0bb12":
        assert report["rows"][2]["tool_calls_this_turn"]  # Okay Yes, it's correct.
    elif case["conversation_id"] == "cv_8fececf625d0":
        assert report["rows"][3]["tool_calls_this_turn"]  # It's correct.
    else:
        assert report["rows"][3]["tool_calls_this_turn"]  # Yes, after full repeat.
