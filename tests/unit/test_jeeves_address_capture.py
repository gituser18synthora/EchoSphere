"""Clean address capture for the Jeeves flow (bot_44a50e8c4146).

Regression for cv_21df0c56facf (2026-10-29): the ``elseIsAnswer`` address
hub stored the whole utterance ("मेरा इस समय करंट एड्रेस है। थाने सेक्टर 16।
पिन कोड है। 404041.") into ``new_address`` and the final readback repeated
the filler as the address. The ``jeeves_address_v1`` semantic-slots
provider now copies only verbatim address spans out of the answer; the
engine asks again when the answer holds no place and falls back to the
address-mismatch branch after the retries.

Model outputs are controlled here (the reader's JSON is scripted per
utterance) to test the verbatim check, the engine paths and the summary
fields; the real model is exercised by the opt-in evals and the E2E run.
"""

import json
import runpy
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as wfe
from shared.orchestration.extensions import jeeves_address as ja
from shared.orchestration.goal_engine import compile_goal_policy
from shared.post_call.structured import derive_structured_fields
from shared.providers.base import LLMResult

V22 = json.load(open("tests/fixtures/jeeves_workflow_v22_bot_44a50e8c4146.json", encoding="utf-8"))
REAL = json.load(open("tests/fixtures/jeeves_call_cv_21df0c56facf.json", encoding="utf-8"))
SETUP_04 = runpy.run_path("jeeves/setup/04_address_capture.py", run_name="jeeves_setup_04")
SETUP_03 = runpy.run_path("jeeves/setup/03_summary_intents.py", run_name="jeeves_setup_03")
FIELDS = SETUP_03["target_summary_fields"]([])

CONTEXT = {"customer_name": "Rahul", "locality": "Sector 62, Noida", "connection_status": "DISCONNECTED"}
UTTERANCE = REAL["slots_recorded"]["new_address"]          # the raw address turn of the real call
CLEANED = REAL["slots_after_fix"]["new_address"]           # "थाने सेक्टर 16, 404041"
OPEN = ["हाँ जी", "हाँ बोलिए", "नहीं नहीं, मैं लोकेशन चेंज कर दिया हूँ।"]   # → awaiting n_ask_address


def _patched():
    nodes, edges, _ = SETUP_04["patch_definition"](V22["nodes"], V22["edges"])
    return {**V22, "version": 23, "nodes": nodes, "edges": edges}


V23 = _patched()


def _policy():
    return compile_goal_policy({"summaryFields": FIELDS}, bot_name="Customer Support", system_prompt="")


# ── the reader's contract (no engine) ────────────────────────────────────────


class TestCleanAddress:
    def test_real_call_filler_is_dropped_and_components_kept_verbatim(self):
        assert ja.clean_address(["थाने सेक्टर 16", "404041"], UTTERANCE) == (["थाने सेक्टर 16", "404041"], CLEANED)

    def test_only_verbatim_spans_survive_never_a_corrected_or_translated_one(self):
        spans, address = ja.clean_address(["Thane Sector 16", "थाने सेक्टर 16", "Thane West 400604"], UTTERANCE)
        assert spans == ["थाने सेक्टर 16"] and address == "थाने सेक्टर 16"

    def test_a_bare_pin_or_nothing_is_not_an_address(self):
        assert ja.clean_address(["404041"], UTTERANCE) == (["404041"], None)
        assert ja.clean_address([], UTTERANCE) == ([], None)
        assert ja.clean_address("थाने सेक्टर 16", UTTERANCE) == ([], None)

    def test_spans_are_ordered_as_spoken_deduplicated_and_unwrapped(self):
        text = "Flat 21, Lotus Apartments, Sector 62, Noida, UP 201301 — mera naya address."
        spans, address = ja.clean_address(['"Sector 62, Noida"', "Flat 21", "Lotus Apartments", "Sector 62", "UP 201301"], text)
        assert spans == ["Flat 21", "Lotus Apartments", "Sector 62, Noida", "UP 201301"]
        assert address == "Flat 21, Lotus Apartments, Sector 62, Noida, UP 201301"

    def test_matching_ignores_case_unicode_form_and_edge_punctuation(self):
        assert ja.verbatim_span("thane sector 16।", "Mera address Thane Sector 16 hai") == (13, "Thane Sector 16")
        assert ja.verbatim_span("Sector 62 Noida", "Sector 62, Noida") is None   # punctuation inside is content


