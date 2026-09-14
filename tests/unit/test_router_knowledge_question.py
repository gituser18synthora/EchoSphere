"""TurnRouter.detect_knowledge_question — a knowledge question folded into a
workflow answer is found clause by clause (cv_56df956b0430)."""

from shared.orchestration.router import TurnRouter

INTENTS = [
    {"name": "policy_question", "route": "knowledge", "confidence_threshold": 0.5,
     "samples": ["onboarding fee kya hoti hai", "upfront fee kya hoti hai",
                 "kya ye har store ke liye same hoti hai", "what is the onboarding fee",
                 "ऑनबोर्डिंग फीस क्या होती है", "installment me fee deduct hoti hai kya",
                 "why is the onboarding fee deducted from my payout",
                 "क्या फीस किस्तों में कट सकती है", "remaining fee payout se kab kategi", "ऑनबोर्डिंग फी क्या होती है"]},
    {"name": "start", "route": "workflow:wf_1", "confidence_threshold": 0.4,
     "samples": ["haan", "yes"]},
]


def _router(has_kbs=True):
    return TurnRouter(intents=INTENTS, has_knowledge_bases=has_kbs)


def test_sample_match_on_a_standalone_question():
    assert _router().detect_knowledge_question("onboarding fee kya hoti hai?") is not None


def test_question_clause_after_an_answer_is_returned_as_the_query():
    clause = _router().detect_knowledge_question(
        "Mujhe 300 bataya tha but 400 cut hua. Waise har store ki onboarding fee same hoti hai kya?")
    assert clause is not None
    assert "store" in clause and "300" not in clause


def test_topic_plus_question_shape_generalizes_beyond_samples():
    assert _router().detect_knowledge_question("Upfront fee kya hai aur mere 500 cut gaye.") == "Upfront fee kya hai"
    assert _router().detect_knowledge_question("Nahi bataya tha. Waise ye onboarding fee kyu lete hain?") is not None
    assert _router().detect_knowledge_question("Why was this fee deducted from my payout?") is not None
    assert _router().detect_knowledge_question("क्या फीस किस्तों में कट सकती है") is not None


def test_plain_answers_and_confirmations_are_not_questions():
    r = _router()
    assert r.detect_knowledge_question("haan bataya tha, 500 katega bola tha, utna hi kata") is None
    assert r.detect_knowledge_question("sahi hai na?") is None
    assert r.detect_knowledge_question("300 bola tha but 400 cut gaya") is None
    assert r.detect_knowledge_question("nahi, kuch nahi bataya tha") is None


def test_without_knowledge_bases_nothing_is_detected():
    assert _router(has_kbs=False).detect_knowledge_question("onboarding fee kya hoti hai?") is None


def test_a_single_shared_topic_word_is_not_enough():
    r = _router()
    assert r.detect_knowledge_question("Store manager ka number kya hai?") is None
    assert r.detect_knowledge_question("Meri salary kab aayegi?") is None
    assert r.detect_knowledge_question("Aaj weather kaisa hai?") is None
    # two topic words still qualify
    assert r.detect_knowledge_question("Upfront fee 300 hoti hai kya?") is not None


def test_stt_spelling_of_fee_counts_as_a_topic_word():
    # Sarvam STT writes फीस as फी and turned "refund" into "फंड" (cv_d327fad0ee31)
    clause = _router().detect_knowledge_question(
        "मुझे तीन सौ बोला था, बट चार सौ कट गया। वैसे ऑनबोर्डिंग फी फंड होती है क्या?")
    assert clause is not None and "ऑनबोर्डिंग फी" in clause
    assert _router().detect_knowledge_question("हाँ, बताया गया था।") is None
