"""Real test calls: building a FreeSWITCH ``originate`` for the Studio.

The Studio's "Real call" option dials a tester's mobile number through the
dialer's FreeSWITCH (event socket ``bgapi originate``) and hands the answered
leg to the voice bot exactly the way a campaign call would arrive: the
dialplan posts the signed telephony webhook with ``To``/``botId`` = the
``voicebotID`` channel variable and the ``vb_*`` channel variables as the
per-call ``variables`` object (see ``free_sweetch/voicebot.lua``).

This module is pure: it validates and shapes the inputs and renders the
command string. Nothing here talks to FreeSWITCH; :class:`ESLClient` in
``voice_runtime.freeswitch`` does, and the backend route owns the policy
(auth, bot gates, audit). Kept in ``shared`` so the backend never imports
the voice runtime just to build a command, and so the worker can reuse it.

Security notes
- Every channel-variable name is shape-checked and every value is wrapped in
  single quotes (the one quoting FreeSWITCH honours both in the api command
  line and inside the ``{...}`` variable block). Control characters are
  rejected; a single quote is replaced by the typographic apostrophe so a
  customer name like O'Brien can never terminate the quoted value.
- The dialed number is normalized to digits (optional leading ``+``) and
  must be 8–15 digits; nothing else ever reaches the dial string.
- The dial string and application come from configuration (or the Studio's
  advanced override) and are validated as a single token / ``&app(args)``
  form — no whitespace, no newlines, no stray braces.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

# Channel variables the dialer team (Ashik, 2026-11) declared mandatory on
# every originate. ``customerNumber`` is filled from the dialed number and
# ``tenantId`` from the bot's tenant; the rest come from configuration
# (``FREESWITCH_ORIGINATE_DEFAULTS``) and may be edited per test call.
MANDATORY_ESL_PARAMS: tuple[str, ...] = (
    "sip_route_uri",
    "ignore_early_media",
    "domain_name",
    "sip_h_route-header",
    "origination_caller_id_number",
    "customerNumber",
    "recording",
    "leadId",
    "campaignId",
    "basketId",
    "operatorId",
    "operatorPrefix",
    "cli",
    "kamailioHost",
    "freeswitchHost",
    "callType",
    "callMode",
    "tenantId",
    "dialTimeout",
    "recordDualStereo",
    "amdEnabled",
    "leadtype",
    "serverType",
)

# Routes the answered leg to the bot (the dialplan forwards it as the
# webhook's ``To``/``botId``). Always set from the Studio's bot, never from
# user input.
BOT_ID_PARAM = "voicebotID"
# Lets the platform find the call afterwards: FreeSWITCH adopts it as the
# channel uuid, the dialplan sends it as the webhook ``callId``, and the
# conversation row stores it in ``call_id``.
ORIGINATION_UUID_PARAM = "origination_uuid"
NUMBER_PARAM = "customerNumber"
TENANT_PARAM = "tenantId"

# Mandatory + the two platform-owned variables: never prefixed, never
# treated as customer data.
SYSTEM_PARAMS: frozenset[str] = frozenset(
    MANDATORY_ESL_PARAMS + (BOT_ID_PARAM, ORIGINATION_UUID_PARAM)
)

# Customer data travels as ``vb_<key>`` channel variables; the dialplan
# strips the prefix and posts the rest as the webhook ``variables``.
CUSTOMER_VARIABLE_PREFIX = "vb_"

# Shape limits. The webhook keeps variable keys matching
# ``[A-Za-z0-9_.-]{1,40}`` and values up to 200 characters
# (shared.telephony_webhooks), so the mapped ``vb_`` names and values are
# bounded to arrive intact. ESL parameter names allow the hyphenated SIP
# header form (``sip_h_route-header``).
_ESL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_CUSTOMER_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,36}$")  # + "vb_" ≤ 40
_PHONE_DIGITS = re.compile(r"^\+?[0-9]{8,15}$")
_DIAL_TOKEN = re.compile(r"^[A-Za-z0-9_.:@/+\-\[\]=,;!~*$^]+$")
_APPLICATION = re.compile(r"^&[A-Za-z0-9_]+\([^\s{}()'\"]*\)$")
# Unquoted values may contain only these; anything else is single-quoted.
_PLAIN_VALUE = re.compile(r"^[A-Za-z0-9_.+@:/\-]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_ESL_VALUE_CHARS = 500
MAX_CUSTOMER_VALUE_CHARS = 200
MAX_CUSTOMER_VARIABLES = 50


class OriginateValidationError(ValueError):
    """One or more inputs cannot be sent safely; ``errors`` lists them."""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        super().__init__("; ".join(e["message"] for e in errors))


def normalize_phone_number(raw: object) -> str:
    """Digits with an optional leading ``+``; spaces, dashes and brackets
    are dropped. Anything else (letters, ``;``, SIP syntax) is rejected."""
    text = str(raw or "").strip()
    text = re.sub(r"[\s().-]", "", text)
    if not _PHONE_DIGITS.match(text):
        raise OriginateValidationError([{
            "field": "phoneNumber",
            "message": "Enter a phone number with 8 to 15 digits "
                       "(optional leading +, spaces and dashes allowed).",
        }])
    return text


def mask_phone_number(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) <= 4:
        return "X" * len(digits)
    return "X" * (len(digits) - 4) + digits[-4:]


def customer_variable_name(key: str) -> str:
    """``customer_name`` → ``vb_customer_name``; an existing ``vb_`` key and
    a system/mandatory parameter name are returned unchanged."""
    key = str(key or "").strip()
    if key in SYSTEM_PARAMS or key.startswith(CUSTOMER_VARIABLE_PREFIX):
        return key
    return CUSTOMER_VARIABLE_PREFIX + key


@dataclass
class CustomerMapping:
    variables: dict[str, str] = field(default_factory=dict)
    # ``[{"key", "variable", "reason"}]`` — entries that did not become a
    # channel variable, so the Studio can show why.
    skipped: list[dict] = field(default_factory=list)


def map_customer_variables(payload: dict | None) -> CustomerMapping:
    """Shape a customer-data JSON object into ``vb_*`` channel variables.

    Scalars are stringified exactly (``True`` → ``"true"`` to match what the
    webhook later keeps; numbers keep their JSON spelling). Nested objects
    and arrays are skipped — channel variables are flat strings and the
    webhook drops non-scalars anyway. Keys that already carry the prefix are
    kept as they are; keys that name a mandatory/system parameter are NOT
    prefixed and are left for the ESL parameter table instead (returned in
    ``skipped`` with reason ``system``).
    """
    result = CustomerMapping()
    if not isinstance(payload, dict):
        return result
    for raw_key, value in payload.items():
        key = str(raw_key).strip()
        if not key:
            continue
        if key in SYSTEM_PARAMS:
            result.skipped.append({"key": key, "variable": key, "reason": "system"})
            continue
        name = customer_variable_name(key)
        bare = name[len(CUSTOMER_VARIABLE_PREFIX):]
        if not _CUSTOMER_KEY.match(bare) or not _ESL_NAME.match(name):
            result.skipped.append({"key": key, "variable": name, "reason": "invalid_key"})
            continue
        if value is None:
            result.skipped.append({"key": key, "variable": name, "reason": "null"})
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            text = json.dumps(value)
        elif isinstance(value, str):
            text = value
        else:
            result.skipped.append({"key": key, "variable": name, "reason": "nested"})
            continue
        if len(result.variables) >= MAX_CUSTOMER_VARIABLES:
            result.skipped.append({"key": key, "variable": name, "reason": "limit"})
            continue
        result.variables[name] = text[:MAX_CUSTOMER_VALUE_CHARS]
    return result


def _clean_value(name: str, value: object, *, limit: int) -> str:
    text = "" if value is None else str(value)
    if isinstance(value, bool):
        text = "true" if value else "false"
    if _CONTROL.search(text):
        raise OriginateValidationError([{
            "field": name,
            "message": "Line breaks and control characters are not allowed.",
        }])
    if len(text) > limit:
        raise OriginateValidationError([{
            "field": name, "message": f"Longer than {limit} characters.",
        }])
    # The only quoting FreeSWITCH understands is the single quote, with no
    # escape inside it. Swap for the typographic apostrophe: readable to the
    # bot, impossible to break out of the quoted value.
    return text.replace("'", "’")


def quote_esl_value(value: str) -> str:
    """Single-quote a value unless it is a plain token (digits, ids, URIs)."""
    if value and _PLAIN_VALUE.match(value):
        return value
    return "'" + value + "'"


def validate_variables(
    variables: dict, *, limit: int = MAX_ESL_VALUE_CHARS
) -> dict[str, str]:
    """Shape-check names, clean values; returns a new ordered dict."""
    errors: list[dict] = []
    out: dict[str, str] = {}
    for raw_name, value in (variables or {}).items():
        name = str(raw_name or "").strip()
        if not _ESL_NAME.match(name):
            errors.append({
                "field": name or "(blank)",
                "message": "Channel variable names use letters, digits, '_', '.' "
                           "or '-' and start with a letter (max 64).",
            })
            continue
        try:
            out[name] = _clean_value(name, value, limit=limit)
        except OriginateValidationError as exc:
            errors.extend(exc.errors)
    if errors:
        raise OriginateValidationError(errors)
    return out


def format_variable_block(variables: dict[str, str]) -> str:
    """``{a=1,b='two words'}`` — the originate channel-variable prefix."""
    parts = [f"{name}={quote_esl_value(value)}" for name, value in variables.items()]
    return "{" + ",".join(parts) + "}"


def render_dial_string(template: str, variables: dict[str, str], number: str) -> str:
    """Fill ``{number}`` and ``{<variable>}`` placeholders in the dial
    template, then validate the result as one dial-string token."""
    template = str(template or "").strip()
    if not template:
        raise OriginateValidationError([{
            "field": "dialTemplate", "message": "A dial string template is required.",
        }])
    values = dict(variables)
    values["number"] = number

    def _fill(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise OriginateValidationError([{
                "field": "dialTemplate",
                "message": f"Placeholder {{{key}}} has no value.",
            }])
        return values[key]

    rendered = re.sub(r"\{([A-Za-z0-9_.-]+)\}", _fill, template)
    if not _DIAL_TOKEN.match(rendered):
        raise OriginateValidationError([{
            "field": "dialTemplate",
            "message": "The dial string may not contain spaces, quotes or braces.",
        }])
    return rendered


def validate_application(application: str) -> str:
    """``&lua(voicebot.lua)`` / ``&park()`` — the originate's B-leg app."""
    app = str(application or "").strip()
    if not _APPLICATION.match(app):
        raise OriginateValidationError([{
            "field": "application",
            "message": "The application must look like &lua(voicebot.lua) or &park().",
        }])
    return app


