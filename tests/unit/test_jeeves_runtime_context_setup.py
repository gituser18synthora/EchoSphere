"""jeeves/setup/02 — pure transform of the bot's runtime-context fields."""

import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "jeeves_setup_02",
    pathlib.Path(__file__).resolve().parents[2]
    / "jeeves" / "setup" / "02_runtime_context_session_override.py",
)
setup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(setup)

# The schema as it is on local and demo live (rcs_dc75361df0af, 2026-10-19).
CURRENT_FIELDS = [
    {"key": "customer_name", "type": "string", "label": "Customer name"},
    {"key": "locality", "type": "string", "label": "Locality / masked address"},
    {"key": "connection_status", "type": "string", "label": "Connection status (DISCONNECTED …)"},
]


def test_upgrade_flags_three_fields_and_adds_context_line():
    from shared.runtime_context import validate_field_definitions

    upgraded = setup.upgrade_fields(CURRENT_FIELDS)
    assert [f["key"] for f in upgraded] == [
        "customer_name", "locality", "connection_status", "context_line",
    ]
    assert all(f["sessionOverride"] is True for f in upgraded)
    # Labels and types untouched; the input is not mutated.
    assert upgraded[1]["label"] == "Locality / masked address"
    assert "sessionOverride" not in CURRENT_FIELDS[0]
    assert validate_field_definitions(upgraded) == []


def test_upgrade_is_idempotent():
    once = setup.upgrade_fields(CURRENT_FIELDS)
    assert setup.upgrade_fields(once) == once
    assert setup.describe(once, setup.upgrade_fields(once)) == ["  (no change — already applied)"]


def test_describe_lists_changes():
    lines = setup.describe(CURRENT_FIELDS, setup.upgrade_fields(CURRENT_FIELDS))
    assert lines[0].startswith("  ~ customer_name: sessionOverride=true")
    assert lines[-1].startswith("  + context_line:")
