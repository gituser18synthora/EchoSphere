"""Engine replay of the Zepto OUTBOUND OB-fee deduction verification flow
(zepto/setup/09_ob_deduction_outbound.py) — no DB, no LLM.

Covers the user-facing contract: conditional branching, multi-answer
extraction (Hindi / Hinglish / English), no repeated questions, corrections,
incomplete answers staying absent (never invented), and the final API payload
(workflow slots + runtime metadata + dialer ticket id).
"""

import importlib.util
import pathlib

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.tool_executor as te
import shared.orchestration.workflow_engine as wfe

_SETUP = pathlib.Path("zepto/setup/09_ob_deduction_outbound.py")


def _module():
    spec = importlib.util.spec_from_file_location("stage09", _SETUP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = _module()
CONTEXT = {"ticket_id": "ZPT-OBF-70412", "partner_id": "ZP-88231",
           "partner_name": "Ravi Kumar"}


def _definition():
    nodes, edges = M.build_workflow()
    return {"id": "wf_ob", "version": 1, "name": M.WORKFLOW_NAME,
            "nodes": nodes, "edges": edges}


@pytest.fixture()
def engine(monkeypatch):
    eng = wfe.WorkflowEngine()

    async def _mem(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem)
    monkeypatch.setattr(wfe, "load_workflow_definition",
                        lambda tenant_id, bot_id, name: _definition())
    return eng


@pytest.fixture()
def payloads(monkeypatch):
    """Fake ticketing executor capturing every api-node payload."""
    seen: list[dict] = []

    class _Result:
        ok = True
        status = "ok"
        mocked = True
        mapped = {"ticket_reference": "ZPT-OBF-70412", "ticket_update_status": "updated"}

    class _Executor:
        async def execute(self, **kwargs):
            seen.append(kwargs)
            return _Result()

    monkeypatch.setattr(te, "get_tool_executor", lambda: _Executor())
    return seen


async def _turn(engine, text, session, language="hi-IN", entry=False, signal=None):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_04250683f1b3", bot_id="bot_ob",
        workflow_name=M.WORKFLOW_NAME, user_text=text, language=language,
        context_values=CONTEXT, signal=signal,
    )


async def _run(engine, turns, session, language="hi-IN"):
    result = None
    for text in turns:
        result = await _turn(engine, text, session, language)
    return result


def _asked(result, question):
    return question in (result.get("reply") or "")


# ── document flow: all three verified ───────────────────────────────────────

class TestAllYesHindi:
    async def test_greeting_yes_enters_flow_and_asks_only_q1(self, engine):
        r = await _turn(engine, "हाँ जी बोलिए", "h1")
        assert r["trace"][-1] == "n_ask_explained"
        assert _asked(r, M.Q_EXPLAINED)
        # the opener is never swallowed as Q1's answer
        assert "deduction_explained" not in r["slots"]

    async def test_one_question_at_a_time_then_consistent(self, engine, payloads):
        s = "h2"
        await _turn(engine, "हाँ जी", s)
        r = await _turn(engine, "हाँ, बताया गया था", s)
        assert r["slots"]["deduction_explained"] == "yes"
        assert _asked(r, M.Q_AMOUNT_INFORMED)
        r = await _turn(engine, "हाँ, पाँच सौ रुपये बताया था", s)
        assert r["slots"]["amount_informed"] == "yes"
        assert r["slots"]["informed_amount"] == "500"       # Hindi number words
        assert not _asked(r, M.Q_INFORMED_AMOUNT)           # already known → skipped
        assert _asked(r, M.Q_AMOUNT_MATCHES)
        r = await _turn(engine, "जी, उतना ही कटा है", s)
        assert r["slots"]["amount_matches"] == "yes"
        assert r["trace"][-1] == "n_hub_verify"
        r = await _turn(engine, "हाँ सही है", s)
        assert r["slots"]["verification_status"] == "consistent"
        assert "n_msg_consistent" in r["trace"]
        assert r["trace"][-1] == "n_hub_more"
        r = await _turn(engine, "नहीं, बस इतना ही", s)
        assert r["done"] is True
        assert "n_api" in r["trace"] and "n_msg_close" in r["trace"]
        payload = payloads[-1]["args"]
        assert payload["deduction_explained"] == "yes"
        assert payload["amount_informed"] == "yes"
        assert payload["informed_amount"] == "500"
        assert payload["amount_matches"] == "yes"
        assert payload["verification_status"] == "consistent"
        assert payload["ticket_type"] == "onboarding_fee_deduction"
        # runtime metadata + dialer ticket id ride along
        assert payload["ticket_id"] == "ZPT-OBF-70412"
        assert payload["partner_id"] == "ZP-88231"
        assert payload["bot_id"] == "bot_ob"
        assert payload["tenant_id"] == "tn_04250683f1b3"
        assert payload["session_id"] == s
        assert payload["conversation_language"] == "hi-IN"
        # never invented
        for absent in ("deducted_amount", "payment_mode", "upfront_amount_paid",
                       "deduction_date_or_week", "additional_concern",
                       "explanation_given_on_call", "partner_name"):
            assert absent not in payload


