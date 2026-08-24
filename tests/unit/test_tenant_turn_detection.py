"""Tenant turn-detection schema, profiles and runtime snapshot precedence."""

from shared.bot_config import ResolvedBotConfig
from shared.turn_detection import (
    NOISE_GATE_BOUNDS,
    NOISE_GATE_DEFAULTS,
    TURN_DETECTION_BOUNDS,
    TURN_DETECTION_DEFAULTS,
    TURN_DETECTION_FIELDS,
    resolve_tenant_turn_detection,
    tenant_turn_detection_payload,
    validate_tenant_turn_detection,
)
from voice_runtime.pipeline import resolve_noise_gate, resolve_turn_detection


def test_authoritative_fields_generate_all_runtime_defaults_and_bounds():
    turn_keys = {field["key"] for field in TURN_DETECTION_FIELDS if field["group"] == "turn_detection"}
    gate_keys = {field["key"] for field in TURN_DETECTION_FIELDS if field["group"] == "noise_gate"}
    assert turn_keys == set(TURN_DETECTION_BOUNDS) == set(TURN_DETECTION_DEFAULTS["browser"])
    assert gate_keys == set(NOISE_GATE_BOUNDS) == set(NOISE_GATE_DEFAULTS["telephony"])


def test_missing_tenant_config_is_exactly_backward_compatible_with_defaults():
    effective = resolve_tenant_turn_detection(None)
    for transport in ("browser", "telephony"):
        assert effective[transport]["turn_detection"] == TURN_DETECTION_DEFAULTS[transport]
        assert effective[transport]["noise_gate"] == NOISE_GATE_DEFAULTS[transport]


def test_recommended_is_balanced_transport_specific_and_within_bounds():
    effective = resolve_tenant_turn_detection({"mode": "recommended"})
    browser = effective["browser"]["turn_detection"]
    phone = effective["telephony"]["turn_detection"]
    assert browser["complete_endpoint"] < phone["complete_endpoint"]
    assert browser["user_speech_timeout"] < phone["user_speech_timeout"]
    assert browser["confidence"] != phone["confidence"]
    for transport in effective.values():
        for key, value in transport["turn_detection"].items():
            assert TURN_DETECTION_BOUNDS[key][0] <= value <= TURN_DETECTION_BOUNDS[key][1]
        for key, value in transport["noise_gate"].items():
            assert NOISE_GATE_BOUNDS[key][0] <= value <= NOISE_GATE_BOUNDS[key][1]


def test_custom_partial_config_isolated_by_transport_and_falls_back_per_field():
    effective = resolve_tenant_turn_detection({
        "mode": "custom",
        "overrides": {"telephony": {"turn_detection": {"confidence": 0.51}}},
    })
    assert effective["telephony"]["turn_detection"]["confidence"] == 0.51
    assert effective["telephony"]["turn_detection"]["stop_secs"] == TURN_DETECTION_DEFAULTS["telephony"]["stop_secs"]
    assert effective["browser"]["turn_detection"] == TURN_DETECTION_DEFAULTS["browser"]


def test_invalid_or_old_storage_safely_falls_back_or_clamps():
    effective = resolve_tenant_turn_detection({
        "mode": "custom",
        "overrides": {"browser": {"turn_detection": {
            "confidence": "junk", "user_speech_timeout": 99,
        }}},
    })
    assert effective["browser"]["turn_detection"]["confidence"] == TURN_DETECTION_DEFAULTS["browser"]["confidence"]
    assert effective["browser"]["turn_detection"]["user_speech_timeout"] == TURN_DETECTION_DEFAULTS["browser"]["user_speech_timeout"]


def test_api_validation_rejects_out_of_bounds_unknown_and_fractional_word_count():
    errors = validate_tenant_turn_detection({
        "mode": "custom",
        "overrides": {"browser": {"turn_detection": {
            "confidence": 0.1,
            "barge_in_min_words": 1.5,
            "not_runtime_safe": 1,
        }}},
    })
    assert len(errors) == 3
    assert "between" in errors[0]


def test_payload_exposes_effective_values_schema_units_and_profiles():
    payload = tenant_turn_detection_payload({"mode": "recommended"})
    assert payload["mode"] == "recommended"
    assert {item["id"] for item in payload["transports"]} == {"browser", "telephony"}
    assert all({"min", "max", "unit", "default", "recommended"} <= set(field) for field in payload["fields"])


def test_runtime_uses_session_snapshot_before_legacy_bot_settings():
    snapshot = resolve_tenant_turn_detection({
        "mode": "custom",
        "overrides": {"telephony": {
            "turn_detection": {"confidence": 0.52},
            "noise_gate": {"min_threshold_dbfs": -55},
        }},
    })
    config = ResolvedBotConfig(
        tenant_id="tn-a", bot_id="bot-a", bot_name="A", version="1", published=True,
        turn_detection=snapshot,
        stt={"settings": {
            "turn_detection": {"confidence": 0.9},
            "noise_gate": {"min_threshold_dbfs": -30},
        }},
    )
    assert resolve_turn_detection(config, "telephony")["confidence"] == 0.52
    assert resolve_noise_gate(config, "telephony")["min_threshold_dbfs"] == -55


def test_enabling_tenant_snapshot_with_defaults_changes_no_effective_timing():
    common = dict(
        tenant_id="tn-a", bot_id="bot-a", bot_name="A", version="1", published=True,
    )
    pre_feature = ResolvedBotConfig(**common)
    with_tenant_layer = ResolvedBotConfig(
        **common, turn_detection=resolve_tenant_turn_detection(None)
    )
    for transport in ("browser", "telephony"):
        assert resolve_turn_detection(with_tenant_layer, transport) == resolve_turn_detection(pre_feature, transport)
        assert resolve_noise_gate(with_tenant_layer, transport) == resolve_noise_gate(pre_feature, transport)


def test_old_cached_snapshot_keeps_legacy_fallback_until_cache_expiry():
    config = ResolvedBotConfig(
        tenant_id="tn-a", bot_id="bot-a", bot_name="A", version="1", published=True,
        stt={"settings": {"turn_detection": {"confidence": 0.8}}},
    )
    assert resolve_turn_detection(config, "browser")["confidence"] == 0.8
