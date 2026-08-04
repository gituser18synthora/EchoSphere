"""Generic runtime context — the domain-independent user-details layer.

Pins the contract that makes the platform multi-domain: tenant-defined
fields of any shape validate with types preserved, every value carries its
provenance, sensitive values are masked at build time (raw never enters the
snapshot), unknowns are stated rather than invented, and a healthcare or
real-estate schema works through configuration alone — no code changes.
"""

from shared.customer_context import CustomerContextSnapshot
from shared.runtime_context import (
    build_runtime_context,
    collection_snapshot_from_context,
    context_from_collection_snapshot,
    resolve_response_path,
    validate_field_definitions,
    validate_payload,
)

LOAN_FIELDS = [
    {"key": "customer_name", "type": "string", "required": True},
    {"key": "overdue_amount", "type": "number"},
    {"key": "days_overdue", "type": "integer"},
    {"key": "due_date", "type": "date"},
    {"key": "partial_payment_allowed", "type": "boolean"},
    {"key": "payment_methods", "type": "array"},
    {"key": "active_offers", "type": "array"},
    {"key": "loan_account_number", "type": "string", "sensitive": True},
]


class TestPayloadValidation:
    def test_types_preserved_exactly(self):
        payload = {
            "customer_name": "Rahul Sharma",
            "overdue_amount": 12500,
            "days_overdue": 18,
            "due_date": "2026-07-17",
            "partial_payment_allowed": True,
            "payment_methods": ["UPI", "Net Banking"],
            "active_offers": [],
        }
        errors, clean = validate_payload(LOAN_FIELDS, payload)
        assert errors == []
        assert clean["overdue_amount"] == 12500
        assert isinstance(clean["overdue_amount"], int)  # number accepts int
        assert clean["partial_payment_allowed"] is True
        assert clean["payment_methods"] == ["UPI", "Net Banking"]
        assert clean["active_offers"] == []

    def test_wrong_types_rejected_not_coerced(self):
        errors, clean = validate_payload(LOAN_FIELDS, {
            "customer_name": "X",
            "overdue_amount": "12500",       # string is NOT a number
            "days_overdue": 2.5,             # float is NOT an integer
            "partial_payment_allowed": "yes",
            "due_date": "17-07-2026",        # not ISO
        })
        bad = {e["field"] for e in errors}
        assert bad == {"overdue_amount", "days_overdue",
                       "partial_payment_allowed", "due_date"}
        # Invalid values are dropped, never half-coerced.
        assert "overdue_amount" not in clean

    def test_boolean_never_satisfies_number(self):
        errors, _ = validate_payload(
            [{"key": "amount", "type": "number"}], {"amount": True}
        )
        assert errors

    def test_required_and_null_semantics(self):
        errors, clean = validate_payload(LOAN_FIELDS, {
            "customer_name": None,  # null == absent
            "offer_terms": None,
        })
        assert any(e["field"] == "customer_name" for e in errors)
        assert clean == {}

    def test_arbitrary_additional_fields_allowed(self):
        errors, clean = validate_payload(LOAN_FIELDS, {
            "customer_name": "X",
            "custom_score": {"model": "v2", "value": 0.87},
            "tags": ["priority", "north-zone"],
        })
        assert errors == []
        assert clean["custom_score"]["value"] == 0.87
        assert clean["tags"] == ["priority", "north-zone"]

    def test_closed_schema_rejects_extras(self):
        errors, clean = validate_payload(
            LOAN_FIELDS, {"customer_name": "X", "surprise": 1},
            allow_additional=False,
        )
        assert any(e["field"] == "surprise" for e in errors)
        assert "surprise" not in clean

    def test_field_definition_validation(self):
        errors = validate_field_definitions([
            {"key": "ok_name", "type": "string"},
            {"key": "bad key!", "type": "string"},
            {"key": "ok_name", "type": "string"},        # duplicate
            {"key": "bad_type", "type": "money"},
        ])
        messages = " ".join(e["message"] for e in errors)
        assert "identifiers" in messages
        assert "Duplicate" in messages
        assert "Type must be one of" in messages


class TestBuildContext:
    def _ctx(self, **kwargs):
        return build_runtime_context(
            tenant_id="tn-1", bot_id="bot-1",
            field_definitions=LOAN_FIELDS, **kwargs,
        )

    def test_sources_and_precedence(self):
        ctx = self._ctx(
            system_values={"call_channel": "telephony", "customer_name": "SYSTEM"},
            session_variables={"customer_name": "Dialer Name", "campaign": "july"},
            payload={"customer_name": "Rahul Sharma", "overdue_amount": 12500},
            payload_source="api",
        )
        # payload (api) > session > system for the same key.
        assert ctx.get("customer_name") == "Rahul Sharma"
        assert ctx.values["customer_name"].source == "api"
        assert ctx.values["campaign"].source == "session"
        assert ctx.values["call_channel"].source == "system"
        # Workflow values win over everything and are tagged.
        ctx.set_workflow_value("payment_status", "completed")
        assert ctx.values["payment_status"].source == "workflow"

    def test_sensitive_masked_at_build_time(self):
        ctx = self._ctx(payload={
            "customer_name": "R", "loan_account_number": "LN00123456",
        })
        entry = ctx.values["loan_account_number"]
        assert entry.sensitive is True
        assert entry.value == "XX3456"
        # The raw value exists nowhere in the snapshot or any of its views.
        assert "LN00123456" not in str(ctx.items_with_sources())
        assert "LN00123456" not in ctx.prompt_section()
        assert "LN00123456" not in str(ctx.prompt_values())

    def test_sensitive_key_hints_mask_even_unflagged(self):
        ctx = build_runtime_context(
            tenant_id="t", bot_id="b", field_definitions=[],
            payload={"card_number": "4111111111111111"},
        )
        assert ctx.values["card_number"].value != "4111111111111111"

    def test_prompt_values_flatten_nested(self):
        ctx = build_runtime_context(
            tenant_id="t", bot_id="b",
            field_definitions=[{"key": "appointment", "type": "object"}],
            payload={"appointment": {"date": "2026-08-10", "doctor": "Dr. Rao"},
                     "payment_methods": ["UPI", "Card"]},
        )
        flat = ctx.prompt_values()
        assert flat["appointment.date"] == "2026-08-10"
        assert flat["payment_methods"] == "UPI, Card"

    def test_prompt_section_states_knowns_and_unknowns(self):
        ctx = self._ctx(
            payload={"customer_name": "Rahul Sharma"},
            missing_value_policy="Say you don't have it and offer a callback.",
        )
        section = ctx.prompt_section()
        assert "Rahul Sharma" in section
        assert "UNKNOWN" in section
        assert "never" in section.lower()          # never guess / invent
        assert "overdue_amount" in section          # declared but absent
        assert "offer a callback" in section        # tenant policy included

    def test_empty_context_is_explicit_not_silent(self):
        ctx = build_runtime_context(tenant_id="t", bot_id="b")
        section = ctx.prompt_section()
        assert "No caller-specific values" in section
        assert "Never" in section