class TestMultiAnswer:
    @pytest.mark.parametrize("utterance", [
        "haan, deduction ke baare mein bataya tha aur bola tha 500 rupees katega, aur utna hi kata hai",
        "हाँ बताया था, पाँच सौ रुपये कटेगा बोला था और उतना ही कटा",
        "Yes, they explained the deduction and told me 500 rupees would be deducted, and the same amount was deducted",
    ])
    async def test_one_utterance_answers_everything_no_repeat(self, engine, utterance):
        s = "m-" + str(abs(hash(utterance)) % 10_000)
        await _turn(engine, "haan ji", s)
        r = await _turn(engine, utterance, s)
        slots = r["slots"]
        assert slots["deduction_explained"] == "yes"
        assert slots["amount_informed"] == "yes"
        assert slots["informed_amount"] == "500"
        assert slots["amount_matches"] == "yes"
        # straight to the readback: none of the later questions is asked
        assert r["trace"][-1] == "n_hub_verify"
        for q in (M.Q_AMOUNT_INFORMED, M.Q_INFORMED_AMOUNT, M.Q_AMOUNT_MATCHES):
            assert not _asked(r, q)

    async def test_narrative_opener_after_greeting_fills_answers(self, engine):
        # story told in reply to the greeting routes into the flow and is
        # consumed by Q1 (entry_slot_filled) — no re-asking of what it said
        r = await _turn(engine, "haan, mujhe bola tha 500 katega par 700 kat gaya", "m-open")
        assert r["slots"]["deduction_explained"] == "yes"
        assert r["slots"]["amount_informed"] == "yes"
        assert r["slots"]["informed_amount"] == "500"
        assert r["slots"]["deducted_amount"] == "700"
        assert r["slots"]["amount_matches"] == "no"
        # only the still-missing discrepancy detail (week) is asked
        assert _asked(r, M.Q_DEDUCTION_WEEK)
        assert not _asked(r, M.Q_AMOUNT_MATCHES)


# ── conditional branches ────────────────────────────────────────────────────

class TestFirstAnswerNo:
    async def test_not_explained_explains_fee_once_and_continues(self, engine, payloads):
        s = "no1"
        await _turn(engine, "haan", s)
        r = await _turn(engine, "nahi, mujhe fee ke baare mein nahi bataya tha", s)
        assert r["slots"]["deduction_explained"] == "no"
        assert "amount_informed" not in r["slots"]          # unknown, never converted to "no"
        assert "n_msg_explain" in r["trace"]
        assert r["slots"]["explanation_given_on_call"] == "yes"
        assert "onboarding fee" in r["reply"]              # document facts spoken
        assert "n_msg_explain" == r["trace"][-2] or "n_msg_explain" in r["trace"]
        # the deducted amount is the first genuinely missing fact
        assert _asked(r, M.Q_DEDUCTED_AMOUNT)
        assert not _asked(r, M.Q_AMOUNT_INFORMED)
        r = await _turn(engine, "aath sau", s)
        assert r["slots"]["deducted_amount"] == "800"
        assert _asked(r, M.Q_AMOUNT_INFORMED)              # then whether it was communicated
        r = await _turn(engine, "haan, 500 bataya tha", s)
        assert r["slots"]["amount_informed"] == "yes"
        assert r["slots"]["informed_amount"] == "500"
        # both figures known → the match is DERIVED, Q3 never asked
        assert r["slots"]["amount_matches"] == "no"
        assert not _asked(r, M.Q_AMOUNT_MATCHES)
        assert "difference" in r["reply"]
        assert _asked(r, M.Q_DEDUCTION_WEEK)
        r = await _turn(engine, "pichle hafte", s)
        r = await _turn(engine, "nahi, pehla wala galat hai — bataya gaya tha", s)
        # rejection at the readback that names Q1 as wrong → cleared → Q1 re-asked,
        # the explanation is NOT repeated on the re-walk
        assert "n_msg_explain" not in r["trace"][-6:] or r["slots"]["deduction_explained"] == "yes"
        r = await _turn(engine, "haan sahi hai", s)
        r = await _turn(engine, "nahi", s)
        assert r["done"] is True
        assert payloads[-1]["args"]["explanation_given_on_call"] == "yes"


class TestAmountNotCommunicated:
    async def test_not_communicated_never_claims_correct(self, engine, payloads):
        s = "nc1"
        await _turn(engine, "ji", s)
        r = await _turn(engine, "haan bataya tha par amount nahi bataya tha", s)
        assert r["slots"]["deduction_explained"] == "yes"
        assert r["slots"]["amount_informed"] == "no"
        # no "does it match" question — nothing to match against; the actual
        # deducted amount is captured instead
        assert not _asked(r, M.Q_AMOUNT_MATCHES)
        assert _asked(r, M.Q_DEDUCTED_AMOUNT)
        r = await _turn(engine, "aath sau rupaye", s)
        assert r["slots"]["deducted_amount"] == "800"
        assert r["trace"][-1] == "n_hub_verify"
        r = await _turn(engine, "haan sahi hai", s)
        assert "n_msg_not_communicated" in r["trace"]
        assert r["slots"]["verification_status"] == "amount_not_communicated"
        assert "consistent" not in r["reply"]
        r = await _turn(engine, "nahi bas", s)
        payload = payloads[-1]["args"]
        assert payload["verification_status"] == "amount_not_communicated"
        assert "amount_matches" not in payload
        assert "informed_amount" not in payload