class TestExtractAddress:
    @staticmethod
    def _llm(text):
        return AsyncMock(generate=AsyncMock(return_value=LLMResult(text=text, input_tokens=30, output_tokens=8)))

    async def test_capture_mode_patches_the_slot_from_validated_spans(self):
        llm = self._llm(json.dumps({"spans": ["थाने सेक्टर 16", "404041"], "same_as_registered": False}, ensure_ascii=False))
        out = await ja.extract_address(llm, text=UTTERANCE, mode="capture")
        assert out.address == CLEANED and out.patch == {"new_address": CLEANED} and out.failed is False
        assert out.input_tokens == 30 and out.requests == 1
        payload = json.loads(llm.generate.await_args.args[0][0]["content"])
        assert payload["latest_customer_utterance"] == UTTERANCE and "already_captured_address" not in payload

    async def test_fenced_or_prose_wrapped_json_is_still_read(self):
        out = await ja.extract_address(self._llm("```json\n{\"spans\": [\"404041\", \"थाने\"]}\n```"), text=UTTERANCE, mode="capture")
        assert out.spans == ["थाने", "404041"] and out.address == "थाने, 404041"

    async def test_same_as_registered_and_empty_answers_decide_no_address(self):
        out = await ja.extract_address(self._llm('{"spans": ["Sector 62"], "same_as_registered": true}'),
                                       text="wahi Sector 62 sahi hai", mode="capture")
        assert out.address is None and out.same_as_registered is True and out.patch == {}
        out = await ja.extract_address(self._llm('{"spans": []}'), text="ek minute, mera address hai na", mode="capture")
        assert out.address is None and out.failed is False

    async def test_failures_are_reported_not_turned_into_values(self):
        slow = AsyncMock(generate=AsyncMock(side_effect=TimeoutError()))
        out = await ja.extract_address(slow, text=UTTERANCE, mode="capture")
        assert out.failed and out.failure_reason == "timeout" and out.address is None
        bad = self._llm("sorry, no address")
        out = await ja.extract_address(bad, text=UTTERANCE, mode="capture")
        assert out.failed and out.failure_reason == "invalid_response"
        assert (await ja.extract_address(bad, text="   ", mode="capture")).requests == 0

    async def test_provider_calls_the_model_only_on_address_turns(self):
        llm = self._llm('{"spans": ["404041"]}')
        p = ja.PROVIDER
        idle = await p.extract(llm, text="कल कर दो", slots={}, pending_question="date?", pending_variable="visit_day",
                               joint_variables=[], history=[])
        assert idle["mode"] == "" and llm.generate.await_count == 0
        cont = await p.extract(llm, text="पिन कोड 404041", slots={"new_address": "थाने सेक्टर 16"},
                               pending_question="date?", pending_variable="visit_day", joint_variables=[], history=[])
        assert cont["mode"] == "continue" and cont["address"] == "404041"
        assert llm.generate.await_count == 1
        assert not p.owns_variable("visit_day") and not p.owns_variable("new_address")
        assert p.llm_extraction_enabled(V23) and not p.llm_extraction_enabled(V22)


class TestMergeExtraction:
    def test_capture_result_sets_clean_and_raw_and_marks_decided(self):
        slots, audit = {}, []
        result = {"mode": "capture", "address": CLEANED, "raw_text": UTTERANCE, "spans": ["थाने सेक्टर 16", "404041"]}
        assert ja.PROVIDER.merge_extraction(slots, result, audit, "n_ask_address") == {"new_address"}
        assert slots == {"new_address": CLEANED, "new_address_raw": UTTERANCE}
        assert audit[-1]["action"] == "also_captured" and audit[-1]["source"] == "jeeves_address_v1"

    def test_empty_capture_is_decided_without_touching_the_slot(self):
        slots, audit = {}, []
        assert ja.PROVIDER.merge_extraction(slots, {"mode": "capture", "address": None, "raw_text": "ek minute"}, audit, "n") == {"new_address"}
        assert slots == {} and audit[-1]["action"] == "capture_empty"

    def test_continuation_appends_only_new_components(self):
        slots, audit = {"new_address": "थाने सेक्टर 16", "new_address_raw": "थाने सेक्टर 16"}, []
        result = {"mode": "continue", "address": "404041", "spans": ["404041"], "raw_text": "पिन कोड 404041"}
        assert ja.PROVIDER.merge_extraction(slots, result, audit, "n_ask_visit_date") == {"new_address"}
        assert slots["new_address"] == "थाने सेक्टर 16, 404041"
        assert audit[-1]["continuation"] is True
        again = {"mode": "continue", "address": "थाने सेक्टर 16", "spans": ["थाने सेक्टर 16"], "raw_text": "थाने सेक्टर 16 bola na"}
        assert ja.PROVIDER.merge_extraction(slots, again, audit, "n") == set()   # already there
        assert slots["new_address"] == "थाने सेक्टर 16, 404041"

    def test_failed_reader_decides_nothing(self):
        slots = {}
        assert ja.PROVIDER.merge_extraction(slots, {"mode": "capture", "failed": True}, [], "n") == set()
        assert slots == {}