class TestDomainConfigurations:
    """Healthcare and real-estate bots configured with zero code changes."""

    def test_healthcare_schema(self):
        fields = [
            {"key": "patient_name", "type": "string", "required": True},
            {"key": "patient_id", "type": "string", "sensitive": True},
            {"key": "appointment", "type": "object"},
            {"key": "allergies", "type": "array"},
            {"key": "insurance_verified", "type": "boolean"},
        ]
        payload = {
            "patient_name": "Meera Iyer",
            "patient_id": "MRN-778812",
            "appointment": {"date": "2026-08-11", "time": "10:15",
                            "doctor": "Dr. Kulkarni", "department": "Cardiology"},
            "allergies": ["penicillin"],
            "insurance_verified": True,
        }
        errors, clean = validate_payload(fields, payload)
        assert errors == []
        ctx = build_runtime_context(
            tenant_id="tn-health", bot_id="bot-h",
            field_definitions=fields, payload=clean, payload_source="api",
        )
        assert ctx.domain_policy == "generic"
        assert ctx.prompt_values()["appointment.doctor"] == "Dr. Kulkarni"
        assert ctx.values["patient_id"].value == "XX8812"
        section = ctx.prompt_section()
        assert "Meera Iyer" in section and "MRN-778812" not in section

    def test_real_estate_schema(self):
        fields = [
            {"key": "lead_name", "type": "string"},
            {"key": "budget_max", "type": "number"},
            {"key": "properties", "type": "array"},
            {"key": "site_visit", "type": "object"},
        ]
        payload = {
            "lead_name": "Arjun",
            "budget_max": 7500000,
            "properties": [{"id": "P-12", "locality": "Baner", "bhk": 2}],
            "site_visit": {"scheduled": False},
        }
        errors, clean = validate_payload(fields, payload)
        assert errors == []
        ctx = build_runtime_context(
            tenant_id="tn-re", bot_id="bot-r",
            field_definitions=fields, payload=clean, payload_source="test",
        )
        assert ctx.get("budget_max") == 7500000
        assert ctx.values["properties"].source == "test"


class TestCollectionsCompatibility:
    def test_snapshot_round_trip(self):
        snap = CustomerContextSnapshot(
            context_id="cctx1", tenant_id="tn", bot_id="b",
            customer_name="Ramesh", lender_name="eDAS Finance",
            loan_account_masked="XX8976", overdue_amount=4850.0,
            days_overdue=12, payment_methods=("UPI",),
            payment_status="pending",
        )
        ctx = context_from_collection_snapshot(snap)
        assert ctx.domain_policy == "collections"
        assert ctx.get("customer_name") == "Ramesh"
        back = collection_snapshot_from_context(ctx)
        assert back.customer_name == "Ramesh"
        assert back.overdue_amount == 4850.0
        assert back.loan_account_masked == "XX8976"
        assert back.payment_methods == ("UPI",)
        assert back.payment_status == "pending"

    def test_generic_payload_projects_onto_collection_policy(self):
        """A collections tenant using the NEW schema (API/test source) still
        drives the deterministic policy — via projection, not new columns."""
        ctx = build_runtime_context(
            tenant_id="tn", bot_id="b",
            field_definitions=LOAN_FIELDS,
            payload={
                "customer_name": "Rahul Sharma", "overdue_amount": 12500,
                "days_overdue": 18, "partial_payment_allowed": True,
                "payment_methods": ["UPI", "Net Banking"],
                "payment_status": "unpaid",
            },
            payload_source="test", domain_policy="collections",
        )
        snap = collection_snapshot_from_context(ctx)
        assert snap.customer_name == "Rahul Sharma"
        assert snap.overdue_amount == 12500.0
        assert snap.days_overdue == 18
        assert snap.partial_payment_allowed is True
        assert snap.payment_methods == ("UPI", "Net Banking")


class TestResponsePath:
    def test_dot_paths(self):
        body = {"data": {"customer": {"name": "X"}, "items": [{"id": 1}]}}
        assert resolve_response_path(body, "data.customer") == {"name": "X"}
        assert resolve_response_path(body, "data.items.0") == {"id": 1}
        assert resolve_response_path(body, "") is body
        assert resolve_response_path(body, "data.missing") is None