class TestMismatch:
    async def test_discrepancy_captured_no_resolution_invented(self, engine, payloads):
        s = "mm1"
        await _turn(engine, "haan", s)
        r = await _turn(engine, "haan bataya tha", s)
        r = await _turn(engine, "haan, five hundred", s)
        assert r["slots"]["informed_amount"] == "500"
        r = await _turn(engine, "nahi, zyada kata hai", s)
        assert r["slots"]["amount_matches"] == "no"
        assert _asked(r, M.Q_DEDUCTED_AMOUNT)
        r = await _turn(engine, "700 kata", s)
        assert r["slots"]["deducted_amount"] == "700"
        assert _asked(r, M.Q_DEDUCTION_WEEK)
        r = await _turn(engine, "pichle hafte", s)
        assert r["slots"]["deduction_date_or_week"] == "pichle hafte"
        assert r["trace"][-1] == "n_hub_verify"
        r = await _turn(engine, "haan sab sahi hai", s)
        assert "n_msg_mismatch" in r["trace"]
        assert r["slots"]["verification_status"] == "amount_mismatch"
        for forbidden in ("refund", "reverse", "wapas", "वापस"):
            assert forbidden not in r["reply"].lower()
        r = await _turn(engine, "nahi", s)
        payload = payloads[-1]["args"]
        assert payload["informed_amount"] == "500"
        assert payload["deducted_amount"] == "700"
        assert payload["verification_status"] == "amount_mismatch"
        assert payload["deduction_date_or_week"] == "pichle hafte"


# ── readback confirmation → outcome, never a second readback ────────────────

FIGURES = ("200", "500", "300", "दो सौ", "पांच सौ", "पाँच सौ", "तीन सौ", "two hundred", "five hundred")


async def _to_readback(engine, session, language="hi-IN"):
    """200 told / 500 deducted / Monday → the exact readback at n_hub_verify."""
    await _turn(engine, "haan" if language == "hi-IN" else "yes", session, language)
    await _turn(engine, "haan bataya tha, 200 katega bola tha, lekin 500 kata"
                if language == "hi-IN" else "Yes, I was told 200 but 500 was deducted",
                session, language, signal="affirm")
    r = await _turn(engine, "Monday ko", session, language)
    assert r["trace"][-1] == "n_hub_verify"
    assert (r["slots"]["informed_amount"], r["slots"]["deducted_amount"]) == ("200", "500")
    assert "200" in r["reply"] and "500" in r["reply"]      # the readback itself
    return r


class TestReadbackConfirmation:
    @pytest.mark.parametrize("confirmation,language", [
        ("Haan sahi hai", "hi-IN"), ("haan", "hi-IN"), ("haan sahi hai", "hi-IN"),
        ("ji sahi hai", "hi-IN"), ("ji haan, sab sahi hai", "hi-IN"),
        ("yes", "en-IN"), ("yes, that's correct", "en-IN"), ("Yes, all correct.", "en-IN"),
    ])
    async def test_affirmative_moves_to_outcome_without_reading_figures_again(
            self, engine, confirmation, language):
        s = f"rb-confirm-{language}-{abs(hash(confirmation))}"
        await _to_readback(engine, s, language)
        r = await _turn(engine, confirmation, s, language, signal="affirm")
        # forward transition: verify hub → outcome conditions → mismatch outcome → anything-else hub
        assert r["trace"][:2] == ["n_hub_verify", "n_cond_out_informed_2"]
        assert "n_msg_mismatch" in r["trace"] and r["trace"][-1] == "n_hub_more"
        assert r["slots"]["verification_status"] == "amount_mismatch"
        # nothing collected again, no second readback
        assert not any(node.startswith("n_ask_") for node in r["trace"])
        assert r["trace"].count("n_hub_verify") == 1
        assert "readback" not in r and r.get("awaitingKind") != "verify"
        # the outcome + next question carry no figures (authored fallback wording)
        for figure in FIGURES:
            assert figure not in r["reply"], (figure, r["reply"])
        assert r["reply"].count("?") == 1                      # exactly one question: anything else?

    async def test_outcome_directive_forbids_repeating_confirmed_values(self):
        for directive in (M.MISMATCH_DIRECTIVE, M.NOT_COMMUNICATED_DIRECTIVE):
            assert "JUST confirmed" in directive
            assert "Do NOT repeat any figure" in directive
        for text in (M.MISMATCH_TEXT, M.NOT_COMMUNICATED_TEXT, M.CONSISTENT_TEXT,
                     M.ENGLISH_TEXT["n_msg_mismatch"], M.ENGLISH_TEXT["n_msg_not_communicated"]):
            assert not any(ch.isdigit() for ch in text)

    @pytest.mark.parametrize("rejection", ["nahi", "nahi galat hai", "sahi nahi hai", "no, that's wrong"])
    async def test_rejection_goes_to_the_correction_ask(self, engine, rejection):
        s = f"rb-reject-{abs(hash(rejection))}"
        await _to_readback(engine, s)
        r = await _turn(engine, rejection, s, signal="refusal")
        assert r["trace"][-1] == "n_ask_correction"
        assert _asked(r, "कौन सी बात सही नहीं है")
        assert "verification_status" not in r["slots"]
        assert "n_msg_mismatch" not in r["trace"]

    async def test_inline_correction_at_readback_reverifies_only(self, engine):
        s = "rb-inline-fix"
        await _to_readback(engine, s)
        r = await _turn(engine, "nahi, 500 nahi 600 kata tha", s, signal="refusal")
        assert r["slots"]["deducted_amount"] == "600"
        assert "verification_status" not in r["slots"]
        assert r["trace"][-1] == "n_hub_verify"                 # acknowledged + re-verified
        assert "600" in r["reply"]
        r = await _turn(engine, "haan ab sahi hai", s, signal="affirm")
        assert "n_msg_mismatch" in r["trace"] and r["trace"][-1] == "n_hub_more"
        assert r["slots"]["verification_status"] == "amount_mismatch"


