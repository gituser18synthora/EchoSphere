"""
Centralized prompt strings and builders for the voicebot.

Import from submodules for clarity, or use ``PromptRegistry`` for a single entry point.
"""

from voicebot.prompts.base_prompts import (
    INTENT_CLASSIFIER_SYSTEM_PROMPT,
    build_intent_classification_user_prompt,
)
from voicebot.prompts.extraction_prompts import (
    get_entity_extraction_prompt,
    get_entity_extraction_system_prompt,
)
from voicebot.prompts.rag_prompts import build_rag_context_prompt, build_rag_miss_prompt
from voicebot.prompts.system_prompts import (
    RUNNING_SUMMARY_SYSTEM_PROMPT,
    append_running_summary_section,
    build_running_summary_user_prompt,
)

__all__ = [
    "INTENT_CLASSIFIER_SYSTEM_PROMPT",
    "PromptRegistry",
    "RUNNING_SUMMARY_SYSTEM_PROMPT",
    "append_running_summary_section",
    "build_intent_classification_user_prompt",
    "build_rag_context_prompt",
    "build_rag_miss_prompt",
    "build_running_summary_user_prompt",
    "get_entity_extraction_prompt",
    "get_entity_extraction_system_prompt",
]


class PromptRegistry:
    """Optional facade for tests or dynamic prompt lookup."""

    extraction = staticmethod(get_entity_extraction_prompt)
    extraction_system = staticmethod(get_entity_extraction_system_prompt)
    rag = staticmethod(build_rag_context_prompt)
    running_summary_user = staticmethod(build_running_summary_user_prompt)
    running_summary_system = staticmethod(lambda: RUNNING_SUMMARY_SYSTEM_PROMPT)
    intent_user = staticmethod(build_intent_classification_user_prompt)
    intent_system = staticmethod(lambda: INTENT_CLASSIFIER_SYSTEM_PROMPT)
