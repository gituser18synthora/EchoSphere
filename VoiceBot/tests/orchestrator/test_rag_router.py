"""Tests for server-side RAG prefetch routing."""

from voicebot.orchestrator.rag_router import is_usable_rag_result, should_prefetch_rag


def test_prefetch_skipped_for_greeting():
    assert not should_prefetch_rag(
        enable_rag=True,
        text="Hi there",
        intent="greeting",
    )


def test_prefetch_for_faq_intent():
    assert should_prefetch_rag(
        enable_rag=True,
        text="What is the recharge process?",
        intent="answer_faq",
    )


def test_prefetch_for_troubleshooting_question():
    assert should_prefetch_rag(
        enable_rag=True,
        text="My internet is very slow today",
        intent="general_query",
    )


def test_prefetch_disabled_when_enable_rag_false():
    assert not should_prefetch_rag(
        enable_rag=False,
        text="How do I reset my router?",
        intent="general_query",
    )


def test_prefetch_skipped_for_short_ack():
    assert not should_prefetch_rag(
        enable_rag=True,
        text="okay",
        intent="general_query",
    )


def test_usable_rag_result_rejects_empty_and_miss_messages():
    assert not is_usable_rag_result("")
    assert not is_usable_rag_result(
        "No specific information found in the knowledge base for that query."
    )
    assert is_usable_rag_result("Router reset: unplug power for 30 seconds.")