# ── corrections, incomplete answers, additional concern ─────────────────────

class TestCorrections:
    async def test_inline_correction_at_readback_updates_only_that_field(self, engine, payloads):
        s = "c1"
        await _turn(engine, "haan", s)
        await _turn(engine, "haan bataya tha, 500 katega bola tha, utna hi kata", s)
        r = await _turn(engine, "nahi, 500 nahi 600 bataya tha", s)
        # the rejection carried the fix → correction ask skipped, re-verified
        assert r["slots"]["informed_amount"] == "600"
        assert "amount_matches" not in r["slots"]
        assert _asked(r, M.Q_AMOUNT_MATCHES)
        assert not _asked(r, "कौन सी बात सही नहीं है")
        r = await _turn(engine, "haan utna hi kata", s)
        assert r["trace"][-1] == "n_hub_verify"
        r = await _turn(engine, "haan ab sahi hai", s)
        r = await _turn(engine, "nahi", s)
        assert payloads[-1]["args"]["informed_amount"] == "600"

    async def test_field_named_wrong_without_value_is_reasked_only(self, engine):
        s = "c2"
        await _turn(engine, "haan", s)
        await _turn(engine, "haan bataya tha, 500 katega bola tha, utna hi kata", s)
        r = await _turn(engine, "nahi, match wala galat hai", s)
        assert "amount_matches" not in r["slots"]
        # re-walk: Q1/Q2/informed amount kept (not re-asked), Q3 re-asked
        assert r["slots"]["deduction_explained"] == "yes"
        assert r["slots"]["informed_amount"] == "500"
        assert _asked(r, M.Q_AMOUNT_MATCHES)
        assert not _asked(r, M.Q_EXPLAINED)
        r = await _turn(engine, "nahi, 700 kata", s)
        assert r["slots"]["amount_matches"] == "no"
        assert r["slots"]["deducted_amount"] == "700"

    async def test_late_correction_after_outcome_reverifies(self, engine):
        s = "c3"
        await _turn(engine, "haan", s)
        await _turn(engine, "haan bataya tha, 500 katega bola tha, utna hi kata", s)
        await _turn(engine, "haan sahi hai", s)
        r = await _turn(engine, "ruko, actually utna nahi kata tha", s)
        assert r["slots"]["amount_matches"] == "no"
        assert "n_ask_correction" in r["trace"]           # declared correction edge
        assert "additional_concern" not in r["slots"]      # not mistaken for a new concern
        # the figure is collected by the flow's own ask, never guessed here
        assert _asked(r, M.Q_DEDUCTED_AMOUNT)
        r = await _turn(engine, "700 kata tha", s)
        assert r["slots"]["deducted_amount"] == "700"

    async def test_new_deduction_mentioned_at_the_end_is_a_new_concern(self, engine):
        s = "c4"
        await _turn(engine, "haan", s)
        await _turn(engine, "haan bataya tha, 500 katega bola tha, utna hi kata", s)
        await _turn(engine, "haan sahi hai", s)
        r = await _turn(engine, "haan, is hafte bhi 300 ka ek aur deduction dikh raha hai", s)
        # the other deduction's figures never overwrite the verified ticket
        assert r["slots"]["informed_amount"] == "500"
        assert "deducted_amount" not in r["slots"]
        assert "deduction_date_or_week" not in r["slots"]
        assert r["slots"]["additional_concern"].startswith("haan, is hafte")
        assert r["done"] is True


class TestIncompleteAndAdditional:
    async def test_unknown_amount_stays_absent(self, engine, payloads):
        s = "i1"
        await _turn(engine, "haan", s)
        await _turn(engine, "haan", s)
        await _turn(engine, "haan", s)
        r = await _turn(engine, "yaad nahi", s)
        assert r["slots"]["informed_amount"] == "not remembered"
        assert _asked(r, M.Q_AMOUNT_MATCHES)
        r = await _turn(engine, "haan", s)
        r = await _turn(engine, "haan sahi hai", s)
        r = await _turn(engine, "nahi", s)
        payload = payloads[-1]["args"]
        assert "informed_amount" not in payload
        assert "deducted_amount" not in payload

    async def test_additional_concern_captured_in_own_words(self, engine, payloads):
        s = "a1"
        await _turn(engine, "haan", s)
        await _turn(engine, "haan bataya tha, 500 katega bola tha, utna hi kata", s)
        await _turn(engine, "haan sahi hai", s)
        r = await _turn(engine, "haan, ek aur baat — is hafte bhi ek deduction dikh raha hai", s)
        assert r["slots"]["additional_concern"].startswith("haan, ek aur baat")
        assert r["done"] is True
        assert "n_msg_additional_noted" in r["trace"]
        assert payloads[-1]["args"]["additional_concern"] == r["slots"]["additional_concern"]


