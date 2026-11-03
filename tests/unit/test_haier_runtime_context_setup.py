"""haier/setup/06 — pure transform of the bot's runtime-context schema."""

import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "haier_setup_06",
    pathlib.Path(__file__).resolve().parents[2]
    / "haier" / "setup" / "06_runtime_context_dialer_variables.py",
)
setup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(setup)

# The schema as it is on local and demo live (rcs_475fe865f3d7, 2026-11-03):
# manual mode, one declared field, a five-key test customer.
CURRENT = {
    "id": "rcs_475fe865f3d7", "botId": "bot_450a4f7b4832", "name": "User details",
    "sourceMode": "manual", "apiConnectionId": None, "responsePath": None,
    "fields": [{"key": "customer_name", "type": "string", "label": "Name"}],
    "allowAdditional": True,
    "testPayload": {
        "l3_tagging": "installation", "product_name": "Split AC",
        "customer_name": "Rohan", "rejection_reason": "customer_refused_visit",
        "rejection_reason_label": "the visit was declined when our technician called",
    },
    "missingValuePolicy": None, "domainPolicy": "generic",
    "identityConfirmationMode": None, "status": "active", "configured": True,
}
WEBHOOK_VARIABLES = {
    "customer_name": "Arjun", "product_name": "Split AC",
    "rejection_reason": "customer_refused_visit",
    "rejection_reason_label": "the visit was declined when our technician called",
}


def test_body_switches_to_dialer_mode_and_declares_the_contract():
    from shared.runtime_context import validate_field_definitions, validate_payload

    body = setup.upgrade_schema(CURRENT)
    assert body["sourceMode"] == "dialer"
    assert body["apiConnectionId"] is None
    assert [f["key"] for f in body["fields"]] == [
        "customer_name", "product_name", "rejection_reason",
        "rejection_reason_label", "l3_tagging",
    ]
    assert body["fields"][0]["label"] == "Name"               # existing declaration kept
    assert body["fields"][0]["description"]                   # … and documented
    assert all(f["type"] == "string" for f in body["fields"])
    assert not any(f.get("sessionOverride") for f in body["fields"])
    assert body["testPayload"] == CURRENT["testPayload"]      # untouched
    assert body["missingValuePolicy"] == setup.MISSING_VALUE_POLICY
    assert len(body["missingValuePolicy"]) <= 500
    assert body["domainPolicy"] == "generic"
    assert body["identityConfirmationMode"] is None
    assert validate_field_definitions(body["fields"]) == []
    # The saved test customer still validates against the declared fields.
    assert validate_payload(body["fields"], body["testPayload"])[0] == []
    assert "sessionOverride" not in CURRENT["fields"][0]      # input not mutated


def test_webhook_payload_maps_onto_every_declared_field():
    from shared.runtime_context import session_override_values

    body = setup.upgrade_schema(CURRENT)
    overrides, consumed = session_override_values(
        body["fields"], WEBHOOK_VARIABLES, all_fields=True,
    )
    assert overrides == WEBHOOK_VARIABLES
    assert consumed == set(WEBHOOK_VARIABLES)


def test_upgrade_is_idempotent_and_keeps_tenant_edits():
    body = setup.upgrade_schema(CURRENT)
    saved = {**CURRENT, **body}
    again = setup.upgrade_schema(saved)
    assert again == body
    assert setup.describe(saved, again) == ["  (no change — already applied)"]
    # A tenant-authored policy or label is never overwritten.
    edited = {**saved, "missingValuePolicy": "Never guess."}
    edited["fields"] = [dict(f, label="Customer") if f["key"] == "product_name" else f
                        for f in saved["fields"]]
    body2 = setup.upgrade_schema(edited)
    assert body2["missingValuePolicy"] == "Never guess."
    assert next(f for f in body2["fields"] if f["key"] == "product_name")["label"] == "Customer"


def test_describe_lists_changes():
    lines = setup.describe(CURRENT, setup.upgrade_schema(CURRENT))
    assert lines[0] == "  ~ sourceMode: 'manual' -> 'dialer'"
    assert lines[1] == "  ~ missingValuePolicy: set"
    assert lines[2].startswith("  ~ customer_name: description=")
    assert lines[3].startswith("  + product_name:")
    assert lines[-1].startswith("  + l3_tagging:")
