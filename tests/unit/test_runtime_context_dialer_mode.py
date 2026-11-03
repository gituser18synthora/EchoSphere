"""Source mode ``dialer`` — the telephony webhook's variables are the live source.

The Haier rejection bot (bot_450a4f7b4832) is dialled with the customer's
name, product and rejection reason in the signed FreeSWITCH webhook's
``variables`` while its schema keeps a test fixture for Studio runs. In
``dialer`` mode a call that carries variables runs on them alone (no test
data, no phone-matched record; a field the dialer leaves out or blank is
unknown); a session without variables uses the fixture as ``manual`` does;
``manual`` and ``api`` schemas behave exactly as before.
"""

import pytest

import shared.runtime_context as rc
from shared.orchestration.placeholders import sanitize_spoken_text
from shared.runtime_context import (
    build_runtime_context,
    dialer_variables_present,
    load_runtime_context,
    session_override_values,
    validate_field_definitions,
)

# The Haier schema as haier/setup/06 declares it (no per-field flags).
HAIER_FIELDS = [
    {"key": "customer_name", "label": "Name", "type": "string"},
    {"key": "product_name", "label": "Product", "type": "string"},
    {"key": "rejection_reason", "label": "Rejection reason code", "type": "string"},
    {"key": "rejection_reason_label", "label": "Rejection reason (plain words)",
     "type": "string"},
    {"key": "l3_tagging", "label": "Request type (L3 tagging)", "type": "string"},
]
# The saved test data: the Studio's test customer, never a real caller.
HAIER_FIXTURE = {
    "l3_tagging": "installation",
    "product_name": "Split AC",
    "customer_name": "Rohan",
    "rejection_reason": "bill_not_available",
    "rejection_reason_label": "the bill was not available at the technician's visit",
}
# The FreeSWITCH webhook payload from the requirement, after _sanitize_variables.
WEBHOOK_VARIABLES = {
    "customer_name": "Arjun",
    "product_name": "Split AC",
    "rejection_reason": "customer_refused_visit",
    "rejection_reason_label": "the visit was declined when our technician called",
}
# Spoken lines from workflow v73 / greeting v7.
INTRO_LINE = ("This call is regarding the [l3_tagging] request for your "
              "[product_name], which was rejected by our field team.")
GREETING = "Am I speaking with {customer_name}?"


def _schema(mode: str, *, fields=HAIER_FIELDS, test_payload=HAIER_FIXTURE, api=None):
    return {
        "id": "rcs_haier", "source_mode": mode, "fields": fields,
        "allow_additional": True, "test_payload": test_payload,
        "missing_value_policy": None, "domain_policy": "generic",
        "response_path": None, "api": api,
    }


def _sources(ctx) -> set[str]:
    return {entry.source for entry in ctx.values.values()}


class TestFieldMapping:
    def test_every_declared_field_maps_without_a_flag(self):
        overrides, consumed = session_override_values(
            HAIER_FIELDS, WEBHOOK_VARIABLES, all_fields=True,
        )
        assert overrides == WEBHOOK_VARIABLES
        assert consumed == set(WEBHOOK_VARIABLES)
        # Without the mode nothing is flagged, so nothing maps — as before.
        assert session_override_values(HAIER_FIELDS, WEBHOOK_VARIABLES) == ({}, set())

    def test_spellings_and_types_follow_the_flag_rules(self):
        fields = [
            {"key": "customer_name", "type": "string"},
            {"key": "visit_count", "type": "integer"},
            {"key": "history", "type": "object"},
            {"key": "items", "type": "array"},
        ]
        overrides, consumed = session_override_values(
            fields, {"customerName": "Arjun", "visit_count": "two",
                     "history": "{}", "items": "a,b"},
            all_fields=True,
        )
        assert overrides == {"customer_name": "Arjun"}
        # Container fields never come from a scalar variable: not consumed,
        # so the raw text stays a plain session value.
        assert consumed == {"customerName", "visit_count"}
        assert validate_field_definitions(fields) == []

    def test_blank_is_not_a_value(self):
        overrides, consumed = session_override_values(
            HAIER_FIELDS, {"product_name": "  ", "customer_name": "Arjun"}, all_fields=True,
        )
        assert overrides == {"customer_name": "Arjun"}
        assert consumed == {"product_name", "customer_name"}


class TestPresence:
    def test_variables_block_counts_even_when_blank(self):
        assert dialer_variables_present(None) is False
        assert dialer_variables_present({}) is False
        assert dialer_variables_present({"customer_name": ""}) is True
        assert dialer_variables_present(WEBHOOK_VARIABLES) is True

    def test_record_selector_is_not_caller_context(self):
        assert dialer_variables_present({"customer_context_id": "rec_1"}) is False
        assert dialer_variables_present({"customer_context_id": "rec_1", "campaign": "nov"}) is True