class TestEnglish:
    async def test_english_caller_end_to_end(self, engine, payloads):
        s = "e1"
        r = await _turn(engine, "yes speaking", s, language="en-IN")
        assert "deduction_explained" not in r["slots"]
        assert r["trace"][-1] == "n_ask_explained"
        assert r["reply"] == M.ENGLISH_TEXT["n_ask_explained"]
        r = await _turn(engine, "Yes, it was explained to me", s, language="en-IN")
        assert r["slots"]["deduction_explained"] == "yes"
        r = await _turn(engine, "They said five hundred rupees would be deducted", s, language="en-IN")
        assert r["slots"]["amount_informed"] == "yes"
        assert r["slots"]["informed_amount"] == "500"
        r = await _turn(engine, "No, they deducted seven hundred", s, language="en-IN")
        assert r["slots"]["amount_matches"] == "no"
        assert r["slots"]["deducted_amount"] == "700"
        r = await _turn(engine, "I don't remember the week", s, language="en-IN")
        assert r["slots"]["deduction_date_or_week"] == "not remembered"
        r = await _turn(engine, "yes, all correct", s, language="en-IN")
        assert r["slots"]["verification_status"] == "amount_mismatch"
        r = await _turn(engine, "no, that's all, thank you", s, language="en-IN")
        assert r["done"] is True
        assert payloads[-1]["args"]["conversation_language"] == "en-IN"


class TestSummaryFieldsContract:
    def test_summary_fields_mirror_the_slots_and_never_use_the_llm(self):
        by_name = {f["name"]: f for f in M.SUMMARY_FIELDS}
        assert set(by_name) >= {
            "ticket_type", "deduction_explained", "amount_informed", "informed_amount",
            "deducted_amount", "amount_matches", "payment_mode", "upfront_amount_paid",
            "deduction_date_or_week", "verification_status", "additional_concern",
        }
        assert all(f["allowLlm"] is False for f in M.SUMMARY_FIELDS)
        assert by_name["verification_status"]["options"] == [
            "consistent", "amount_mismatch", "amount_not_communicated"]

    def test_workflow_validates_structurally(self):
        from backend.routers.workflows import validate_definition

        nodes, edges = M.build_workflow()
        errors, issues = validate_definition(nodes, edges)
        assert errors == []
        assert [i for i in issues if i["level"] == "error"] == []


class TestAuditRegressions:
    def test_ticket_api_contract_rejects_missing_context_and_invalid_fields(self):
        schema = M.build_connection("bot_ob")["requestSchema"]
        valid = {**CONTEXT, "ticket_type": M.TICKET_TYPE, "verification_status": "consistent"}
        assert te.validate_args(schema, valid) == []
        assert te.validate_args(schema, {})
        for key in ("ticket_id", "partner_id"):
            args = {k: v for k, v in valid.items() if k != key}
            assert te.validate_args(schema, args)
        for invalid in ({"verification_status": "anything"}, {"informed_amount": "banana"},
                        {"amount_matches": "maybe"}):
            assert te.validate_args(schema, valid | invalid)

    async def test_numeric_contradiction_overrides_yes_and_reaches_api(self, engine, payloads):
        s = "audit-numeric"
        r = await _run(engine, ["haan", "haan bataya tha", "haan 500 bataya tha", "haan, 700 kata hai"], s)
        assert r["slots"]["amount_matches"] == "no"
        assert _asked(r, M.Q_DEDUCTION_WEEK)
        r = await _turn(engine, "pichle hafte", s)
        assert "500" in r["reply"] and "700" in r["reply"]
        assert r["responseMode"] == "exact"
        r = await _turn(engine, "haan sahi hai", s)
        assert r["slots"]["verification_status"] == "amount_mismatch"
        r = await _turn(engine, "nahi", s)
        assert r["done"]
        assert payloads[-1]["args"]["verification_status"] == "amount_mismatch"

    async def test_no_amount_correction_clears_stale_match_and_amount(self, engine, payloads):
        s = "audit-correction"
        r = await _run(engine, ["haan", "haan bataya tha, 500 katega bola tha, utna hi kata",
                                "nahi, amount nahi bataya tha", "800 kata hai"], s)
        assert r["slots"]["amount_informed"] == "no"
        assert "informed_amount" not in r["slots"]
        assert "amount_matches" not in r["slots"]
        assert "अंतर" not in r["reply"] and "उतना ही" not in r["reply"]
        assert "800" in r["reply"]
        r = await _turn(engine, "haan sahi hai", s)
        assert r["slots"]["verification_status"] == "amount_not_communicated"
        await _turn(engine, "nahi", s)
        args = payloads[-1]["args"]
        assert "informed_amount" not in args and "amount_matches" not in args

    async def test_unknown_comparison_readback_is_not_invented(self, engine):
        r = await _run(engine, ["haan", "haan bataya tha par amount nahi bataya tha", "aath sau rupaye kate hain"], "audit-readback")
        assert r["responseMode"] == "exact"
        assert "800" in r["reply"]
        assert "अंतर" not in r["reply"] and "उतना ही" not in r["reply"]

    async def test_english_authored_questions_and_readback(self, engine):
        r = await _run(engine, ["yes speaking", "Yes, it was explained to me"], "audit-en", "en-IN")
        assert r["reply"] == M.ENGLISH_TEXT["n_ask_amount_informed"]
        r = await _turn(engine, "They told me 500 rupees would be deducted, and the same amount was deducted", "audit-en", "en-IN")
        assert r["responseMode"] == "exact"
        # grouped natural readback: told 500, same amount deducted, one confirmation
        assert "you were told 500 rupees" in r["reply"]
        assert "same amount was deducted" in r["reply"]
        assert "difference" not in r["reply"]
        assert r["reply"].endswith("Is all of this correct?")


