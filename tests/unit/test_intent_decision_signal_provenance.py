"""A configured intent name cannot override a validated semantic null."""
import pytest

from shared.orchestration.decision_schema import ConversationDecision
from shared.orchestration.intent_classifier import HybridIntentPipeline


@pytest.mark.parametrize("intent", ["affirm", "payment_intent", "already_paid", "refusal"])
def test_validated_null_is_preserved_even_for_platform_named_intents(intent):
    pipeline = HybridIntentPipeline(intents=[{"name": intent, "route": "workflow:test"}])
    decision = ConversationDecision(intent=intent, signal=None, confidence=0.95,
                                    next_action="continue_workflow")
    result = pipeline.from_decision(decision)
    assert result.intent == intent
    assert result.source == "goal_engine"
    assert result.signal is None


def test_explicit_signal_overrides_configured_platform_intent_name():
    pipeline = HybridIntentPipeline(intents=[{"name": "affirm", "route": "workflow:test"}])
    decision = ConversationDecision(intent="affirm", signal="refusal", confidence=0.95)
    result = pipeline.from_decision(decision)
    assert result.intent == "affirm"
    assert result.signal == "refusal"


def test_signal_provenance_does_not_change_configured_tool_binding():
    pipeline = HybridIntentPipeline(intents=[{"name": "payment_intent", "route": "tool:lookup"}])
    result = pipeline.from_decision(ConversationDecision(
        intent="payment_intent", signal=None, confidence=0.95,
    ))
    assert result.signal is None
    assert result.requires_tool is True
    assert result.tool_name == "lookup"


def test_legacy_configured_intent_classification_keeps_its_platform_signal():
    pipeline = HybridIntentPipeline(intents=[{"name": "affirm"}])
    result = pipeline._from_intent({"name": "affirm"}, 0.95, source="phrase")
    assert result.signal == "affirm"
