from shared.knowledge.retrieval.retriever import _has_significant_lexical_overlap


def test_mock_gate_rejects_brand_only_overlap():
    assert not _has_significant_lexical_overlap(
        "What is served in the ZEE5 office cafeteria for lunch today?",
        "ZEE5 subscription upgrade rules and differential payment guidance.",
    )


def test_mock_gate_keeps_meaningful_subscription_overlap():
    assert _has_significant_lexical_overlap(
        "What current ZEE5 plan includes live sports?",
        "The All Access plus Sports plan includes live sports in India.",
    )


def test_mock_gate_does_not_treat_question_words_as_evidence():
    assert not _has_significant_lexical_overlap(
        "Where can I see this today?",
        "This document explains where a subscription can be purchased.",
    )
