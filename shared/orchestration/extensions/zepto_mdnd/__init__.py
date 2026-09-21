"""Zepto MDND extension: the ``mdnd_v1`` semantic-slots provider.

Wraps the four-fact state rules (:mod:`state`) and the LLM slot extractor
(:mod:`slots`) behind the engine's :class:`SemanticSlotsProvider` contract.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from shared.orchestration.extensions import register_semantic_provider
from shared.orchestration.extensions.zepto_mdnd import state


class MDNDSemanticSlots:
    key = "mdnd_v1"
    fields = state.FIELDS

    def llm_extraction_enabled(self, definition: dict) -> bool:
        return state.llm_extraction_enabled(definition)

    def canonical_slots(self, slots: dict) -> dict:
        return state.canonical_slots(slots)

    def pending_fields(self, slots: dict) -> tuple[str, ...]:
        return tuple(key for key, legacy in state.FIELDS.items()
                     if not str(slots.get(legacy) or "").strip())

    def merge_extraction(self, slots: dict, result: dict, audit: list, node: str | None) -> set[str]:
        return state.merge_extraction(slots, result, audit, node)

    def semantic_node(self, node: dict, decided: set[str] | None = None) -> dict:
        return state.semantic_node(node, decided)

    def summary_fallback(self, slots: dict, language: str) -> str:
        return state.summary_fallback(slots, language)

    def owns_variable(self, variable: str) -> bool:
        return variable in state.FIELDS.values()

    def is_summary_hub(self, node: dict) -> bool:
        return state.is_summary_hub(node)

    def is_narrative_ask(self, node: dict, variable: str) -> bool:
        return state.is_narrative_ask(node, variable)

    async def extract(self, llm: Any, *, text: str, slots: dict, pending_question: str,
                      pending_variable: str, joint_variables: list[str], history: Any) -> dict:
        """Run the LLM four-fact extractor for this turn (as a plain dict for
        the checkpointed state). Only the facts the pending ask collects are
        'pending' — the handover recipient is never guessed from a yes/no ask."""
        from shared.orchestration.extensions.zepto_mdnd.slots import extract_mdnd_slots

        asked = {pending_variable, *joint_variables}
        pending_fields = tuple(key for key, legacy in state.FIELDS.items()
                               if key != "delivery_handoff" and legacy in asked)
        extraction = await extract_mdnd_slots(
            llm, text=text, slots=slots, pending_question=pending_question,
            pending_variable=pending_variable, history=history, pending_fields=pending_fields,
        )
        return asdict(extraction)


PROVIDER = MDNDSemanticSlots()
register_semantic_provider(PROVIDER)