@dataclass
class OriginatePlan:
    phone_number: str
    variables: dict[str, str]
    dial_string: str
    application: str
    origination_uuid: str
    customer_variables: dict[str, str]
    skipped_customer_fields: list[dict]
    missing_mandatory: list[str]

    @property
    def command(self) -> str:
        """The ``originate`` command tail (what follows ``bgapi ``)."""
        return (
            "originate " + format_variable_block(self.variables)
            + self.dial_string + " " + self.application
        )

    @property
    def phone_masked(self) -> str:
        return mask_phone_number(self.phone_number)


def build_originate_plan(
    *,
    phone_number: object,
    bot_id: str,
    tenant_id: str,
    base_params: dict,
    param_overrides: dict | None,
    customer_data: dict | None,
    dial_template: str,
    application: str,
    origination_uuid: str | None = None,
) -> OriginatePlan:
    """Compose the effective channel variables and command for one call.

    Precedence (lowest → highest): ``base_params`` (configuration defaults,
    bot-derived values) → ``param_overrides`` (what the tester edited) →
    customer ``vb_*`` variables (from the bot's test JSON plus the tester's
    edits, already merged by the caller) → the platform-owned values
    (``customerNumber``, ``tenantId``, ``voicebotID``, ``origination_uuid``)
    which cannot be overridden: the call always targets the selected bot
    and tenant, and the call id stays traceable.
    """
    number = normalize_phone_number(phone_number)
    params: dict[str, object] = {}
    for name in MANDATORY_ESL_PARAMS:
        params[name] = ""
    params.update({k: v for k, v in (base_params or {}).items()})
    params.update({k: v for k, v in (param_overrides or {}).items()})
    # Everything the tester typed gets validated, including names.
    variables = validate_variables(params)

    mapping = map_customer_variables(customer_data)
    customer_variables = validate_variables(
        mapping.variables, limit=MAX_CUSTOMER_VALUE_CHARS
    )
    variables.update(customer_variables)

    call_uuid = str(origination_uuid or uuid.uuid4())
    if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", call_uuid):
        raise OriginateValidationError([{
            "field": ORIGINATION_UUID_PARAM, "message": "Must be a UUID.",
        }])
    variables[NUMBER_PARAM] = number
    variables[TENANT_PARAM] = tenant_id
    variables[BOT_ID_PARAM] = bot_id
    variables[ORIGINATION_UUID_PARAM] = call_uuid

    missing = [name for name in MANDATORY_ESL_PARAMS if not variables.get(name, "").strip()]
    dial = render_dial_string(dial_template, variables, number)
    app = validate_application(application)
    return OriginatePlan(
        phone_number=number,
        variables=variables,
        dial_string=dial,
        application=app,
        origination_uuid=call_uuid,
        customer_variables=customer_variables,
        skipped_customer_fields=mapping.skipped,
        missing_mandatory=missing,
    )


def parse_originate_result(body: str) -> tuple[bool, str]:
    """``+OK <uuid>`` / ``-ERR <cause>`` → (ok, detail)."""
    text = (body or "").strip()
    if text.startswith("+OK"):
        return True, text[3:].strip()
    if text.startswith("-ERR"):
        return False, text[4:].strip()
    return False, text[:200]