# ── the engine on the patched v22 flow ───────────────────────────────────────


@pytest.fixture
def engine(monkeypatch):
    eng = wfe.WorkflowEngine()
    saver = MemorySaver()

    async def _mem(self):
        return saver

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem)
    monkeypatch.setattr(wfe, "load_workflow_definition", lambda *args: V23)
    return eng


def _reader(script: dict, *, failure: bool = False):
    """A fake reader: ``script`` maps a customer utterance to the model's JSON."""

    async def generate(messages, **kwargs):
        if failure:
            raise RuntimeError("provider unavailable")
        payload = json.loads(messages[0]["content"])
        answer = script.get(payload["latest_customer_utterance"], {"spans": []})
        return LLMResult(text=json.dumps(answer, ensure_ascii=False), input_tokens=20, output_tokens=5)

    return AsyncMock(generate=AsyncMock(side_effect=generate))


async def _turn(engine, llm, text, session, language="hi-IN"):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_x", bot_id="bot_44a50e8c4146", workflow_name="wf_9685ed07e312",
        user_text=text, language=language, context_values=CONTEXT, llm=llm,
    )


async def _open(engine, llm, session, language="hi-IN"):
    last = None
    for text in OPEN:
        last = await _turn(engine, llm, text, session, language)
    assert last["trace"][-1] == "n_ask_address", last["trace"]
    return last


class TestRealCallRegression:
    async def test_readback_and_slots_carry_only_the_address_components(self, engine):
        llm = _reader({UTTERANCE: {"spans": REAL["address_turn_spans"]}})
        await _open(engine, llm, "real")
        r = await _turn(engine, llm, UTTERANCE, "real")
        assert r["trace"][-1] == "n_ask_visit_date" and r["offScript"] is False
        assert r["slots"]["new_address"] == CLEANED
        assert r["slots"]["new_address_raw"] == UTTERANCE
        assert r["slots"]["address_verified"] == "changed"
        assert llm.generate.await_count == 1          # one reader call, on the address turn only
        r = await _turn(engine, llm, "कल कर दो।", "real")
        assert r["trace"][-1] == "n_final_confirm"
        assert CLEANED in r["reply"] and "मेरा इस समय" not in r["reply"] and "पिन कोड है" not in r["reply"]
        r = await _turn(engine, llm, "हाँ ठीक रहेगा।", "real")
        assert r["done"] is True and r["slots"]["visit_status"] == "booked"
        fields = derive_structured_fields(_policy(), r["slots"])
        assert fields["address_changed"] == "Yes" and fields["updated_address"] == CLEANED
        assert fields["appointment_booked"] == "Yes" and fields["call_result"] == "booked"
        assert fields["already_picked_up"] is None   # a booked visit is never a completed pickup
        for key, value in REAL["expected"].items():
            if key != "appointment_date":
                assert fields[key] == value, key

    async def test_the_v22_flow_without_the_patch_still_stores_the_raw_utterance(self, engine, monkeypatch):
        """Documents the pre-fix behaviour the patch script removes."""
        monkeypatch.setattr(wfe, "load_workflow_definition", lambda *args: V22)
        llm = _reader({UTTERANCE: {"spans": REAL["address_turn_spans"]}})
        await _open(engine, llm, "raw")
        r = await _turn(engine, llm, UTTERANCE, "raw")
        assert r["slots"]["new_address"] == UTTERANCE and llm.generate.await_count == 0


