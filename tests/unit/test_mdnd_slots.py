"""MDND extraction boundary checks and opt-in real-model semantic regressions."""

import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest

from shared.orchestration.mdnd_slots import extract_mdnd_slots
from shared.providers.base import LLMResult


def _provider(patch=None, evidence=None, **extra):
    body = {"patch": patch or {}, "evidence": evidence or {}, **extra}
    return AsyncMock(generate=AsyncMock(return_value=LLMResult(
        text=json.dumps(body, ensure_ascii=False), input_tokens=25, output_tokens=12,
    )))


async def _extract(llm, text="मैं वहाँ गया था", **kwargs):
    return await extract_mdnd_slots(
        llm, text=text, slots=kwargs.pop("slots", {}),
        pending_question=kwargs.pop("pending_question", "क्या आपने customer को call किया था?"),
        pending_variable=kwargs.pop("pending_variable", "m_called_customer"), **kwargs,
    )


async def test_all_fields_are_accepted_even_while_customer_call_is_pending():
    text = "I called the customer, left it at the door, and CX called me."
    patch = {"customer_called": "yes", "reached_location": "yes",
             "delivery_handoff": "doorstep", "cx_support_called": "yes"}
    llm = _provider(patch, {key: text for key in patch}, drop_location="at the door")
    result = await _extract(llm, text)
    assert result.patch == patch
    assert result.drop_location == "at the door"
    assert result.understood and not result.failed
    assert (result.input_tokens, result.output_tokens, result.requests) == (100, 48, 4)


async def test_prompt_receives_entire_utterance_state_and_bounded_reference_history():
    llm = _provider()
    text = "extra complaint " * 600 + "CX से कोई call नहीं आया"
    slots = {"customer_called": "yes", "delivery_handoff": "doorstep", "unrelated": "secret"}
    history = [{"role": "user", "content": str(i) + "x" * 1000} for i in range(12)]
    history.append({"role": "system", "content": "not conversation evidence"})
    await _extract(llm, text, slots=slots, history=history)
    requests = [json.loads(call.args[0][0]["content"]) for call in llm.generate.call_args_list]
    assert {item["target_field"] for item in requests} == {
        "customer_called", "reached_location", "delivery_handoff", "cx_support_called"}
    for request in requests:
        assert request["latest_partner_utterance"] == text
        assert "unrelated" not in request["stored_slots"]
        assert set(request["stored_slots"]) <= {request["target_field"]}
        assert request["pending_variable"] == "customer_called"
        assert len(request["recent_history"]) <= 6
        assert all(len(item["content"]) <= 800 for item in request["recent_history"])
    assert slots["unrelated"] == "secret" and len(history) == 13


async def test_invalid_enums_and_invented_evidence_do_not_pollute_state():
    llm = _provider(
        {"customer_called": "perhaps", "reached_location": "yes",
         "cx_support_called": "no", "delivery_handoff": ["guard"], "m_guard_name": "Raju"},
        {"reached_location": "मैं वहाँ गया था", "cx_support_called": "No CX call"},
        drop_location="doorstep", recipient_detail="Raju",
    )
    result = await _extract(llm)
    assert result.patch == {"reached_location": "yes"}
    assert result.evidence == {"reached_location": "मैं वहाँ गया था"}
    assert result.drop_location is None and result.recipient_detail is None


async def test_ordinary_unknown_never_erases_known_information():
    slots = {"customer_called": "yes", "reached_location": "yes"}
    result = await _extract(
        _provider({"customer_called": "unknown"}, {"customer_called": "याद नहीं"}),
        "याद नहीं", slots=slots,
    )
    assert result.patch == {} and result.explicit_retractions == ()
    assert slots == {"customer_called": "yes", "reached_location": "yes"}


async def test_explicit_retraction_and_latest_clear_correction_are_preserved():
    text = "Actually I did not call. I cannot confirm my earlier answer about reaching there."
    llm = _provider(
        {"customer_called": "no", "reached_location": "unknown"},
        {"customer_called": "Actually I did not call.",
         "reached_location": "I cannot confirm my earlier answer about reaching there."},
        explicit_retractions=["reached_location"],
    )
    result = await _extract(llm, text, slots={"customer_called": "yes", "reached_location": "yes"})
    assert result.patch == {"customer_called": "no", "reached_location": "unknown"}
    assert result.explicit_retractions == ("reached_location",)


