"""Context Manager: Redis per-call sessions + MongoDB caller graphs + token budget.

Avoid importing ContextManager at package import time to prevent circular imports
with orchestrator.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from context_manager.context_manager import ContextManager

__all__ = ["ContextManager"]


def __getattr__(name: str):
    if name == "ContextManager":
        from context_manager.context_manager import ContextManager

        return ContextManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