class TestUserExamples:
    """The examples from the 2026-09-11 review (Hindi / Hinglish / English)."""

    @pytest.mark.parametrize("utterance", [
        "Mujhe onboarding fee ke baare mein bataya gaya tha. Bola tha 300 katega, lekin 400 cut gaya.",
        "They told me three hundred but four hundred was deducted.",
        "Mujhe 300 बताया था लेकिन 400 कट गया.",
    ])
    async def test_example_1_informed_300_deducted_400(self, engine, payloads, utterance):
        s = "ex1-" + str(abs(hash(utterance)) % 10_000)
        await _turn(engine, "haan", s)
        r = await _turn(engine, utterance, s)
        slots = r["slots"]
        assert slots["deduction_explained"] == "yes"
        assert slots["amount_informed"] == "yes"
        assert slots["informed_amount"] == "300"
        assert slots["deducted_amount"] == "400"
        assert slots["amount_matches"] == "no"
        for q in (M.Q_AMOUNT_INFORMED, M.Q_INFORMED_AMOUNT, M.Q_AMOUNT_MATCHES, M.Q_DEDUCTED_AMOUNT):
            assert not _asked(r, q)
        assert _asked(r, M.Q_DEDUCTION_WEEK)             # the only still-missing fact
        r = await _turn(engine, "pichle hafte", s)
        assert r["trace"][-1] == "n_hub_verify"
        r = await _turn(engine, "haan sahi hai", s)
        assert r["slots"]["verification_status"] == "amount_mismatch"
        r = await _turn(engine, "nahi", s)
        payload = payloads[-1]["args"]
        assert (payload["informed_amount"], payload["deducted_amount"],
                payload["amount_matches"], payload["verification_status"]) == \
            ("300", "400", "no", "amount_mismatch")

    async def test_example_2_not_explained_amount_unknown(self, engine):
        s = "ex2"
        await _turn(engine, "haan", s)
        r = await _turn(engine, "Mujhe onboarding fee ke baare mein nahi bataya gaya tha, bas paise cut gaye.", s)
        assert r["slots"]["deduction_explained"] == "no"
        assert "deducted_amount" not in r["slots"]
        assert "amount_informed" not in r["slots"]
        assert "n_msg_explain" in r["trace"]
        assert "onboarding fee" in r["reply"] and "installment" in r["reply"]
        assert _asked(r, M.Q_DEDUCTED_AMOUNT)             # explain, then the unknown amount
        assert not _asked(r, M.Q_AMOUNT_INFORMED)

    async def test_example_3_not_explained_400_deducted(self, engine):
        s = "ex3"
        await _turn(engine, "haan", s)
        r = await _turn(engine, "Mujhe fee ke baare mein nahi bataya tha aur 400 rupaye cut gaye.", s)
        assert r["slots"]["deduction_explained"] == "no"
        assert r["slots"]["deducted_amount"] == "400"
        assert "n_msg_explain" in r["trace"]
        assert not _asked(r, M.Q_DEDUCTED_AMOUNT)         # 400 already known
        assert _asked(r, M.Q_AMOUNT_INFORMED)             # next genuinely missing fact
        r = await _turn(engine, "nahi, kuch nahi bataya", s)
        assert r["slots"]["amount_informed"] == "no"
        assert r["trace"][-1] == "n_hub_verify"

    async def test_same_amount_without_figures(self, engine):
        s = "same1"
        await _turn(engine, "haan", s)
        r = await _turn(engine, "haan bataya tha, amount bhi bataya tha aur same amount kata", s)
        assert r["slots"]["amount_matches"] == "yes"
        assert r["slots"]["amount_informed"] == "yes"
        assert _asked(r, M.Q_INFORMED_AMOUNT)             # only the figure is still open
        r = await _turn(engine, "yaad nahi", s)
        assert r["trace"][-1] == "n_hub_verify"
        assert not _asked(r, M.Q_AMOUNT_MATCHES)

    async def test_yes_to_no_change_at_readback(self, engine):
        s = "flip1"
        await _turn(engine, "haan", s)
        await _turn(engine, "haan bataya tha, 500 katega bola tha, utna hi kata", s)
        r = await _turn(engine, "nahi nahi, mujhe deduction ke baare mein nahi bataya gaya tha", s)
        assert r["slots"]["deduction_explained"] == "no"
        assert "n_msg_explain" in r["trace"]              # explained now, once
        assert r["slots"]["informed_amount"] == "500"     # untouched facts kept
        assert r["slots"]["amount_matches"] == "yes"
        # the not-explained path wants the actual figure, which was never given
        assert _asked(r, M.Q_DEDUCTED_AMOUNT)
        r = await _turn(engine, "paanch sau hi kata", s)
        assert r["slots"]["deducted_amount"] == "500"
        assert r["trace"][-1] == "n_hub_verify"
        assert "n_msg_explain" not in r["trace"]          # never re-explained