async def test_handoff_details_must_be_quoted_from_actual_partner_response():
    llm = _provider(
        {"delivery_handoff": "other"}, {"delivery_handoff": "इन्वर्टर के ऊपर रखा"},
        drop_location="इन्वर्टर के ऊपर", recipient_detail="a security guard",
    )
    result = await _extract(llm, "मैंने order इन्वर्टर के ऊपर रखा था।")
    assert result.drop_location == "इन्वर्टर के ऊपर"
    assert result.recipient_detail is None


@pytest.mark.parametrize("raw", ["Sorry, I am checking your ticket", "[]", "{}",
                                      '{"patch":[],"evidence":{}}', "```json\nnope\n```"])
async def test_unstructured_or_invalid_output_cannot_be_used_as_speech(raw):
    llm = AsyncMock(generate=AsyncMock(return_value=LLMResult(text=raw)))
    result = await _extract(llm)
    assert result.failed and result.failure_reason == "invalid_response"
    assert result.patch == {} and not result.understood


async def test_fenced_json_and_whitespace_normalized_evidence_are_supported():
    raw = '```json\n{"patch":{"reached_location":"yes"},"evidence":{"reached_location":"I went there"}}\n```'
    result = await _extract(AsyncMock(generate=AsyncMock(return_value=LLMResult(text=raw))),
                            "I  went\nthere")
    assert result.patch == {"reached_location": "yes"}


async def test_provider_failure_is_not_a_state_change():
    llm = AsyncMock(generate=AsyncMock(side_effect=RuntimeError("upstream")))
    result = await _extract(llm)
    assert result.failed and result.failure_reason == "provider_error"
    assert result.patch == {}


async def test_timeout_cancels_extraction_without_erasing_state():
    cancelled = asyncio.Event()

    async def slow(*args, **kwargs):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    result = await _extract(AsyncMock(generate=AsyncMock(side_effect=slow)), timeout_seconds=0.001)
    assert result.failed and result.failure_reason == "timeout" and cancelled.is_set()
    assert result.patch == {}