class TestBuildPrecedence:
    def test_dialer_mode_wins_over_a_payload_for_every_declared_field(self):
        # The explicit-record case: a payload is present, the variables still
        # win per field; only what the dialer did not send comes from it.
        ctx = build_runtime_context(
            tenant_id="tn", bot_id="bot", field_definitions=HAIER_FIELDS,
            payload=HAIER_FIXTURE, payload_source="record",
            session_variables=WEBHOOK_VARIABLES, source_mode="dialer",
        )
        assert {k: ctx.get(k) for k in WEBHOOK_VARIABLES} == WEBHOOK_VARIABLES
        assert all(ctx.values[k].source == "session" for k in WEBHOOK_VARIABLES)
        assert ctx.get("l3_tagging") == "installation"
        assert ctx.values["l3_tagging"].source == "record"

    def test_manual_mode_lets_the_fixture_overwrite_the_webhook(self):
        # Pins the pre-change behaviour Haier ran into: without the mode (or
        # a flag) the test customer overwrites the real caller.
        ctx = build_runtime_context(
            tenant_id="tn", bot_id="bot", field_definitions=HAIER_FIELDS,
            payload=HAIER_FIXTURE, payload_source="test",
            session_variables=WEBHOOK_VARIABLES, source_mode="manual",
        )
        assert ctx.get("customer_name") == "Rohan"
        assert ctx.values["customer_name"].source == "test"
        assert ctx.get("rejection_reason") == "bill_not_available"


@pytest.fixture
def loader(monkeypatch):
    """load_runtime_context with the DB and HTTP edges stubbed and observed."""
    state = {"schema": None, "record": None, "record_lookups": [], "api_calls": 0}
    monkeypatch.setattr(rc, "_load_schema_sync", lambda tenant, bot: state["schema"])

    def fake_record(tenant, bot, record_id=None, phone=None):
        state["record_lookups"].append({"record_id": record_id, "phone": phone})
        if record_id and state["record"] and state["record"]["id"] == record_id:
            return state["record"]
        if phone and state["record"] and state["record"].get("phone") == phone:
            return state["record"]
        return None

    monkeypatch.setattr(rc, "_load_record_sync", fake_record)

    async def fake_fetch(api, variables):
        state["api_calls"] += 1
        return HAIER_FIXTURE, None

    monkeypatch.setattr(rc, "_fetch_api_payload", fake_fetch)
    return state


