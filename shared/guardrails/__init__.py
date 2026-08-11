"""Tenant-effective guardrail resolution and deterministic runtime enforcement.

``loader`` resolves a tenant's effective guardrails (mandatory platform rules
∪ assigned-profile rules) server-side, fail-closed. ``engine`` applies them
deterministically at the runtime hook points (user input, model output, tool
calls, persistence) and records tenant-scoped triggers without raw values.
"""

from shared.guardrails.engine import (
    GuardrailEngine,
    GuardrailHit,
    guardrail_reply,
    register_session_engine,
    release_session_engine,
    session_engine,
)
from shared.guardrails.loader import (
    MANDATORY_FLOOR,
    EffectiveGuardrails,
    GuardrailRule,
    load_effective_guardrails_sync,
    persist_triggers_sync,
)

__all__ = [
    "EffectiveGuardrails",
    "GuardrailEngine",
    "GuardrailHit",
    "GuardrailRule",
    "MANDATORY_FLOOR",
    "guardrail_reply",
    "load_effective_guardrails_sync",
    "persist_triggers_sync",
    "register_session_engine",
    "release_session_engine",
    "session_engine",
]
