"""Extension registry — business-specific capabilities the core engine
executes by NAME, never by import.

Two kinds of extension exist today:

* **graph builders**: hand-built LangGraph flows the runtime falls back to
  when a bot routes to a workflow name with no saved definition
  (``payment_collection`` for mPokket, the appointment reference flow).
* **semantic-slot providers**: per-definition slot semantics a workflow
  opts into with ``semanticSlots: "<provider key>"`` on any node (the Zepto
  MDND four-fact collector is ``mdnd_v1``). The engine calls the provider's
  hooks (merge an LLM extraction, shield decided slots from keyword captures,
  render a grounded summary, name its summary hub / narrative ask); the
  provider owns every tenant literal.

Built-in extensions are listed in :mod:`manifest` and imported lazily on the
first registry read, so importing this module has no side effects.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

GraphBuilder = Callable[[Any], Any]


@runtime_checkable
class SemanticSlotsProvider(Protocol):
    """Contract for a definition-level slot-semantics extension."""

    key: str
    # canonical fact name → legacy slot variable it is stored under
    fields: Mapping[str, str]

    def llm_extraction_enabled(self, definition: dict, language: str = "") -> bool: ...
    def canonical_slots(self, slots: dict) -> dict: ...
    def pending_fields(self, slots: dict) -> tuple[str, ...]: ...
    def merge_extraction(self, slots: dict, result: dict, audit: list, node: str | None) -> set[str]: ...
    def semantic_node(self, node: dict, decided: set[str] | None = None) -> dict: ...
    def summary_fallback(self, slots: dict, language: str) -> str: ...
    def owns_variable(self, variable: str) -> bool: ...
    def is_summary_hub(self, node: dict) -> bool: ...
    def is_narrative_ask(self, node: dict, variable: str) -> bool: ...
    async def extract(self, llm: Any, *, text: str, slots: dict, pending_question: str,
                      pending_variable: str, joint_variables: list[str], history: Any,
                      language: str = "") -> dict: ...


_graph_builders: dict[str, GraphBuilder] = {}
_semantic_providers: dict[str, SemanticSlotsProvider] = {}
_loaded = False


def register_graph_builder(name: str, builder: GraphBuilder) -> None:
    _graph_builders[name] = builder


def register_semantic_provider(provider: SemanticSlotsProvider) -> None:
    _semantic_providers[provider.key] = provider


def load_builtin_extensions() -> None:
    """Import every module in the manifest once (idempotent, failure-tolerant:
    a broken extension never takes the engine down for other tenants)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from shared.orchestration.extensions.manifest import BUILTIN_EXTENSIONS

    for module in BUILTIN_EXTENSIONS:
        try:
            importlib.import_module(module)
        except Exception:  # noqa: BLE001 — isolate one extension's failure
            logger.exception("extension %s failed to load", module)


def graph_builders() -> Mapping[str, GraphBuilder]:
    load_builtin_extensions()
    return _graph_builders


def semantic_providers() -> Mapping[str, SemanticSlotsProvider]:
    load_builtin_extensions()
    return _semantic_providers


def semantic_slots_key(definition: dict | None) -> str | None:
    """The ``semanticSlots`` provider key a definition opts into, if any."""
    for node in (definition or {}).get("nodes") or []:
        config = node.get("config") if isinstance(node, dict) else None
        key = (config or {}).get("semanticSlots")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def semantic_provider_for(definition: dict | None) -> SemanticSlotsProvider | None:
    key = semantic_slots_key(definition)
    if key is None:
        return None
    provider = semantic_providers().get(key)
    if provider is None:
        logger.warning("definition opts into unknown semanticSlots provider %r", key)
    return provider


def reset_for_tests() -> None:
    global _loaded
    _graph_builders.clear()
    _semantic_providers.clear()
    _loaded = False