class TestSttPunctuation:
    """Sarvam STT ends sentences with a danda (।), which sits inside the
    Devanagari Unicode block — word-end lookaheads must still fire (cv_e31d7ca6470b)."""

    async def test_hinglish_caller_with_danda_terminated_sentences(self, engine):
        s = "danda1"
        await _turn(engine, "हाँ जी बोलिए।", s)
        r = await _turn(engine, "मुझे ऑनबोर्डिंग फी के बारे में नहीं बताया था। और चार सौ रुपये कट गए।", s)
        assert r["slots"]["deduction_explained"] == "no"
        assert r["slots"]["deducted_amount"] == "400"
        assert "n_msg_explain" in r["trace"]
        assert not _asked(r, M.Q_DEDUCTED_AMOUNT)
        assert _asked(r, M.Q_AMOUNT_INFORMED)
        r = await _turn(engine, "नहीं, कुछ नहीं बताया था।", s)
        assert r["slots"]["amount_informed"] == "no"
        assert r["trace"][-1] == "n_hub_verify"

    async def test_q2_answer_given_at_the_amount_ask_is_kept(self, engine):
        s = "danda2"
        await _turn(engine, "हाँ", s)
        r = await _turn(engine, "नहीं बताया गया था।", s)
        assert _asked(r, M.Q_DEDUCTED_AMOUNT)
        r = await _turn(engine, "नहीं, कुछ नहीं बताया था।", s)       # answers Q2 while the amount is pending
        assert r["slots"]["amount_informed"] == "no"
        assert "deducted_amount" not in r["slots"]
        r = await _turn(engine, "चार सौ रुपये कटे हैं।", s)
        assert r["slots"]["deducted_amount"] == "400"
        assert not _asked(r, M.Q_AMOUNT_INFORMED)                    # already known
        assert r["trace"][-1] == "n_hub_verify"


class TestSttVariantsFromVoiceRuns:
    async def test_teen_sau_bataya_bat_chaar_sau_kat_hua(self, engine):
        # Sarvam STT rendering of "300 bataya tha but 400 cut hua" (cv_d8ddcb2291ea)
        s = "stt-300-400"
        await _turn(engine, "हाँ जी।", s)
        r = await _turn(engine, "मुझे तीन सौ बताया था, बट चार सौ कट हुआ।", s)
        assert r["slots"]["informed_amount"] == "300"
        assert r["slots"]["deducted_amount"] == "400"
        assert r["slots"]["amount_matches"] == "no"
        assert not _asked(r, M.Q_AMOUNT_MATCHES)
        assert _asked(r, M.Q_DEDUCTION_WEEK)

    async def test_contrast_without_deduction_verb_is_not_reasked(self, engine):
        # cv_1979484122a8 (live, Sarvam STT): "200 ke bajaye mera 300 rupaye … ka tha"
        # names both figures with NO deduction verb — the bot asked Q3 and the
        # deducted amount again although both were already on the table.
        s = "cv_1979484122a8"
        await _turn(engine, "Haan bol raha hoon", s)
        r = await _turn(engine, "Haan mujhe bataya gaya tha ki anurodhan fee jo hai do sau rupaya "
                                "katega Aur do sau ke bajaye mera teen sau rupaye onloading fees ka tha", s)
        assert r["slots"]["deduction_explained"] == "yes"
        assert r["slots"]["amount_informed"] == "yes"
        assert (r["slots"]["informed_amount"], r["slots"]["deducted_amount"]) == ("200", "300")
        assert r["slots"]["amount_matches"] == "no"
        assert "n_msg_amounts_differ" in r["trace"]            # acknowledged, not re-asked
        assert not _asked(r, M.Q_AMOUNT_INFORMED) and not _asked(r, M.Q_INFORMED_AMOUNT)
        assert not _asked(r, M.Q_AMOUNT_MATCHES) and not _asked(r, M.Q_DEDUCTED_AMOUNT)
        assert _asked(r, M.Q_DEDUCTION_WEEK)
        r = await _turn(engine, "last Tuesday ko", s)
        assert r["trace"][-1] == "n_hub_verify"
        assert "200 रुपये बताए गए थे" in r["reply"] and "300 रुपये deduct हुए" in r["reply"]

    @pytest.mark.parametrize("utterance", [
        "haan bataya tha. 200 ke bajaye 300 kat gaya",
        "हाँ बताया था, दो सौ की जगह मेरा तीन सौ कटा।",
        "haan, 200 ke badle 300 rupaye ka deduction tha",
    ])
    async def test_hindi_contrast_variants_fill_both_figures(self, engine, utterance):
        s = "contrast-" + str(abs(hash(utterance)))
        await _turn(engine, "haan", s)
        # the leading "haan" reaches the engine as the classifier's affirm signal
        r = await _turn(engine, utterance, s, signal="affirm")
        assert (r["slots"]["informed_amount"], r["slots"]["deducted_amount"],
                r["slots"]["amount_matches"]) == ("200", "300", "no")
        assert not _asked(r, M.Q_AMOUNT_MATCHES) and not _asked(r, M.Q_DEDUCTED_AMOUNT)
        assert _asked(r, M.Q_DEDUCTION_WEEK)

    @pytest.mark.parametrize("utterance", [
        "Yes, they told me. Instead of 200 they took 300 from my payout.",
        "yes I was told, but 300 was deducted instead of 200",
        "Yes. They charged 300 rather than the 200 they had mentioned.",
        # the figure after "told," is the deducted one — the contrast wins over "told <figure>"
        "yes I was told, 300 instead of 200",
    ])
    async def test_english_contrast_variants_fill_both_figures(self, engine, utterance):
        s = "contrast-en-" + str(abs(hash(utterance)))
        await _turn(engine, "yes", s, language="en-IN")
        r = await _turn(engine, utterance, s, language="en-IN", signal="affirm")
        assert (r["slots"]["informed_amount"], r["slots"]["deducted_amount"],
                r["slots"]["amount_matches"]) == ("200", "300", "no")
        # filled asks are walked (slot_reused) but never spoken
        for node in ("n_ask_amount_informed", "n_ask_informed_amount",
                     "n_ask_amount_matches", "n_ask_deducted_amount"):
            assert M.ENGLISH_TEXT[node] not in r["reply"]
        assert r["trace"][-1] == "n_ask_deduction_week"
        assert M.ENGLISH_TEXT["n_msg_amounts_differ"] in r["reply"]

    async def test_told_figure_without_contrast_is_still_the_informed_amount(self, engine):
        s = "told-plain"
        await _turn(engine, "yes", s, language="en-IN")
        r = await _turn(engine, "Yes, I was told 200", s, language="en-IN", signal="affirm")
        assert r["slots"]["informed_amount"] == "200"
        assert "deducted_amount" not in r["slots"]

    async def test_contrast_question_about_values_fills_nothing(self, engine):
        s = "contrast-q"
        await _turn(engine, "haan", s)
        r = await _turn(engine, "kya 200 ke bajaye 300 kat sakta hai?", s)
        for key in ("informed_amount", "deducted_amount", "amount_matches", "amount_informed"):
            assert key not in r["slots"]

    async def test_contrast_with_a_deduction_verb_still_prefers_the_verb(self, engine):
        # "500 katega bola tha, 300 ke bajaye 700 kata" — the verb-anchored figure wins
        s = "contrast-verb"
        await _turn(engine, "haan", s)
        r = await _turn(engine, "haan bataya tha, 500 katega bola tha aur 700 kata", s, signal="affirm")
        assert (r["slots"]["informed_amount"], r["slots"]["deducted_amount"]) == ("500", "700")

    async def test_explanation_step_is_flagged_as_the_kb_answer(self, engine):
        s = "kb-covered"
        await _turn(engine, "haan", s)
        r = await _turn(engine, "Nahi bataya tha. Waise onboarding fee kya hoti hai?", s)
        assert "n_msg_explain" in r["trace"]
        assert r["knowledgeCovered"] is True
        r2 = await _turn(engine, "400 rupaye", s)
        assert r2["knowledgeCovered"] is False