class TestClarificationAndFallback:
    async def test_an_answer_without_a_place_is_asked_again_then_captured(self, engine):
        llm = _reader({UTTERANCE: {"spans": ["थाने सेक्टर 16", "404041"]}})
        await _open(engine, llm, "clar")
        r = await _turn(engine, llm, "मेरा एड्रेस है ना... वो क्या है", "clar")
        assert r["trace"] == ["n_ask_address"] and r["offScript"] is False
        assert r["reply"] == SETUP_04["CLARIFICATION"]["hi"]
        assert "new_address" not in r["slots"]
        r = await _turn(engine, llm, UTTERANCE, "clar")
        assert r["trace"][-1] == "n_ask_visit_date" and r["slots"]["new_address"] == CLEANED

    async def test_a_bare_pin_is_not_accepted_as_the_address(self, engine):
        llm = _reader({"404041": {"spans": ["404041"]}})
        await _open(engine, llm, "pin")
        r = await _turn(engine, llm, "404041", "pin")
        assert r["trace"] == ["n_ask_address"] and "new_address" not in r["slots"]
        assert r["reply"] == SETUP_04["CLARIFICATION"]["hi"]

    async def test_an_invented_span_is_never_stored(self, engine):
        llm = _reader({UTTERANCE: {"spans": ["Thane West, 400604"]}})   # not what the customer said
        await _open(engine, llm, "invent")
        r = await _turn(engine, llm, UTTERANCE, "invent")
        assert "new_address" not in r["slots"] and r["reply"] == SETUP_04["CLARIFICATION"]["hi"]

    async def test_after_the_retries_the_flow_takes_the_address_mismatch_branch(self, engine):
        llm = _reader({})
        await _open(engine, llm, "fb")
        for line in ("wo kya naam hai uska, bhool gaya", "mera address wo hai jo"):
            r = await _turn(engine, llm, line, "fb")
            assert r["trace"] == ["n_ask_address"] and r["reply"] == SETUP_04["CLARIFICATION"]["hi"]
        r = await _turn(engine, llm, "अरे यार, बताया तो", "fb")
        assert r["trace"][:2] == ["n_ask_address", "n_mark_address_mismatch"] and r["done"] is True
        assert r["slots"]["visit_status"] == "address_mismatch" and r["slots"]["address_verified"] == "no"
        assert "new_address" not in r["slots"]
        fields = derive_structured_fields(_policy(), r["slots"])
        assert fields["address_changed"] == "Yes" and fields["updated_address"] is None
        assert fields["call_result"] == "address_mismatch" and fields["appointment_booked"] == "No"

    async def test_english_caller_hears_the_english_clarification_and_readback(self, engine):
        text = "My address is Flat 21 Lotus Apartments Sector 62 Noida, pin 201301"
        llm = _reader({text: {"spans": ["Flat 21 Lotus Apartments Sector 62 Noida", "201301"]}})
        session = "en"
        for line in ("yes speaking", "yes go ahead", "no, I have moved"):
            r = await _turn(engine, llm, line, session, "en-IN")
        assert r["trace"][-1] == "n_ask_address"
        r = await _turn(engine, llm, "my address is, let me think", session, "en-IN")
        assert r["reply"] == SETUP_04["CLARIFICATION"]["en"]
        r = await _turn(engine, llm, text, session, "en-IN")
        assert r["slots"]["new_address"] == "Flat 21 Lotus Apartments Sector 62 Noida, 201301"
        r = await _turn(engine, llm, "tomorrow", session, "en-IN")
        assert "Flat 21 Lotus Apartments Sector 62 Noida, 201301" in r["reply"] and "My address is" not in r["reply"]