class TestLoader:
    async def test_webhook_call_runs_on_the_variables_alone(self, loader):
        loader["schema"] = _schema("dialer")
        # A record staged for this very number must NOT be consulted either.
        loader["record"] = {"id": "rec_1", "phone": "9790135919",
                            "data": {"customer_name": "Someone Else"}, "call_state": {}}
        ctx = await load_runtime_context(
            "tn_fb39b67f5361", "bot_450a4f7b4832", phone="9790135919",
            session_variables=WEBHOOK_VARIABLES,
            system_values={"call_channel": "freeswitch", "bot_language": "hi-IN"},
        )
        assert ctx.payload_skipped is True
        assert ctx.api_skipped is False and loader["api_calls"] == 0
        assert loader["record_lookups"] == []
        assert ctx.record_id is None
        assert {k: ctx.get(k) for k in WEBHOOK_VARIABLES} == WEBHOOK_VARIABLES
        assert _sources(ctx) == {"session", "system"}
        # Nothing from the fixture, not even the key the dialer never sends.
        assert ctx.get("l3_tagging") is None
        assert ctx.declared_missing() == ["l3_tagging"]
        # What the workflow and the spoken lines see.
        values = ctx.prompt_values()
        assert values["rejection_reason"] == "customer_refused_visit"   # prefillFromContext
        assert sanitize_spoken_text(GREETING, values) == "Am I speaking with Arjun?"
        assert sanitize_spoken_text(INTRO_LINE, values) == (
            "This call is regarding the request for your Split AC, which was "
            "rejected by our field team."
        )
        section = ctx.prompt_section()
        assert "- Name: Arjun" in section and "- Product: Split AC" in section
        assert "Not available on this call (never guess): Request type (L3 tagging)" in section

    async def test_missing_and_blank_fields_stay_unknown(self, loader):
        loader["schema"] = _schema("dialer")
        ctx = await load_runtime_context(
            "tn", "bot", phone="9790135919",
            session_variables={"customer_name": "Arjun", "product_name": "",
                               "rejection_reason": "some_new_code"},
        )
        assert ctx.payload_skipped is True
        assert ctx.get("customer_name") == "Arjun"
        assert ctx.get("product_name") is None            # blank → unknown, never "Split AC"
        assert ctx.get("rejection_reason") == "some_new_code"
        assert ctx.get("rejection_reason_label") is None
        assert ctx.get("l3_tagging") is None
        assert set(ctx.declared_missing()) == {"product_name", "rejection_reason_label", "l3_tagging"}
        assert "Not available on this call (never guess): Product, " in ctx.prompt_section()
        values = ctx.prompt_values()
        assert sanitize_spoken_text("Thank you. Has your [product_name] been delivered?", values) == (
            "Thank you. Has your been delivered?"
        )

    async def test_all_blank_variables_never_pull_the_fixture(self, loader):
        loader["schema"] = _schema("dialer")
        ctx = await load_runtime_context(
            "tn", "bot", phone="9790135919",
            session_variables={k: "" for k in WEBHOOK_VARIABLES},
        )
        assert ctx.payload_skipped is True
        assert all(ctx.get(k) is None for k in HAIER_FIXTURE)
        assert ctx.declared_missing() == [f["key"] for f in HAIER_FIELDS]
        assert "test" not in _sources(ctx)

    async def test_undeclared_variables_keep_their_place(self, loader):
        loader["schema"] = _schema("dialer")
        ctx = await load_runtime_context(
            "tn", "bot", session_variables={**WEBHOOK_VARIABLES, "campaign_id": "NOV-7"},
        )
        assert ctx.get("campaign_id") == "NOV-7"
        assert ctx.values["campaign_id"].source == "session"

    async def test_session_without_variables_uses_the_fixture(self, loader):
        loader["schema"] = _schema("dialer")
        for variables in (None, {}):
            ctx = await load_runtime_context(
                "tn", "bot", phone="9790135919", session_variables=variables,
            )
            assert ctx.payload_skipped is False
            assert ctx.get("customer_name") == "Rohan"
            assert ctx.values["customer_name"].source == "test"
            assert ctx.get("l3_tagging") == "installation"
        # The phone-matched record was looked up, as manual mode does.
        assert loader["record_lookups"][-1] == {"record_id": None, "phone": "9790135919"}

    async def test_session_without_variables_uses_a_phone_record(self, loader):
        loader["schema"] = _schema("dialer")
        loader["record"] = {"id": "rec_1", "phone": "9790135919",
                            "data": {"customer_name": "Meera", "product_name": "Washing Machine"},
                            "call_state": {}}
        ctx = await load_runtime_context("tn", "bot", phone="9790135919")
        assert ctx.record_id == "rec_1" and ctx.payload_skipped is False
        assert ctx.get("customer_name") == "Meera"
        assert ctx.values["customer_name"].source == "record"

    async def test_explicitly_selected_record_is_honoured_under_the_variables(self, loader):
        loader["schema"] = _schema("dialer")
        loader["record"] = {"id": "rec_7", "phone": "0000000000",
                            "data": {"customer_name": "Meera", "l3_tagging": "complaint"},
                            "call_state": {}}
        ctx = await load_runtime_context(
            "tn", "bot", phone="9790135919", record_id="rec_7",
            session_variables=WEBHOOK_VARIABLES,
        )
        assert loader["record_lookups"] == [{"record_id": "rec_7", "phone": None}]
        assert ctx.record_id == "rec_7" and ctx.payload_skipped is False
        assert ctx.get("customer_name") == "Arjun"           # dialer still wins per field
        assert ctx.values["customer_name"].source == "session"
        assert ctx.get("l3_tagging") == "complaint"          # the selected record fills the rest
        assert ctx.values["l3_tagging"].source == "record"

    async def test_record_selector_alone_is_not_dialer_context(self, loader):
        loader["schema"] = _schema("dialer")
        ctx = await load_runtime_context(
            "tn", "bot", phone="9790135919", record_id="rec_missing",
            session_variables={"customer_context_id": "rec_missing"},
        )
        # No such record → the session carries no caller context → fixture.
        assert ctx.payload_skipped is False
        assert ctx.get("customer_name") == "Rohan"

    async def test_dialer_mode_without_test_data_is_simply_empty(self, loader):
        loader["schema"] = _schema("dialer", test_payload=None)
        ctx = await load_runtime_context("tn", "bot", phone="9790135919")
        assert ctx.payload_skipped is False
        assert ctx.get("customer_name") is None
        assert ctx.declared_missing() == [f["key"] for f in HAIER_FIELDS]

    async def test_manual_mode_is_unchanged_by_the_same_webhook(self, loader):
        loader["schema"] = _schema("manual")
        ctx = await load_runtime_context(
            "tn", "bot", phone="9790135919", session_variables=WEBHOOK_VARIABLES,
        )
        assert ctx.payload_skipped is False
        assert ctx.get("customer_name") == "Rohan"            # fixture over the dialer, as before
        assert ctx.values["customer_name"].source == "test"
        assert ctx.get("rejection_reason") == "bill_not_available"

    async def test_api_mode_is_unchanged_by_the_new_helpers(self, loader):
        api = {"id": "api_1", "name": "User Details", "method": "GET",
               "url": "https://crm.example/users/{{caller_phone}}", "headers": {},
               "query_params": {}, "body_template": None, "timeout_ms": 2000,
               "secret_ref": None, "auth_type": "none", "sensitive_masks": []}
        loader["schema"] = _schema("api", api=api)
        ctx = await load_runtime_context(
            "tn", "bot", phone="9790135919", session_variables=WEBHOOK_VARIABLES,
        )
        assert loader["api_calls"] == 1
        assert ctx.payload_skipped is False and ctx.api_skipped is False
        assert ctx.get("customer_name") == "Rohan"            # api over unflagged session
