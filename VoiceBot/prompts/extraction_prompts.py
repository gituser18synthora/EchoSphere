"""End-of-call entity / graph extraction prompts.

All extraction targets are driven dynamically from the voicebot's
call_data_extraction config (standard_fields + custom_fields).
Nothing is hardcoded — if the config says a field is disabled, it is
not included in the prompt and will not be extracted.
"""

from __future__ import annotations

from typing import Any


ENTITY_EXTRACTION_SYSTEM_PROMPT = (
    "You are an entity extractor. "
    "Return only valid JSON. "
    "No markdown. No explanation."
)


def _build_standard_fields_section(standard_fields: dict[str, bool]) -> str:
    """Build the standard fields instruction block from config."""
    field_descriptions: dict[str, str] = {
        "customer_name":       "customer_name: full name of the caller (string or null)",
        "caller_phone_number": "caller_phone_number: caller's phone number (string)",
        "call_duration":       "call_duration_seconds: total call duration in seconds (number)",
        "call_intent_reason":  "call_intent_reason: primary reason the caller called (string or null)",
        "sentiment":           "sentiment: overall caller sentiment — positive/neutral/negative (string)",
        "language_detected":   "language_detected: language code detected e.g. en, hi, te (string)",
        "goal_outcome":        "goal_outcome: whether the caller's goal was achieved — achieved/partial/unresolved (string or null)",
    }
    enabled = [
        field_descriptions[key]
        for key, enabled_flag in standard_fields.items()
        if enabled_flag and key in field_descriptions
    ]
    if not enabled:
        return ""
    lines = ["STANDARD FIELDS to extract (enabled in this voicebot's configuration):"]
    for f in enabled:
        lines.append(f"  - {f}")
    return "\n".join(lines)


def _build_custom_fields_section(custom_fields: list[dict[str, Any]]) -> str:
    """Build the custom fields instruction block from config."""
    if not custom_fields:
        return ""
    lines = ["CUSTOM FIELDS to extract (defined by this voicebot's configuration):"]
    for f in custom_fields:
        name = f.get("field_name", "")
        dtype = f.get("data_type", "String")
        required = f.get("required", False)
        hint = (f.get("extraction_prompt") or "").strip()
        req_label = "required" if required else "optional"
        line = f"  - {name} ({dtype}, {req_label})"
        if hint:
            line += f": {hint}"
        lines.append(line)
    return "\n".join(lines)


def _build_standard_fields_json_schema(standard_fields: dict[str, bool]) -> str:
    """Build JSON output schema entries for enabled standard fields."""
    schema_map: dict[str, str] = {
        "customer_name":       '"customer_name": "full name of caller or null"',
        "caller_phone_number": '"caller_phone_number": "phone number string"',
        "call_duration":       '"call_duration_seconds": 0',
        "call_intent_reason":  '"call_intent_reason": "why they called or null"',
        "sentiment":           '"sentiment": "positive|neutral|negative"',
        "language_detected":   '"language_detected": "en|hi|te|..."',
        "goal_outcome":        '"goal_outcome": "achieved|partial|unresolved|null"',
    }
    return "\n".join(
        f"  {schema_map[key]}"
        for key, enabled_flag in standard_fields.items()
        if enabled_flag and key in schema_map
    )


def _build_custom_fields_json_schema(custom_fields: list[dict[str, Any]]) -> str:
    """Build JSON output schema entries for custom fields."""
    lines = []
    for f in custom_fields:
        name = f.get("field_name", "")
        dtype = f.get("data_type", "String").lower()
        required = f.get("required", False)
        snake = name.lower().replace(" ", "_")
        placeholder = "0" if dtype == "number" else '"value or null"'
        if not required:
            placeholder = f"{placeholder}  // optional"
        lines.append(f'  "{snake}": {placeholder}')
    return "\n".join(lines)


