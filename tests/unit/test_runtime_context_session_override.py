"""Dialer variables vs the User Details API — per-field ``sessionOverride``.

The Jeeves Airtel bot (bot_44a50e8c4146) receives the customer's name,
locality and an opening context line on the signed telephony webhook
(``variables``) while its schema still provides a fixture / API payload.
Flagged fields take the dialer's value; fields the dialer omits or sends
blank fall back to the payload; the API is skipped only when the dialer
covered every declared field; nothing changes for schemas without the flag.
"""

import pytest

import shared.runtime_context as rc
from shared.runtime_context import (
    build_runtime_context,
    canonical_variable_key,
    load_runtime_context,
    session_override_values,
    validate_field_definitions,
)

JEEVES_FIELDS = [
    {"key": "customer_name", "type": "string", "label": "Customer name",
     "sessionOverride": True},
    {"key": "locality", "type": "string", "label": "Locality / masked address",
     "sessionOverride": True},
    {"key": "connection_status", "type": "string",
     "label": "Connection status (DISCONNECTED …)", "sessionOverride": True},
    {"key": "context_line", "type": "string", "label": "Context line",
     "sessionOverride": True},
]
JEEVES_FIXTURE = {
    "locality": "Sector 62, Noida",
    "customer_name": "Rahul",
    "connection_status": "DISCONNECTED",
}
# The FreeSWITCH webhook payload from the requirement, after _sanitize_variables.
DIALER_VARIABLES = {
    "customer_name": "Raju",
    "contextLine": "your treatment/category opening line",
    "locality": "Gurugram",
}


def _ctx(fields=JEEVES_FIELDS, **kwargs):
    return build_runtime_context(
        tenant_id="tn-1", bot_id="bot-1", field_definitions=fields, **kwargs,
    )


class TestCanonicalKey:
    def test_spellings_collapse(self):
        for spelling in ("customer_name", "customerName", "CustomerName",
                         "customer-name", "Customer Name", " customer_name "):
            assert canonical_variable_key(spelling) == "customer_name"
        assert canonical_variable_key("contextLine") == "context_line"
        assert canonical_variable_key("DISCONNECTED") == "disconnected"
        assert canonical_variable_key("visitDay2") == "visit_day2"


class TestFieldFlagValidation:
    def test_flag_must_be_boolean_and_scalar(self):
        errors = validate_field_definitions([
            {"key": "ok", "type": "string", "sessionOverride": True},
            {"key": "bad_flag", "type": "string", "sessionOverride": "yes"},
            {"key": "bad_type", "type": "array", "sessionOverride": True},
        ])
        fields = {e["field"] for e in errors}
        assert fields == {"fields[1].sessionOverride", "fields[2].sessionOverride"}

    def test_flag_absent_is_fine(self):
        assert validate_field_definitions([{"key": "ok", "type": "string"}]) == []


class TestOverrideResolution:
    def test_jeeves_payload_maps_onto_declared_keys(self):
        overrides, consumed = session_override_values(JEEVES_FIELDS, DIALER_VARIABLES)
        assert overrides == {
            "customer_name": "Raju",
            "locality": "Gurugram",
            "context_line": "your treatment/category opening line",
        }
        assert consumed == {"customer_name", "contextLine", "locality"}

    def test_blank_and_missing_are_not_overrides(self):
        overrides, consumed = session_override_values(
            JEEVES_FIELDS, {"customer_name": "   ", "locality": "Gurugram"},
        )
        assert overrides == {"locality": "Gurugram"}
        assert consumed == {"customer_name", "locality"}  # blank is consumed, not shown

    def test_unflagged_fields_and_unknown_keys_untouched(self):
        fields = [{"key": "customer_name", "type": "string"}]
        assert session_override_values(fields, {"customer_name": "Raju", "campaign": "oct"}) == ({}, set())

    def test_declared_spelling_beats_alias_when_both_sent(self):
        overrides, _ = session_override_values(
            JEEVES_FIELDS, {"customerName": "Alias", "customer_name": "Exact"},
        )
        assert overrides["customer_name"] == "Exact"
        overrides, _ = session_override_values(
            JEEVES_FIELDS, {"customer_name": "Exact", "customerName": "Alias"},
        )
        assert overrides["customer_name"] == "Exact"

    def test_typed_fields_parse_strictly(self):
        fields = [
            {"key": "amount", "type": "number", "sessionOverride": True},
            {"key": "days", "type": "integer", "sessionOverride": True},
            {"key": "verified", "type": "boolean", "sessionOverride": True},
            {"key": "due", "type": "date", "sessionOverride": True},
        ]
        overrides, _ = session_override_values(fields, {
            "amount": "12500.50", "days": "12", "verified": "yes", "due": "2026-10-21",
        })
        assert overrides == {"amount": 12500.5, "days": 12, "verified": True,
                             "due": "2026-10-21"}
        overrides, consumed = session_override_values(fields, {
            "amount": "twelve", "days": "12.5", "verified": "maybe", "due": "21/10/2026",
        })
        assert overrides == {}            # none parse → the payload fills them
        assert consumed == {"amount", "days", "verified", "due"}


