"""Opt-in opening retries preserve intent routing and completed workflows."""

import pytest

from shared.orchestration.router import RouteDecision, RouteKind, TurnRouter


def router(*, fallback="clarify", extra_intents=()):
    return TurnRouter(
        intents=[
            {
                "name": "start_enquiries",
                "samples": ["yes", "हाँ", "haan"],
                "route": "workflow:delivery_enquiry",
                "confidence_threshold": 0.4,
                "fallback_behavior": fallback,
            },
            *extra_intents,
        ],
        has_knowledge_bases=True,
    )


@pytest.mark.parametrize("message", [
    "कंज्यूम कर रहे हैं ना।",  # First unclear STT in cv_1090953f3652.
    "अपने बैठने की जगह नहीं है। मैम के केबिन में।",  # Its next turn.
    "hmm",
    "hello",
    "",
    "I could not hear clearly",
])
def test_unclear_opening_retries_pending_identity_question(message):
    current = router()
    decision = current.apply_entry_fallback(current.decide(message))

    assert decision.kind == RouteKind.CLARIFY
    assert decision.reason == "entry_reprompt"
    assert decision.action == "repeat_entry_question"
    assert decision.intent == "start_enquiries"


@pytest.mark.parametrize("fallback", [None, "llm", "handoff"])
def test_existing_bots_without_opt_in_keep_their_route(fallback):
    current = router(fallback=fallback)
    original = current.decide("कंज्यूम कर रहे हैं ना।")

    assert current.apply_entry_fallback(original) is original
    assert original.kind == RouteKind.CHAT


@pytest.mark.parametrize("message, kind", [
    ("हाँ", RouteKind.WORKFLOW),
    ("haan main bol raha hoon", RouteKind.WORKFLOW),
    ("please hang up the call", RouteKind.CALL_CONTROL),
    ("transfer me to a human agent", RouteKind.HANDOFF),
    ("could you repeat that please", RouteKind.CALL_CONTROL),
    ("What is the delivery refund policy?", RouteKind.KNOWLEDGE),
])
def test_meaningful_opening_routes_take_priority(message, kind):
    current = router()
    original = current.decide(message)

    assert original.kind == kind
    assert current.apply_entry_fallback(original) is original


@pytest.mark.parametrize("kind", [
    RouteKind.WORKFLOW, RouteKind.KNOWLEDGE, RouteKind.HANDOFF,
    RouteKind.CALL_CONTROL, RouteKind.TOOL, RouteKind.INTENT, RouteKind.SAFETY,
])
def test_semantic_route_upgrade_is_never_replaced(kind):
    current = router()
    # The brain calls the fallback after classification has upgraded a
    # default-chat result, including Hindi/English meanings without samples.
    original = RouteDecision(kind=kind, reason="semantic_intent", intent="known")

    assert current.apply_entry_fallback(original) is original


@pytest.mark.parametrize("state", [
    {"active_workflow": "delivery_enquiry"},
    {"allow_affirm_entry": False},
])
def test_active_and_completed_flows_never_restart_the_opening(state):
    current = router()
    original = RouteDecision(kind=RouteKind.CHAT, reason="default_chat")

    assert current.apply_entry_fallback(original, **state) is original


def test_ambiguous_entry_configuration_cannot_choose_an_opening_question():
    current = router(extra_intents=[{
        "name": "another_opening",
        "samples": ["yes"],
        "route": "workflow:another_flow",
        "fallback_behavior": "clarify",
    }])
    original = current.decide("कंज्यूम कर रहे हैं ना।")

    assert current.apply_entry_fallback(original) is original
