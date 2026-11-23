"""Provenance for a conversational outcome the caller explicitly contested.

This state records a need for review. It never claims that an external action
was reversed, or grants transcript analysis permission to choose a new action.
"""

CORRECTION_STATE_KEY = "workflow_outcome_correction"
CORRECTION_SOURCE = "validated_caller_correction"


def contested_slots(correction: dict | None) -> frozenset[str]:
    if not isinstance(correction, dict) or (
        correction.get("status") != "contested"
        or correction.get("source") != CORRECTION_SOURCE
    ):
        return frozenset()
    names = correction.get("invalidated_slots")
    if not isinstance(names, list):
        return frozenset()
    return frozenset(name for name in names[:48] if isinstance(name, str) and name)


def correction_from_state(call_state: dict | None) -> dict | None:
    correction = (call_state or {}).get(CORRECTION_STATE_KEY)
    return correction if contested_slots(correction) else None