class TestBuildPrecedence:
    def test_flagged_session_value_wins_over_payload(self):
        ctx = _ctx(session_variables=DIALER_VARIABLES, payload=JEEVES_FIXTURE,
                   payload_source="test")
        assert ctx.get("customer_name") == "Raju"
        assert ctx.values["customer_name"].source == "session"
        assert ctx.get("locality") == "Gurugram"
        assert ctx.values["locality"].source == "session"
        # Not supplied by the dialer → the configured source still fills it.
        assert ctx.get("connection_status") == "DISCONNECTED"
        assert ctx.values["connection_status"].source == "test"
        # camelCase variable landed on the declared snake_case field, once.
        assert ctx.get("context_line") == "your treatment/category opening line"
        assert ctx.values["context_line"].source == "session"
        assert "contextLine" not in ctx.values

    def test_same_against_api_payload(self):
        ctx = _ctx(session_variables=DIALER_VARIABLES, payload=JEEVES_FIXTURE,
                   payload_source="api")
        assert ctx.get("customer_name") == "Raju"
        assert ctx.get("connection_status") == "DISCONNECTED"
        assert ctx.values["connection_status"].source == "api"

    def test_greeting_and_workflow_see_the_dialer_values(self):
        from shared.orchestration.placeholders import resolve_placeholders

        ctx = _ctx(session_variables=DIALER_VARIABLES, payload=JEEVES_FIXTURE,
                   payload_source="test")
        # The brain seeds placeholder resolution with the raw dialer variables
        # and then the context's prompt values on top (voice_runtime/brain.py).
        values = {**DIALER_VARIABLES, **ctx.prompt_values()}
        greeting = resolve_placeholders("हेलो, क्या मेरी बात {customer_name} जी से हो रही है?", values)
        assert "Raju" in greeting and "Rahul" not in greeting
        assert resolve_placeholders("connection at [locality]", values) == "connection at Gurugram"
        # Both spellings resolve: the declared field and the dialer's own key.
        assert resolve_placeholders("{context_line} / {contextLine}", values) == (
            "your treatment/category opening line / your treatment/category opening line"
        )
        # Workflow prefillFromContext reads exact keys from prompt_values().
        context_values = ctx.prompt_values()
        assert context_values["connection_status"] == "DISCONNECTED"
        assert context_values["locality"] == "Gurugram"
        assert context_values["context_line"] == "your treatment/category opening line"
        section = ctx.prompt_section()
        assert "Customer name: Raju" in section
        assert "Locality / masked address: Gurugram" in section
        assert "Rahul" not in section and "Noida" not in section

    def test_blank_dialer_value_falls_back_to_payload(self):
        ctx = _ctx(session_variables={"customer_name": "", "locality": "Gurugram"},
                   payload=JEEVES_FIXTURE, payload_source="api")
        assert ctx.get("customer_name") == "Rahul"
        assert ctx.values["customer_name"].source == "api"
        assert ctx.get("locality") == "Gurugram"

    def test_blank_dialer_value_without_payload_stays_unknown(self):
        ctx = _ctx(session_variables={"customer_name": " "}, payload=None,
                   payload_source="api")
        assert "customer_name" not in ctx.values
        assert "customer_name" in ctx.declared_missing()
        assert "No caller-specific values" in ctx.prompt_section()   # stated, never ""

    def test_unflagged_schema_keeps_payload_precedence(self):
        fields = [{"key": k["key"], "type": "string"} for k in JEEVES_FIELDS]
        ctx = _ctx(fields=fields, session_variables=DIALER_VARIABLES,
                   payload=JEEVES_FIXTURE, payload_source="api")
        assert ctx.get("customer_name") == "Rahul"      # api > session, as before
        assert ctx.values["customer_name"].source == "api"
        assert ctx.get("contextLine") == "your treatment/category opening line"
        assert ctx.values["contextLine"].source == "session"
        assert "context_line" not in ctx.values

    def test_undeclared_variables_keep_their_place(self):
        ctx = _ctx(session_variables={**DIALER_VARIABLES, "campaign": "oct-wave-2"},
                   payload=JEEVES_FIXTURE, payload_source="test")
        assert ctx.values["campaign"].source == "session"
        assert ctx.get("campaign") == "oct-wave-2"

    def test_record_and_workflow_interplay(self):
        ctx = _ctx(session_variables=DIALER_VARIABLES, payload=JEEVES_FIXTURE,
                   payload_source="record")
        assert ctx.get("customer_name") == "Raju"       # flagged: dialer beats the record
        ctx.set_workflow_value("customer_name", "Raju Kumar")
        assert ctx.get("customer_name") == "Raju Kumar"  # workflow still wins over all
        assert ctx.values["customer_name"].source == "workflow"

    def test_sensitive_override_is_masked(self):
        fields = [{"key": "account_ref", "type": "string", "sensitive": True,
                   "sessionOverride": True}]
        ctx = _ctx(fields=fields, session_variables={"accountRef": "ACC123456789"},
                   payload={"account_ref": "ACC000000000"}, payload_source="api")
        assert ctx.values["account_ref"].source == "session"
        assert ctx.get("account_ref") != "ACC123456789"
        assert str(ctx.get("account_ref")).endswith("6789")


