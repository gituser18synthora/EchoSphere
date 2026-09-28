from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import MemorySaver

from shared.orchestration.router import TurnRouter, RouteKind
from shared.orchestration.workflow_engine import build_definition_graph
from shared.orchestration.value_readback import configured_readback


LOCAL_CALL_REQUESTS = [
    "Mera number pehle phir se repeat karo mujhe OTP nahi aaya.",
    "Nahi OTP mujhe nahi aaya aapne kya number note down kiya hai mujhe bata sakti ho",
    "Are mujhe OTP nahi aaya hai. Mujhe lagta hai ki aapne number galat likha hai. Ek baar number mujhe repeat karke bata sakte ho?",
]


def definition(mode="full"):
    return {
        "id": "wf_example", "version": 1,
        "nodes": [
            {"id": "start", "kind": "start"},
            {"id": "contact", "kind": "ask", "config": {
                "variable": "contact", "question": "Your phone number?",
                "entity": {"dataType": "phone", "regexPattern": r"(?<!\d)(\d{10})(?!\d)", "pii": True},
                "valueReadback": {
                    "aliases": ["mobile", "phone number", "मोबाइल", "booking reference"],
                    "mode": mode, "speakDigits": True,
                    "responses": {
                        "en": {"template": "You gave {value}.", "missing": "No number collected yet."},
                        "hi": {"template": "आपने {value} बताया था।", "missing": "अभी नंबर नहीं मिला है।"},
                    },
                },
            }},
            {"id": "code", "kind": "ask", "config": {
                "variable": "code", "question": "Please say the six digit code.",
                "entity": {"dataType": "text", "regexPattern": r"(?<!\d)(\d{6})(?!\d)"},
                "rejectAnswerPatterns": [r"\bmobile\b|मोबाइल"],
                "rejectedAnswerReply": "That is a phone number; please give the code.",
            }},
            {"id": "hub", "kind": "intent", "config": {"prompt": "How may I help?"}},
        ],
        "edges": [{"from": "start", "to": "contact"}, {"from": "contact", "to": "code"},
                  {"from": "code", "to": "hub"}],
    }


@pytest.mark.parametrize("text", [
    "Maine jo mobile number bataya usko thoda repeat karoge kya",
    "Please repeat my phone number", "Repeat what I told you",
    "Please repeat the booking reference",
    "मेरा मोबाइल नंबर रिपीट करो",
])
def test_value_repeat_reaches_workflow(text):
    assert TurnRouter().decide(text, active_workflow="wf").kind == RouteKind.WORKFLOW


@pytest.mark.parametrize("text", ["repeat", "please repeat your question", "say that again", "pardon"])
def test_plain_repeat_still_repeats_bot(text):
    assert TurnRouter().decide(text, active_workflow="wf").action == "repeat"


@pytest.mark.parametrize("text,action", [
    ("Repeat my phone number and hang up", "hangup"),
    ("repeat my phone number and connect me to an agent", "transfer"),
])
def test_escape_hatches_win(text, action):
    assert TurnRouter().decide(text, active_workflow="wf").action == action


async def call(graph, text, session="s", language="en-IN"):
    return await graph.ainvoke({"user_text": text, "language": language},
                              {"configurable": {"thread_id": session}})


@pytest.mark.parametrize("text,language", [
    ("Ma'am, can you confirm my mobile number once?", "en-IN"),
    ("Maine jo mobile number bataya usko thoda repeat karoge kya", "hi-IN"),
    ("Mujhe jaanna hai ki jo maine mobile number bataya vo Use thoda batao phir se.", "hi-IN"),
    ("मेरा मोबाइल नंबर फिर से बताइए", "hi-IN"),
    ("Please repeat the booking reference", "en-IN"),
])
async def test_readback_preserves_pending_step_and_retries(text, language):
    graph = build_definition_graph(definition(), MemorySaver())
    before = await call(graph, "1234567890")
    assert before["awaiting"] == "code"
    for _ in range(4):
        result = await call(graph, text, language=language)
        assert "1 2 3 4 5 6 7 8 9 0" in result["reply"]
        for key in ("slots", "awaiting", "node_retries", "pending_digits"):
            assert result.get(key) == before.get(key)
        assert result["response_mode"] == "fixed"
    after = await call(graph, "654321")
    assert after["awaiting"] == "hub"
    result = await call(graph, text, language=language)
    assert "1 2 3 4 5 6 7 8 9 0" in result["reply"]
    assert result["awaiting"] == "hub"


async def test_missing_and_session_isolation():
    graph = build_definition_graph(definition(), MemorySaver())
    await call(graph, "1234567890", session="a")
    await call(graph, "hello", session="b")
    result = await call(graph, "confirm my mobile number", session="b")
    assert result["reply"] == "No number collected yet."
    assert "contact" not in result["slots"]
    assert result["awaiting"] == "contact"


async def test_wrong_field_digits_are_not_code_and_do_not_consume_retry():
    graph = build_definition_graph(definition(), MemorySaver())
    before = await call(graph, "1234567890")
    result = await call(graph, "Mera mobile one two three four five six")
    assert result["awaiting"] == "code" and "code" not in result["slots"]
    assert result.get("node_retries") == before.get("node_retries")
    assert result.get("pending_digits") == before.get("pending_digits")
    assert "phone number" in result["reply"]
    assert (await call(graph, "654321"))["awaiting"] == "hub"


async def test_rejected_entry_digits_are_not_buffered_or_accepted():
    spec = definition()
    spec["edges"][0]["to"] = "code"
    graph = build_definition_graph(spec, MemorySaver())
    result = await call(graph, "Mera mobile one two three four five six")
    assert result["awaiting"] == "code" and "code" not in result["slots"]
    assert not result.get("pending_digits")