class TestContinuationAndRetraction:
    async def test_a_pin_said_in_the_next_breath_is_appended(self, engine):
        first = "थाने सेक्टर 16"
        llm = _reader({first: {"spans": ["थाने सेक्टर 16"]}, "पिन कोड 404041": {"spans": ["404041"]}})
        await _open(engine, llm, "cont")
        r = await _turn(engine, llm, first, "cont")
        assert r["slots"]["new_address"] == "थाने सेक्टर 16" and r["trace"][-1] == "n_ask_visit_date"
        r = await _turn(engine, llm, "पिन कोड 404041", "cont")
        assert r["slots"]["new_address"] == "थाने सेक्टर 16, 404041"
        assert r["trace"][-1] == "n_ask_visit_date" and "visit_day" not in r["slots"]   # the date is asked again
        r = await _turn(engine, llm, "कल कर दो", "cont")
        assert "थाने सेक्टर 16, 404041" in r["reply"]

    async def test_a_day_answer_never_touches_the_address(self, engine):
        llm = _reader({UTTERANCE: {"spans": ["थाने सेक्टर 16", "404041"]}})
        await _open(engine, llm, "day")
        await _turn(engine, llm, UTTERANCE, "day")
        r = await _turn(engine, llm, "कल कर दो", "day")
        assert r["slots"]["new_address"] == CLEANED and r["slots"]["visit_day"] == "कल"

    async def test_taking_the_change_back_confirms_the_registered_address(self, engine):
        llm = _reader({})
        await _open(engine, llm, "retract")
        r = await _turn(engine, llm, "नहीं नहीं, वही सही है जो आपने बोला", "retract")
        assert r["trace"][:2] == ["n_ask_address", "n_mark_address_ok"]
        assert r["slots"]["address_verified"] == "yes" and "new_address" not in r["slots"]
        assert llm.generate.await_count == 1   # the reader ran; the authored edge decided
        fields = derive_structured_fields(_policy(), r["slots"])
        assert fields["address_changed"] == "No" and fields["updated_address"] is None


class TestReaderFailures:
    """A reader failure must never reproduce the original bug: on a
    ``semanticCapture`` hub the utterance is never the value. Timeout,
    unreadable output, a provider error and no reader at all take the same
    clarification → retries → fallback path; the words survive only in
    ``new_address_raw`` for audit."""

    @staticmethod
    def _broken(effect):
        return AsyncMock(generate=AsyncMock(side_effect=effect))

    @staticmethod
    def _prose():
        return AsyncMock(generate=AsyncMock(return_value=LLMResult(text="Sorry, I cannot read addresses.")))

    @pytest.mark.parametrize("name,llm_factory", [
        ("timeout", lambda: TestReaderFailures._broken(TimeoutError())),
        ("provider_error", lambda: TestReaderFailures._broken(RuntimeError("provider unavailable"))),
        ("invalid_output", lambda: TestReaderFailures._prose()),
        ("no_reader", lambda: None),
    ])
    async def test_a_failed_reader_asks_again_and_a_working_one_then_captures(self, engine, name, llm_factory):
        broken = llm_factory()
        await _open(engine, broken, name)
        r = await _turn(engine, broken, UTTERANCE, name)
        assert r["trace"] == ["n_ask_address"] and r["offScript"] is False
        assert r["reply"] == SETUP_04["CLARIFICATION"]["hi"]
        assert "new_address" not in r["slots"]
        assert r["slots"]["new_address_raw"] == UTTERANCE          # audit only
        working = _reader({UTTERANCE: {"spans": REAL["address_turn_spans"]}})
        r = await _turn(engine, working, UTTERANCE, name)
        assert r["trace"][-1] == "n_ask_visit_date" and r["slots"]["new_address"] == CLEANED
        assert r["slots"]["new_address_raw"] == UTTERANCE

    async def test_a_reader_that_keeps_failing_leaves_the_address_unresolved(self, engine):
        broken = self._broken(RuntimeError("down"))
        await _open(engine, broken, "down")
        for line in (UTTERANCE, "B-12, Sector 15, Noida, pincode 201301"):
            r = await _turn(engine, broken, line, "down")
            assert r["trace"] == ["n_ask_address"] and r["reply"] == SETUP_04["CLARIFICATION"]["hi"]
        r = await _turn(engine, broken, "Flat 4, Lotus Society, Thane 400604", "down")
        assert r["trace"][:2] == ["n_ask_address", "n_mark_address_mismatch"] and r["done"] is True
        assert "new_address" not in r["slots"]
        assert r["slots"]["new_address_raw"] == "Flat 4, Lotus Society, Thane 400604"
        assert r["slots"]["visit_status"] == "address_mismatch" and r["slots"]["address_verified"] == "no"
        assert "Thane" not in r["reply"] and "Sector 16" not in r["reply"]     # nothing confirmed
        fields = derive_structured_fields(_policy(), r["slots"])
        assert fields["address_changed"] == "Yes" and fields["updated_address"] is None
        assert fields["call_result"] == "address_mismatch"
        assert broken.generate.await_count == 3

    async def test_a_failure_after_a_verified_capture_keeps_the_verified_value(self, engine):
        """n_ask_address_2 re-asks after a bare yes/no; a reader failure there
        must not erase or replace the address already verified."""
        good = _reader({UTTERANCE: {"spans": REAL["address_turn_spans"]}})
        await _open(engine, good, "keep")
        r = await _turn(engine, good, UTTERANCE, "keep")
        assert r["slots"]["new_address"] == CLEANED
        # the reader runs only on address turns: a failing one changes nothing later
        r = await _turn(engine, self._broken(RuntimeError("down")), "कल कर दो", "keep")
        assert r["slots"]["new_address"] == CLEANED and r["trace"][-1] == "n_final_confirm"
        assert CLEANED in r["reply"]

    async def test_without_a_fallback_target_the_hub_goes_off_script_and_keeps_waiting(self, engine, monkeypatch):
        nodes = [dict(n, config={k: v for k, v in (n.get("config") or {}).items() if k != "captureFallbackTo"})
                 if n["id"] == "n_ask_address" else n for n in V23["nodes"]]
        monkeypatch.setattr(wfe, "load_workflow_definition", lambda *args: {**V23, "nodes": nodes})
        broken = self._broken(RuntimeError("down"))
        await _open(engine, broken, "nofb")
        for _ in range(2):
            r = await _turn(engine, broken, UTTERANCE, "nofb")
            assert r["reply"] == SETUP_04["CLARIFICATION"]["hi"]
        r = await _turn(engine, broken, UTTERANCE, "nofb")
        assert r["offScript"] is True and r["trace"] == ["n_ask_address"]
        assert "new_address" not in r["slots"] and r["done"] is False

    def test_the_raw_slot_is_never_the_updated_address(self):
        fields = derive_structured_fields(_policy(), {"address_verified": "changed", "new_address_raw": UTTERANCE,
                                                      "visit_status": "address_mismatch"})
        assert fields["address_changed"] == "Yes" and fields["updated_address"] is None
        assert "new_address_raw" not in fields


