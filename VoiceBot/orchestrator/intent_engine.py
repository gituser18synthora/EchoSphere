# orchestrator/intent_engine.py

import json
import logging

from voicebot.adapters.base import LLMAdapter
from voicebot.config_layer.models import VoicebotConfig
from voicebot.orchestrator.call_state import CallState, IntentResult
from voicebot.prompts.base_prompts import (
    INTENT_CLASSIFIER_SYSTEM_PROMPT,
    build_intent_classification_user_prompt,
)

logger = logging.getLogger(__name__)


class IntentEngine:
    def __init__(self, llm_adapter: LLMAdapter, config: VoicebotConfig):
        self._llm = llm_adapter
        self._config = config

    async def classify(
        self,
        text: str,
        call_state: CallState,
    ) -> IntentResult:
        """
        Classify caller utterance into one intent.
        Uses LLM with temperature=0.1 for consistency.
        Always returns a safe IntentResult even on failure.
        """
        prompt = self._build_prompt(text)
        try:
            response = await self._llm.generate(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=INTENT_CLASSIFIER_SYSTEM_PROMPT,
                max_tokens=100,
                temperature=0.1,
            )
            call_state.usage.record_llm(response)
            return self._parse_response(response.text)
        except Exception as e:
            logger.error(f"Intent classification failed: {e}")
            return IntentResult(
                intent="general_query",
                confidence=0.5,
                sentiment="neutral",
            )

    def _build_prompt(self, text: str) -> str:
        """
        Dynamically build intent prompt from enabled goals.

        Structure:
        1. Always-present intents
        2. Goal intents (only if enabled in config)
        3. Rules
        4. Caller text
        5. Expected JSON format

        Response format:
        {
          "intent": "<label>",
          "confidence": <float 0.0-1.0>,
          "sentiment": "positive|neutral|negative|frustrated"
        }
        """
        intent_lines = []
        ic = self._config.intent_config

        for intent, desc in ic.always_present_intents.items():
            intent_lines.append(f"- {intent}: {desc}")

        goals = self._config.goals
        for intent, flag in ic.goal_flag_map.items():
            if getattr(goals, flag, False):
                desc = ic.goal_intent_descriptions.get(intent, "")
                if desc:
                    intent_lines.append(f"- {intent}: {desc}")

        intent_block = "\n".join(intent_lines)

        return build_intent_classification_user_prompt(intent_block, text)

    def _parse_response(self, raw: str) -> IntentResult:
        """
        Parse LLM JSON response safely.
        Strip markdown fences.
        Clamp confidence 0.0-1.0.
        Validate sentiment value.
        On any failure return safe default.
        """
        try:
            clean = raw.strip()
            if clean.startswith("```"):
                parts = clean.split("```")
                clean = parts[1] if len(parts) > 1 else clean
                if clean.startswith("json"):
                    clean = clean[4:]
            clean = clean.strip()
            data = json.loads(clean)
            intent = data.get("intent", "general_query")
            confidence = float(data.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
            sentiment = data.get("sentiment", "neutral")
            if sentiment not in (
                "positive",
                "neutral",
                "negative",
                "frustrated",
            ):
                sentiment = "neutral"
            return IntentResult(
                intent=intent,
                confidence=confidence,
                sentiment=sentiment,
            )
        except Exception as e:
            logger.warning(f"Intent parse failed: {e}. Raw: {raw[:100]}")
            return IntentResult(
                intent="general_query",
                confidence=0.5,
                sentiment="neutral",
            )
