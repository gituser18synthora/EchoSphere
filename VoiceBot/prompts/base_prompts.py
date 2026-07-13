"""Shared / classifier prompts (non-system-prompt assembly)."""

INTENT_CLASSIFIER_SYSTEM_PROMPT = (
    "You are an intent classifier. "
    "Return only valid JSON. No markdown. No explanation."
)


def build_intent_classification_user_prompt(
    intent_block: str,
    caller_text: str,
) -> str:
    """User message for intent LLM call. `intent_block` is the bullet list of intents."""
    return f"""You are an intent classifier for a voice assistant.
Classify the caller utterance into exactly one intent.

Available intents:
{intent_block}

Rules:
- Return ONLY valid JSON. No markdown. No explanation.
- confidence must be float 0.0 to 1.0
- If ambiguous return most likely intent with lower confidence
- If nothing matches use general_query
- sentiment reflects caller emotional tone in this utterance

Caller said: "{caller_text}"

Respond with:
{{"intent": "<label>", "confidence": <float>, "sentiment": "<positive|neutral|negative|frustrated>"}}"""