async def test_external_cancellation_propagates():
    llm = AsyncMock(generate=AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await _extract(llm)


async def test_empty_utterance_never_calls_the_provider():
    llm = _provider()
    result = await _extract(llm, "  ")
    assert not result.failed and not result.understood
    llm.generate.assert_not_called()


@pytest.mark.parametrize("understood,expected", [(True, True), (False, False), ("yes", False)])
async def test_intelligible_complaint_is_distinct_from_unclear_speech(understood, expected):
    result = await _extract(_provider(understood=understood), "मेरा पैसा कट गया")
    assert result.patch == {} and result.understood is expected


# These assert the model's interpretation, not a mock's prewritten answers.
# Cases run on the same event loop as the configured provider's HTTP client.
SEMANTIC_CASES = [
    ("customer_negative_not_cx", "नहीं, कस्टमर को कॉल नहीं किया था।", "क्या आपने customer को call किया था?", "m_called_customer", {}, {"customer_called": "no"}),
    ("cv_365673967604", "असल में customer को call किया था, customer ने मुझे बताया कि product मेरे घर के सामने रख दो तो मैंने वहीं पर product रख दिया। मुझे CX support से call आया था।",
     "क्या हुआ था?", "m_issue_description", {},
     {"customer_called": "yes", "reached_location": "yes", "delivery_handoff": "doorstep", "cx_support_called": "yes"}),
    ("cv_87b3ba52431e_first", "मैं वहाँ पे गया था, customer था नहीं और उसने बोला मेरे door के बाहर parcel छोड़ के चले जाओ, तो मैंने parcel वहीं रखा।",
     "क्या हुआ था?", "m_issue_description", {},
     {"reached_location": "yes", "delivery_handoff": "doorstep"}),
    ("cv_87b3ba52431e_followup", "हाँ मैंने call किया था तभी तो उसने बताया call पर मेरे को कहाँ रखना है parcel",
     "क्या आपने customer को call किया था?", "m_called_customer",
     {"reached_location": "yes", "delivery_handoff": "doorstep"}, {"customer_called": "yes"}),
    ("hinglish_four", "Customer ko phone kiya tha, ghar tak gaya, uski mummy ko parcel de diya aur CX ka bhi call aaya tha.",
     "क्या हुआ था?", "m_issue_description", {},
     {"customer_called": "yes", "reached_location": "yes", "delivery_handoff": "family_member", "cx_support_called": "yes"}),
    ("english_four", "I rang the customer before delivery, went to their address and handed the parcel to the customer. Support never called me.",
     "What happened?", "m_issue_description", {},
     {"customer_called": "yes", "reached_location": "yes", "delivery_handoff": "customer", "cx_support_called": "no"}),
    ("out_of_order_two", "CX se koi call nahi aaya. Customer ko bhi phone nahi kiya tha.",
     "आप location पर पहुंचे थे?", "m_reached_location", {},
     {"cx_support_called": "no", "customer_called": "no"}),
    ("arbitrary_instructed_spot", "उनके घर जाकर order इन्वर्टर के ऊपर रख दिया था, जैसे उन्होंने कहा था।",
     "क्या आपने customer को call किया था?", "m_called_customer", {},
     {"reached_location": "yes", "delivery_handoff": "other"}),
    ("instruction_only", "Customer ने call पर बोला guard को दे देना, लेकिन अभी मैं निकला नहीं हूँ और order मेरे पास ही है।",
     "क्या हुआ था?", "m_issue_description", {},
     {"customer_called": "yes", "reached_location": "no", "delivery_handoff": "other"}),
    ("instruction_no_arrival_evidence", "Customer told me to leave it outside their door.",
     "What happened?", "m_issue_description", {}, {}),
    ("cx_is_not_customer", "हाँ CX support का फोन आया था, customer से बात नहीं हुई।",
     "क्या आपने customer को call किया था?", "m_called_customer", {},
     {"cx_support_called": "yes"}),
    ("brief_yes", "हाँ", "क्या आपने customer को call किया था?", "m_called_customer", {},
     {"customer_called": "yes"}),
    ("brief_no", "No", "Did CX Support call you about this delivery?", "m_cx_support_call", {},
     {"cx_support_called": "no"}),
    ("joint_yes", "जी हाँ", "क्या आप customer की location पर पहुंचे थे, और क्या आपने customer को call किया था?",
     "m_reached_location", {}, {"reached_location": "yes", "customer_called": "yes"}),
    ("identity_yes", "हाँ", "क्या मैं सौरभ से बात कर रहा हूँ?", "partner_identity", {}, {}),
    ("leading_yes_specific_other_fact", "हाँ customer को call किया था", "क्या आप location पर पहुंचे थे?",
     "m_reached_location", {}, {"customer_called": "yes"}),
    ("correct_recipient", "नहीं, guard को नहीं दिया था, उनकी माँ को दिया था।", "क्या सभी details सही हैं?",
     None, {"delivery_handoff": "guard"}, {"delivery_handoff": "family_member"}),
    ("correct_call", "Sorry, I said I called earlier, but actually I did not phone the customer.",
     "Who received the order?", "m_handover_recipient", {"customer_called": "yes"}, {"customer_called": "no"}),
    ("retract_call", "मैंने पहले बोला कि customer को call किया था पर अब मुझे याद नहीं कि call किया था या नहीं।",
     "क्या सब सही है?", None, {"customer_called": "yes"}, {"customer_called": "unknown"}),
    ("unclear_first", "अरे वो मतलब फिर ऐसे था आप सुनो हल्लो आवाज़",
     "बताइए क्या हुआ था?", "m_issue_description", {}, {}),
    ("complaint_interruption", "Wait, listen, this deduction is unfair! I went all the way to their house, handed it to security, and your CX team did call about it.",
     "Did you call the customer?", "m_called_customer", {},
     {"reached_location": "yes", "delivery_handoff": "guard", "cx_support_called": "yes"}),
    ("same_utterance_correction", "Customer ko call kiya tha, sorry galat bola, customer ko call nahi kiya tha. CX ka call aaya tha.",
     "क्या हुआ था?", "m_issue_description", {},
     {"customer_called": "no", "cx_support_called": "yes"}),
    ("final_summary_yes", "हाँ सब सही है", "आपने call किया, location पर पहुंचे, guard को दिया और CX का call आया, क्या सब सही है?",
     None, {"customer_called": "yes", "reached_location": "yes", "delivery_handoff": "guard", "cx_support_called": "yes"}, {}),
]


@pytest.mark.skipif(os.environ.get("RUN_LLM_TESTS") != "1", reason="set RUN_LLM_TESTS=1 to call the configured model")
async def test_real_model_semantic_regressions():
    from shared.config import get_settings
    from shared.providers.base import ProviderConfig
    from shared.providers.factory import get_llm_provider

    settings = get_settings()
    llm = get_llm_provider(ProviderConfig(
        provider=settings.llm_provider, model=settings.llm_model,
        api_key_reference=settings.llm_api_key_reference,
    ))
    failures = []
    for case, text, question, variable, slots, expected in SEMANTIC_CASES:
        result = await extract_mdnd_slots(
            llm, text=text, slots=slots, pending_question=question,
            pending_variable=variable, timeout_seconds=6.0,
            pending_fields=("reached_location", "customer_called") if case == "joint_yes" else None,
        )
        if result.failed or result.patch != expected:
            failures.append((case, expected, result))
    assert not failures, failures


# ── Deterministic evidence gate ──────────────────────────────────────────────
# A verbatim quote is necessary but not sufficient: it must talk about the
# field. Live cv_2e2d8c7ce20d-style failures — the model answering the bot's
# OWN improvised confirmation ("haan, ye sahi hai") with a recipient, or
# carrying a customer-call negative into the CX field — must not reach state.

async def test_bare_yes_never_answers_the_handover_field():
    text = "हाँ, ये सही है।"
    llm = _provider({"delivery_handoff": "family_member"}, {"delivery_handoff": text},
                    recipient_detail="mother")
    result = await _extract(llm, text, pending_variable="m_handover_recipient",
                            pending_question="ये order आपने किसको सौंपा था?")
    assert result.patch == {} and result.recipient_detail is None


async def test_bare_yes_answers_only_the_pending_yes_no_field():
    text = "हाँ"
    patch = {"customer_called": "yes", "cx_support_called": "yes", "reached_location": "yes"}
    llm = _provider(patch, {key: text for key in patch})
    result = await _extract(llm, text, pending_variable="m_cx_support_call",
                            pending_question="क्या आपको CX support से कोई call आया था?")
    assert result.patch == {"cx_support_called": "yes"}


async def test_bare_yes_at_the_combined_question_answers_both_halves():
    text = "haan"
    patch = {"customer_called": "yes", "reached_location": "yes", "cx_support_called": "yes"}
    llm = _provider(patch, {key: text for key in patch})
    result = await _extract(
        llm, text, pending_variable="m_reached_location",
        pending_fields=("reached_location", "customer_called"),
        pending_question="क्या आप delivery के लिए customer की location पर पहुंचे थे, और क्या आपने customer को call किया था?")
    assert result.patch == {"customer_called": "yes", "reached_location": "yes"}


async def test_bare_yes_at_a_hub_creates_no_delivery_fact():
    text = "yes, all correct"
    patch = {"customer_called": "yes", "reached_location": "yes"}
    llm = _provider(patch, {key: text for key in patch})
    result = await _extract(llm, text, pending_variable="", pending_question="Is all of this correct?")
    assert result.patch == {}


async def test_field_words_in_the_quote_are_accepted_whatever_is_pending():
    text = "सीएक्स सपोर्ट से मुझे कॉल आया था और मैंने गार्ड को हैंडओवर कर दिया था"
    patch = {"cx_support_called": "yes", "delivery_handoff": "guard"}
    llm = _provider(patch, {"cx_support_called": "सीएक्स सपोर्ट से मुझे कॉल आया था",
                            "delivery_handoff": "गार्ड को हैंडओवर कर दिया था"})
    result = await _extract(llm, text, pending_variable="m_reached_location",
                            pending_question="क्या आप customer की location पर पहुंचे थे?")
    assert result.patch == patch