def test_full_masked_opt_in_and_caller_provenance():
    spec = definition("last4")
    audit = [{"action": "slot_filled", "variable": "contact"}]
    retained = {"contact": {"slot": "1234567890", "mode": "last4", "value": "7890"}}
    args = ("confirm my mobile number", {"contact": "1234567890"}, audit, "en-IN", retained)
    assert configured_readback(spec["nodes"], *args) == ("contact", "You gave 7 8 9 0.")
    assert configured_readback(spec["nodes"], args[0], args[1], [], "en-IN")[1] == "No number collected yet."
    spec["nodes"][1]["config"].pop("valueReadback")
    assert configured_readback(spec["nodes"], *args) is None


def test_ambiguous_fields_do_not_choose_one():
    nodes = definition()["nodes"]
    duplicate = deepcopy(nodes[1])
    duplicate["config"]["variable"] = "other"
    nodes.append(duplicate)
    assert configured_readback(nodes, "confirm my mobile number", {}, [], "en-IN") is None


@pytest.mark.parametrize("text", ["Don't repeat my mobile number", "Mera mobile repeat mat karo", "मेरा मोबाइल नंबर मत बताओ"])
def test_negated_request_never_reads_value(text):
    assert configured_readback(definition()["nodes"], text, {}, [], "en-IN") is None


async def test_partial_digits_complete_then_readback_and_masked_mode_retention():
    graph = build_definition_graph(definition("last4"), MemorySaver())
    result = await call(graph, "12345")
    assert result["awaiting"] == "contact"
    result = await call(graph, "67890")
    assert result["awaiting"] == "code"
    assert result["readback_values"]["contact"]["value"] == "7890"
    result = await call(graph, "confirm my mobile number")
    assert result["reply"] == "You gave 7 8 9 0."


async def test_source_overwrite_cannot_disclose_context_value():
    graph = build_definition_graph(definition(), MemorySaver())
    await call(graph, "1234567890")
    await graph.aupdate_state({"configurable": {"thread_id": "s"}},
                             {"slots": {"contact": "9999999999"}}, as_node="step")
    result = await call(graph, "confirm my mobile number")
    assert result["reply"] == "No number collected yet."


async def test_non_numeric_field_uses_same_opt_in_feature():
    spec = definition()
    config = spec["nodes"][1]["config"]
    config["entity"] = {"dataType": "text"}
    config["valueReadback"]["aliases"] = ["delivery address"]
    config["valueReadback"]["speakDigits"] = False
    graph = build_definition_graph(spec, MemorySaver())
    await call(graph, "hello")
    await call(graph, "Flat seven, Green Road")
    result = await call(graph, "Please repeat my delivery address")
    assert result["reply"] == "You gave Flat seven, Green Road."
    assert result["awaiting"] == "code"


async def test_engine_context_question_reset_and_export(monkeypatch):
    import shared.orchestration.workflow_engine as module

    engine = module.WorkflowEngine()
    monkeypatch.setattr(module, "load_workflow_definition", lambda *args: definition())

    async def memory():
        if engine._checkpointer is None:
            engine._checkpointer = MemorySaver()
        return engine._checkpointer

    monkeypatch.setattr(engine, "_get_checkpointer", memory)

    async def turn(text, **kwargs):
        return await engine.handle_turn_detailed(
            session_id="context", tenant_id="tenant", bot_id="bot", workflow_name="wf",
            user_text=text, language="en-IN", **kwargs,
        )

    await turn("1234567890")
    result = await turn("confirm my mobile number", pause_for_context=True)
    assert result["reply"] == "You gave 1 2 3 4 5 6 7 8 9 0."
    assert not result["offScript"]
    assert "readback_values" not in result
    assert result["slots"]["contact"] != "1234567890"
    await turn("hello", reset_state=True)
    result = await turn("confirm my mobile number")
    assert result["reply"] == "No number collected yet."


@pytest.mark.parametrize("text", LOCAL_CALL_REQUESTS + [
    "मेरा नंबर फिर से बताइए, ओटीपी नहीं आया।",
    "आपने क्या नंबर नोट किया है? मुझे बता सकती हो?",
    "What number did you note down? My OTP has not arrived.",
])
async def test_local_call_number_requests_through_router_and_engine(text):
    from au_bank.setup.number_confirmation import READBACK

    spec = definition()
    spec["nodes"][1]["config"]["valueReadback"] = deepcopy(READBACK)
    graph = build_definition_graph(spec, MemorySaver())
    before = await call(graph, "Ek do teen chaar paanch chhah saat aath nau zero")
    assert before["awaiting"] == "code"
    route = TurnRouter().decide(text, active_workflow="wf_example")
    assert route.kind == RouteKind.WORKFLOW
    result = await call(graph, text, language="hi-IN")
    assert result["reply"] == "आपने 1 2 3 4 5 6 7 8 9 0 बताया था।"
    for key in ("slots", "awaiting", "node_retries", "pending_digits"):
        assert result.get(key) == before.get(key)


@pytest.mark.parametrize("text", [
    "repeat my OTP number", "Mera OTP ka number batao", "Mera account number batao",
    "मेरा ओटीपी नंबर फिर से बताइए", "Please confirm my card number",
])
async def test_generic_number_alias_does_not_disclose_wrong_field(text):
    from au_bank.setup.number_confirmation import READBACK

    spec = definition()
    spec["nodes"][1]["config"]["valueReadback"] = deepcopy(READBACK)
    graph = build_definition_graph(spec, MemorySaver())
    await call(graph, "1234567890")
    result = await call(graph, text)
    assert "1 2 3 4 5 6 7 8 9 0" not in result["reply"]
    assert not any(e["action"] == "value_readback" for e in result["audit"])