def _schema(mode: str, *, fields=JEEVES_FIELDS, test_payload=None, api=None):
    return {
        "id": "rcs_test", "source_mode": mode, "fields": fields,
        "allow_additional": True, "test_payload": test_payload,
        "missing_value_policy": None, "domain_policy": "generic",
        "response_path": None, "api": api,
    }


_API = {"id": "api_1", "name": "User Details", "method": "GET",
        "url": "https://crm.example/users/{{caller_phone}}", "headers": {},
        "query_params": {}, "body_template": None, "timeout_ms": 2000,
        "secret_ref": None, "auth_type": "none", "sensitive_masks": []}


@pytest.fixture
def loader(monkeypatch):
    """load_runtime_context with the DB and HTTP edges stubbed."""
    state = {"schema": None, "record": None, "api_payload": JEEVES_FIXTURE,
             "api_calls": 0, "api_error": None}
    monkeypatch.setattr(rc, "_load_schema_sync", lambda tenant, bot: state["schema"])
    monkeypatch.setattr(rc, "_load_record_sync",
                        lambda tenant, bot, record_id=None, phone=None: state["record"])

    async def fake_fetch(api, variables):
        state["api_calls"] += 1
        state["last_variables"] = variables
        if state["api_error"]:
            return None, state["api_error"]
        return state["api_payload"], None

    monkeypatch.setattr(rc, "_fetch_api_payload", fake_fetch)
    return state


