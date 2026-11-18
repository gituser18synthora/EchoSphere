"""Configuration repairs preserve the saved graph and require no database."""
import copy

import pytest

from tenant.Wordline.setup.call_review_repair import (
    CONFIRMATION_HEADING, OPTIONS, repair_prompt, repair_workflow, stage_dynamic_readbacks,
)
from tenant.Wordline.setup.identifier_confirmation import upgrade
from tenant.Wordline.tests.test_identifier_confirmation import original


def test_repair_preserves_graph_and_unrelated_settings_and_is_idempotent():
    source = upgrade(original(), ["1132986A"])
    source["nodes"].extend([
        {"id": name, "kind": "intent", "x": 30, "y": 40, "config": {
            "prompt": "Long old text", "textByLanguage": {"hi": "old", "ta": "retained"},
            "optionHub": True, "custom": {"untouched": True},
        }} for name in ("n_options", "n_options_unverified", "n_pr_again")
    ])
    frozen = copy.deepcopy(source)
    updated = repair_workflow(source)
    assert source == frozen and updated["edges"] == source["edges"]
    assert [n["id"] for n in updated["nodes"]] == [n["id"] for n in source["nodes"]]
    assert repair_workflow(updated) == updated
    for node in updated["nodes"]:
        if node["id"] in {"n_options", "n_options_unverified", "n_pr_again"}:
            assert node["config"]["textByLanguage"]["en"] == OPTIONS["en"]
            assert node["config"]["textByLanguage"]["ta"] == "retained"
            assert node["config"]["custom"] == {"untouched": True}
            assert (node["x"], node["y"]) == (30, 40)


def test_prompt_repair_preserves_business_instructions_without_duplicate_appendices():
    original_prompt = ("# BUSINESS\nDo not promise a delivery date.\n"
                       "After Terminal ID lookup, the flow confirms outlet name, bank, terminal model "
                       "and registered contact number individually.\n\n"
                       + CONFIRMATION_HEADING + "\nOld rules.\n\n# LATER SECTION\nRetain this too.\n")
    result = repair_prompt(original_prompt)
    assert "Do not promise a delivery date." in result
    assert "Only describe details actually confirmed" in result
    assert "It's correct" in result and "fresh confirmation" in result
    assert "say every digit separately in English" in result
    assert result.count(CONFIRMATION_HEADING) == 1
    assert "# LATER SECTION\nRetain this too." in result
    assert repair_prompt(result) == result


def test_unrecognised_prompt_shape_fails_without_silently_appending_rules():
    with pytest.raises(ValueError):
        repair_prompt("An unrelated prompt")


def test_live_repair_does_not_add_metadata_requiring_new_runtime():
    source = upgrade(original(), ["1132986A"])
    for node in repair_workflow(source)["nodes"]:
        for localized in node.get("config", {}).get("readback", {}).values():
            assert "formats" not in localized
            assert all("format" not in f for f in localized.get("fields", []))


def test_dynamic_contact_and_id_formats_are_separate_and_idempotent():
    source = upgrade(original(), ["1132986A"])
    for name in ("n_bat_contact", "n_bat_visit_contact", "n_pr_visit_contact",
                 "n_bat_visit_record", "n_pr_visit_record"):
        source["nodes"].append({"id": name, "kind": "intent", "config": {
            "readback": {language: {"groups": [{"template": "{contact_number} {terminal_id}"}]}
                         for language in ("en", "hi")},
        }})
    frozen = copy.deepcopy(source)
    staged = stage_dynamic_readbacks(source)
    assert source == frozen and staged["edges"] == source["edges"]
    assert stage_dynamic_readbacks(staged) == staged
    for node in staged["nodes"]:
        config = node.get("config", {})
        for localized in config.get("readback", {}).values():
            if config.get("confirmsIdentifier"):
                assert localized["fields"][0]["format"] == "identifier"
                assert "values" not in localized["fields"][0]
            if node["id"] in {"n_bat_contact", "n_bat_visit_contact", "n_pr_visit_contact"}:
                assert localized["formats"] == {"contact_number": "phone"}
            if node["id"] in {"n_bat_visit_record", "n_pr_visit_record"}:
                assert localized["formats"] == {"terminal_id": "identifier"}


def test_live_sms_destination_change_preserves_quantity_model_and_amount_text():
    source = upgrade(original())
    text = "Model Linura 7220, amount 2500, quantity 12; send SMS to 575758."
    source["nodes"].append({"id": "n_sms", "kind": "intent", "config": {
        "prompt": text, "textByLanguage": {"en": text, "hi": text},
    }})
    config = next(n for n in repair_workflow(source)["nodes"] if n["id"] == "n_sms")["config"]
    expected = text.replace("575758", "five, seven, five, seven, five, eight")
    assert config["prompt"] == expected
    assert config["textByLanguage"] == {"en": expected, "hi": expected}


def test_docket_readback_uses_the_prefilled_slot_the_engine_actually_has():
    """``docket_number`` is a call-context placeholder, never a workflow slot:
    a readback group requiring it dropped the whole docket message."""
    from shared.orchestration.workflow_engine import render_readback
    from tenant.Wordline.setup.call_review_repair import DOCKET_SLOT

    source = upgrade(original())
    text = {"en": "Your docket number is {docket_number}. Kindly note it.",
            "hi": "आपका docket number है {docket_number}। कृपया note कर लीजिए।"}
    source["nodes"].append({"id": "n_bat_docket", "kind": "message",
                            "config": {"text": text["en"], "textByLanguage": dict(text)}})
    staged = stage_dynamic_readbacks(source)
    assert stage_dynamic_readbacks(staged) == staged
    config = next(n for n in staged["nodes"] if n["id"] == "n_bat_docket")["config"]
    assert config["textByLanguage"] == text  # the authored text itself is kept
    slots = {DOCKET_SLOT: "WL458921"}
    assert render_readback(config["readback"]["hi"], slots) == (
        "आपका docket number है letter W, letter L, four, five, eight, nine, two, one। कृपया note कर लीजिए।")
    assert render_readback(config["readback"]["en"], slots) == (
        "Your docket number is letter W, letter L, four, five, eight, nine, two, one. Kindly note it.")