class TestPatchScript:
    def test_patch_is_idempotent_and_refuses_an_unknown_flow(self):
        nodes, edges, changes = SETUP_04["patch_definition"](V22["nodes"], V22["edges"])
        assert len(changes) == 10
        again, again_edges, none = SETUP_04["patch_definition"](nodes, edges)
        assert none == [] and again == nodes and again_edges == edges
        assert len(edges) == len(V22["edges"]) + 1
        start = next(n for n in nodes if n["kind"] == "start")
        assert start["config"]["semanticSlots"] == "jeeves_address_v1"
        for hub in ("n_ask_address", "n_ask_address_2"):
            cfg = next(n for n in nodes if n["id"] == hub)["config"]
            assert cfg["semanticCapture"] is True and cfg["captureMaxRetries"] == 2
            assert cfg["captureVariable"] == "new_address" and cfg["elseIsAnswer"] is True
            assert cfg["captureFallbackTo"] == "n_mark_address_mismatch"
            fallback = next(e for e in edges if e["from"] == hub and e["to"] == "n_mark_address_mismatch")
            assert "pata nahi" in fallback["label"] and "fallback" not in fallback["label"]   # never an else token
        with pytest.raises(ValueError):
            SETUP_04["patch_definition"]([n for n in V22["nodes"] if n["id"] != "n_ask_address_2"], V22["edges"])
        baseline = json.load(open("jeeves/setup/workflow_v12_bot_44a50e8c4146.json", encoding="utf-8"))
        with pytest.raises(ValueError):
            SETUP_04["patch_definition"](baseline["nodes"], baseline["edges"])

    def test_untouched_nodes_and_edges_are_byte_identical(self):
        nodes, edges, _ = SETUP_04["patch_definition"](V22["nodes"], V22["edges"])
        touched = {"n_start", "n_ask_address", "n_ask_address_2"}
        assert [n for n in nodes if n["id"] not in touched] == [n for n in V22["nodes"] if n["id"] not in touched]
        changed_edges = {e["id"] for e in edges} - {e["id"] for e in V22["edges"]}
        assert changed_edges == {SETUP_04["RETRACTION_EDGE_ID"]}
        assert [e for e in edges if e["id"] != SETUP_04["RETRACTION_EDGE_ID"]] == V22["edges"]