class TestLoader:
    async def test_manual_mode_jeeves_today(self, loader):
        loader["schema"] = _schema("manual", test_payload=JEEVES_FIXTURE)
        ctx = await load_runtime_context("tn-1", "bot-1", phone="7738184155",
                                         session_variables=DIALER_VARIABLES)
        assert ctx.get("customer_name") == "Raju"
        assert ctx.get("locality") == "Gurugram"
        assert ctx.get("connection_status") == "DISCONNECTED"   # fixture fills the gap
        assert ctx.get("context_line") == "your treatment/category opening line"
        assert ctx.api_skipped is False and loader["api_calls"] == 0

    async def test_api_mode_partial_coverage_calls_api_for_the_rest(self, loader):
        loader["schema"] = _schema("api", api=_API)
        ctx = await load_runtime_context("tn-1", "bot-1", phone="7738184155",
                                         session_variables=DIALER_VARIABLES)
        assert loader["api_calls"] == 1
        assert ctx.api_skipped is False
        assert ctx.get("customer_name") == "Raju"
        assert ctx.values["customer_name"].source == "session"
        assert ctx.get("connection_status") == "DISCONNECTED"
        assert ctx.values["connection_status"].source == "api"
        # The API still receives the dialer's variables as templates.
        assert loader["last_variables"]["caller_phone"] == "7738184155"
        assert loader["last_variables"]["locality"] == "Gurugram"

    async def test_api_mode_full_coverage_skips_the_api(self, loader):
        loader["schema"] = _schema("api", api=_API)
        ctx = await load_runtime_context(
            "tn-1", "bot-1", phone="7738184155",
            session_variables={**DIALER_VARIABLES, "connection_status": "DISCONNECTED"},
        )
        assert loader["api_calls"] == 0
        assert ctx.api_skipped is True
        assert ctx.load_error is None
        assert ctx.source_mode == "api"
        assert {k: v.source for k, v in ctx.values.items()
                if k in ("customer_name", "locality", "connection_status", "context_line")} == {
            "customer_name": "session", "locality": "session",
            "connection_status": "session", "context_line": "session",
        }
        assert ctx.missing_required() == [] and ctx.declared_missing() == []

    async def test_blank_variable_keeps_the_api_call(self, loader):
        loader["schema"] = _schema("api", api=_API)
        ctx = await load_runtime_context(
            "tn-1", "bot-1", phone="7738184155",
            session_variables={"customer_name": "", "locality": "Gurugram",
                               "connection_status": "DISCONNECTED",
                               "contextLine": "x"},
        )
        assert loader["api_calls"] == 1
        assert ctx.get("customer_name") == "Rahul"          # API fills the blank
        assert ctx.get("locality") == "Gurugram"

    async def test_api_failure_keeps_dialer_values(self, loader):
        loader["schema"] = _schema("api", api=_API)
        loader["api_error"] = "HTTP 503"
        ctx = await load_runtime_context("tn-1", "bot-1", phone="7738184155",
                                         session_variables=DIALER_VARIABLES)
        assert ctx.load_error == "HTTP 503"
        assert ctx.get("customer_name") == "Raju"
        assert ctx.get("connection_status") is None
        assert "connection_status" in ctx.declared_missing()

    async def test_unflagged_api_schema_is_unchanged(self, loader):
        fields = [{"key": k["key"], "type": "string"} for k in JEEVES_FIELDS]
        loader["schema"] = _schema("api", fields=fields, api=_API)
        ctx = await load_runtime_context(
            "tn-1", "bot-1", phone="7738184155",
            session_variables={**DIALER_VARIABLES, "connection_status": "ACTIVE"},
        )
        assert loader["api_calls"] == 1
        assert ctx.api_skipped is False
        assert ctx.get("customer_name") == "Rahul"
        assert ctx.get("connection_status") == "DISCONNECTED"   # api > session, as before

    async def test_open_schema_never_skips_the_api(self, loader):
        loader["schema"] = _schema("api", fields=[], api=_API)
        ctx = await load_runtime_context("tn-1", "bot-1", phone="7738184155",
                                         session_variables=DIALER_VARIABLES)
        assert loader["api_calls"] == 1 and ctx.api_skipped is False

    async def test_no_variables_means_api_as_before(self, loader):
        loader["schema"] = _schema("api", api=_API)
        ctx = await load_runtime_context("tn-1", "bot-1", phone="7738184155",
                                         session_variables={})
        assert loader["api_calls"] == 1
        assert ctx.get("customer_name") == "Rahul"
        assert ctx.values["customer_name"].source == "api"

    async def test_staged_record_fills_only_what_the_dialer_omits(self, loader):
        loader["schema"] = _schema("api", api=_API)
        loader["record"] = {"id": "rcr_1", "data": {**JEEVES_FIXTURE, "customer_name": "Rahul R"},
                            "call_state": {}}
        ctx = await load_runtime_context("tn-1", "bot-1", phone="7738184155",
                                         session_variables=DIALER_VARIABLES)
        assert loader["api_calls"] == 0                    # record short-circuits the API (as before)
        assert ctx.record_id == "rcr_1"
        assert ctx.get("customer_name") == "Raju"          # flagged: dialer beats the record
        assert ctx.get("connection_status") == "DISCONNECTED"
        assert ctx.values["connection_status"].source == "record"