_ENTITY_EXTRACTION_TEMPLATE = """\
Extract structured facts and relationships from this call.

Call details:
- VoiceBot: {voicebot_name} ({business_name})
- Caller phone: {caller_phone}
- Duration: {duration_seconds:.0f} seconds ({duration_minutes:.1f} minutes)
- Date: {date}

Full conversation transcript:
{transcript}

{standard_fields_section}

{custom_fields_section}

GRAPH EXTRACTION RULES:
- Extract ONLY facts explicitly stated by the caller
- GRANULARITY: Every distinct piece of information is its own node.
  Do NOT merge related facts into one node.
  BAD:  preference_contact_time = "March 28th after 12 p.m."
  GOOD: fact_appointment_date   = "March 28, 2026"
        fact_appointment_time   = "after 12 p.m."
        action_appointment_booking = "booked appointment"
- APPOINTMENTS: If any appointment/meeting/callback is mentioned, you MUST
  create separate nodes for: the action (booked), the date, and the time.
- IDENTITY FACTS: Always create nodes for name, email, age, phone if mentioned.
- node_id must be deterministic: type_key
  examples: person_caller, fact_appointment_date, fact_age,
            preference_call_time, action_policy_enquiry
- confidence: 1.0=explicit  0.7=implied  0.5=uncertain
- Edge relations: has_preference has_fact requested discussed unresolved resolved scheduled
- CRITICAL: Every non-person node MUST have a corresponding edge to person_caller.
  Never create a node without an edge — no orphaned nodes.
- Add relation "scheduled" for appointment/booking edges.

Return ONLY valid JSON. No markdown. No explanation:
{{
  "caller_name": "name or null",
  "caller_email": "email or null",
{standard_fields_json}
{custom_fields_json}
  "nodes": [
    {{
      "node_id": "type_key",
      "type": "person|preference|fact|issue|action|topic",
      "key": "snake_case_identifier",
      "value": "actual value as string",
      "confidence": 0.0
    }}
  ],
  "edges": [
    {{
      "from_node": "node_id",
      "to_node": "node_id",
      "relation": "has_preference|has_fact|requested|discussed|unresolved|resolved"
    }}
  ],
  "summary": "One sentence summary of entire call"
}}
"""


def get_entity_extraction_system_prompt() -> str:
    return ENTITY_EXTRACTION_SYSTEM_PROMPT


def build_extraction_prompt(
    *,
    voicebot_name: str,
    business_name: str,
    caller_phone: str,
    duration_seconds: float,
    date: str,
    transcript: str,
    call_data_extraction: dict[str, Any] | None,
) -> str:
    """
    Build the entity extraction prompt dynamically from call_data_extraction config.

    Reads standard_fields (enabled/disabled flags) and custom_fields list
    directly from the voicebot config — nothing is hardcoded here.
    """
    extraction_cfg = call_data_extraction or {}
    standard_fields: dict[str, bool] = extraction_cfg.get("standard_fields", {})
    custom_fields: list[dict[str, Any]] = extraction_cfg.get("custom_fields", [])

    standard_section = _build_standard_fields_section(standard_fields)
    custom_section = _build_custom_fields_section(custom_fields)
    standard_json = _build_standard_fields_json_schema(standard_fields)
    custom_json = _build_custom_fields_json_schema(custom_fields)

    # Add trailing commas only when sections are non-empty
    if standard_json and not standard_json.endswith(","):
        standard_json = standard_json.rstrip() + ","
    if custom_json and not custom_json.endswith(","):
        custom_json = custom_json.rstrip() + ","

    return _ENTITY_EXTRACTION_TEMPLATE.format(
        voicebot_name=voicebot_name,
        business_name=business_name,
        caller_phone=caller_phone,
        duration_seconds=duration_seconds,
        duration_minutes=duration_seconds / 60,
        date=date,
        transcript=transcript,
        standard_fields_section=standard_section,
        custom_fields_section=custom_section,
        standard_fields_json=standard_json,
        custom_fields_json=custom_json,
    )


# ---------------------------------------------------------------------------
# Backwards-compat shim — keeps existing callers that pass old kwargs working.
# ---------------------------------------------------------------------------

def get_entity_extraction_prompt(**kwargs) -> str:
    """
    Legacy shim. Prefer build_extraction_prompt() for all new call sites.
    """
    duration_minutes = kwargs.get("duration_minutes", 0.0)
    return build_extraction_prompt(
        voicebot_name=kwargs.get("voicebot_name", ""),
        business_name=kwargs.get("business_name", ""),
        caller_phone=kwargs.get("caller_phone", ""),
        duration_seconds=duration_minutes * 60,
        date=kwargs.get("date", ""),
        transcript=kwargs.get("transcript", ""),
        call_data_extraction=None,
    )