class TestNaturalReadback:
    async def test_200_300_last_week_monday(self, engine):
        s = "rb-1"
        await _turn(engine, "haan", s)
        await _turn(engine, "haan bataya gaya tha", s)
        r = await _turn(engine, "haan, mujhe bataya tha ki 200 katega, lekin 300 kata", s)
        assert (r["slots"]["informed_amount"], r["slots"]["deducted_amount"], r["slots"]["amount_matches"]) == ("200", "300", "no")
        r = await _turn(engine, "पिछले हफ्ते मंडे को।", s)
        assert r["slots"]["deduction_date_or_week"] == "पिछले हफ्ते मंडे"      # weekday kept
        reply = r["reply"]
        assert "200 रुपये बताए गए थे" in reply and "300 रुपये deduct हुए" in reply
        assert "100 रुपये का difference" in reply                              # derived, not stored
        assert "amount_difference" not in r["slots"]
        assert "पिछले हफ्ते मंडे के payout" in reply
        assert reply.count("बताया गया था") == 1                                   # no robotic repetition
        assert reply.count("?") == 1
        # correction at the readback: acknowledged with the new value, no full re-read
        r = await _turn(engine, "नहीं, Sunday नहीं, Monday को हुआ था.", s)
        assert r["slots"]["deduction_date_or_week"] == "Monday"
        assert "Monday" in r["reply"] and "बाकी सारी details सही हैं ना" in r["reply"]
        assert "200 रुपये बताए गए थे" not in r["reply"]
        assert r["trace"][-1] == "n_hub_verify"

    async def test_300_400_difference_and_equal_amounts(self, engine):
        s = "rb-2"
        await _turn(engine, "haan", s)
        r = await _turn(engine, "haan bataya tha, 300 katega bola tha, lekin 400 kata", s)
        r = await _turn(engine, "pichle hafte", s)
        assert "100 रुपये का difference" in r["reply"]
        s2 = "rb-3"
        await _turn(engine, "haan", s2)
        r = await _turn(engine, "haan bataya tha, 500 katega bola tha aur 500 hi kate", s2)
        assert "उतना ही amount deduct हुआ" in r["reply"] and "difference" not in r["reply"]

    async def test_closing_never_claims_an_unconfirmed_ticket_update(self, engine, payloads):
        s = "rb-4"
        await _turn(engine, "haan", s)
        await _turn(engine, "haan bataya tha, 500 katega bola tha aur 500 hi kate", s)
        await _turn(engine, "haan sahi hai", s)
        r = await _turn(engine, "nahi", s)
        assert r["done"] is True
        assert "update कर रहा" not in r["reply"] and "confirm नहीं हुआ" not in r["reply"]
