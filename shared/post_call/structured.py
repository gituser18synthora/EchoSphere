"""Structured post-call summary fields, derived from configuration.

A bot's goal policy may declare ``summaryFields`` (see
:class:`shared.orchestration.goal_engine.SummaryFieldSpec`): a fixed
vocabulary of Yes/No, choice and text fields that every call's summary must
report — for example a delivery-dispute line's ``call_customer``,
``reach_customer_location``, ``hand_over_product``, ``hand_over_to`` and
``call_cx``.

Two sources feed a field, in strict precedence:

1. **Workflow slots** (authoritative). The final slot values of the guided
   flow already reflect every correction the caller made during the
   verification step, so a slot-derived value is never overridden.
2. **The post-call analyst** (LLM), only for a field whose slot never got a
   value and whose spec allows it — validated against the field's own
   vocabulary, never trusted raw.

Fields with neither source stay ``None`` ("not determined") — the key is
still present so downstream consumers see a stable shape.
"""

import re

from shared.orchestration.goal_engine import BotGoalPolicy, SummaryFieldSpec

YES = "Yes"
NO = "No"
_YES_TOKENS = frozenset({"yes", "yeah", "yep", "haan", "haa", "han", "ha", "ji",
                         "true", "y", "हाँ", "हां", "हा", "जी"})
_NO_TOKENS = frozenset({"no", "nope", "nahi", "nahin", "nahee", "false", "n",
                        "नहीं", "नही", "ना"})
_TOKEN_SPLIT = re.compile(r"[\s(),.;:!?\-]+")
_MAX_TEXT_CHARS = 120

SOURCE_WORKFLOW = "workflow"
SOURCE_ANALYSIS = "analysis"


def _norm(value) -> str:
    return " ".join(str(value if value is not None else "").split()).strip().lower()


def normalize_yes_no(value) -> str | None:
    """Map a slot canonical / free answer onto "Yes"/"No" (None when unclear).

    Slot canonicals in this platform read like ``"yes (called the customer)"``
    or ``"no (did not call)"`` — the leading token decides.
    """
    lowered = _norm(value)
    if not lowered:
        return None
    first = next((t for t in _TOKEN_SPLIT.split(lowered) if t), "")
    if first in _NO_TOKENS:
        return NO
    if first in _YES_TOKENS:
        return YES
    return None


def _match_option(spec: SummaryFieldSpec, value) -> str | None:
    lowered = _norm(value)
    if not lowered:
        return None
    for option in spec.options:
        if _norm(option) == lowered:
            return option
    return None


def derive_field(spec: SummaryFieldSpec, slots: dict | None) -> str | None:
    """The field's value from the workflow slots alone (None = not derivable)."""
    raw = (slots or {}).get(spec.source) if spec.source else None
    lowered = _norm(raw)
    if not lowered:
        return None
    if lowered in spec.values:
        mapped = spec.values[lowered]
        # An explicit empty mapping means "not applicable" (e.g. hand_over_to
        # when nothing was handed over) — deliberately None, not the raw slot.
        return _validate_value(spec, mapped) if mapped else None
    if spec.type == "yes_no":
        decided = normalize_yes_no(lowered)
        if decided is not None:
            return decided
    if "*" in spec.values:
        return _validate_value(spec, spec.values["*"]) or None
    if spec.type == "choice":
        return _match_option(spec, lowered)
    if spec.type == "text":
        return str(raw).strip()[:_MAX_TEXT_CHARS]
    return None


def _validate_value(spec: SummaryFieldSpec, value) -> str | None:
    """Clamp any proposed value onto the field's own vocabulary."""
    if value is None:
        return None
    if spec.type == "yes_no":
        return normalize_yes_no(value)
    if spec.type == "choice":
        return _match_option(spec, value)
    text = " ".join(str(value).split()).strip()
    return text[:_MAX_TEXT_CHARS] or None


def derive_structured_fields(
    policy: BotGoalPolicy | None, slots: dict | None
) -> dict[str, str | None]:
    """Every configured field from the workflow slots (None where unknown)."""
    if policy is None:
        return {}
    return {spec.name: derive_field(spec, slots) for spec in policy.summary_fields}


def merge_structured_fields(
    policy: BotGoalPolicy | None,
    slots: dict | None,
    proposed: dict | None,
) -> tuple[dict[str, str | None], dict[str, str]]:
    """Final fields + per-field provenance (``workflow`` | ``analysis``).

    Slot-derived values win outright; an analyst proposal fills only a field
    the slots left empty, and only after validation against the vocabulary.
    """
    if policy is None or not policy.summary_fields:
        return {}, {}
    proposed = proposed if isinstance(proposed, dict) else {}
    proposed_lower = {_norm(k): v for k, v in proposed.items()}
    fields: dict[str, str | None] = {}
    sources: dict[str, str] = {}
    for spec in policy.summary_fields:
        value = derive_field(spec, slots)
        if value is not None:
            fields[spec.name] = value
            sources[spec.name] = SOURCE_WORKFLOW
            continue
        candidate = None
        if spec.allow_llm:
            candidate = _validate_value(spec, proposed_lower.get(_norm(spec.name)))
        fields[spec.name] = candidate
        if candidate is not None:
            sources[spec.name] = SOURCE_ANALYSIS
    return fields, sources


def summary_fields_prompt_block(policy: BotGoalPolicy | None) -> str:
    """Analyst instructions for the configured fields ('' when none)."""
    if policy is None or not policy.summary_fields:
        return ""
    lines = [
        "# Structured summary fields (REQUIRED — emit as \"structured_fields\")",
        "Report each field below with exactly one of its allowed values, or "
        "null when the transcript does not settle it. Never guess.",
    ]
    for spec in policy.summary_fields:
        if spec.type == "yes_no":
            allowed = '"Yes" | "No" | null'
        elif spec.type == "choice":
            allowed = " | ".join(f'"{o}"' for o in spec.options) + " | null"
        else:
            allowed = "short text | null"
        meaning = spec.description or spec.label or spec.name.replace("_", " ")
        lines.append(f'- "{spec.name}": {allowed} — {meaning}')
    return "\n".join(lines)


__all__ = [
    "SOURCE_ANALYSIS",
    "SOURCE_WORKFLOW",
    "derive_field",
    "derive_structured_fields",
    "merge_structured_fields",
    "normalize_yes_no",
    "summary_fields_prompt_block",
]
