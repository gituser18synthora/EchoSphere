"""Effective disposition: the runtime's stored value, else the structured
summary's ``disposition`` field (shared.post_call.dispositions)."""

from shared.post_call.dispositions import (
    STRUCTURED_DISPOSITION_FIELD,
    effective_disposition,
    structured_disposition,
)

HAIER_LABEL = "Connected – Service Required (Re-dispatch)"


class TestStructuredDisposition:
    def test_reads_the_disposition_structured_field(self):
        memory = {"structured_fields": {STRUCTURED_DISPOSITION_FIELD: HAIER_LABEL, "x": "y"}}
        assert structured_disposition(memory) == HAIER_LABEL

    def test_collapses_whitespace(self):
        memory = {"structured_fields": {"disposition": "  Connected –  Contact later \n"}}
        assert structured_disposition(memory) == "Connected – Contact later"

    def test_none_when_the_bot_declares_no_such_field(self):
        assert structured_disposition({"structured_fields": {"appointment_booked": "Yes"}}) is None
        assert structured_disposition({"structured_fields": {}}) is None
        assert structured_disposition({}) is None
        assert structured_disposition(None) is None

    def test_none_when_the_field_was_not_settled(self):
        assert structured_disposition({"structured_fields": {"disposition": None}}) is None
        assert structured_disposition({"structured_fields": {"disposition": "   "}}) is None

    def test_malformed_memory_shapes_are_ignored(self):
        assert structured_disposition({"structured_fields": ["disposition"]}) is None
        assert structured_disposition("not a dict") is None


class TestEffectiveDisposition:
    def test_runtime_value_wins(self):
        assert effective_disposition("promise_to_pay", HAIER_LABEL) == "promise_to_pay"

    def test_structured_fills_an_empty_runtime_value(self):
        assert effective_disposition(None, HAIER_LABEL) == HAIER_LABEL
        assert effective_disposition("", HAIER_LABEL) == HAIER_LABEL
        assert effective_disposition("   ", HAIER_LABEL) == HAIER_LABEL

    def test_none_when_neither_exists(self):
        assert effective_disposition(None, None) is None
        assert effective_disposition("", "") is None


class TestChoiceOptionBound:
    def test_official_labels_survive_option_and_value_validation(self):
        from shared.orchestration.goal_engine import SummaryFieldSpec

        long_label = "Connected – Issue Resolved (Either by customer or local Tech.)"
        assert len(long_label) == 62
        spec = SummaryFieldSpec.model_validate({
            "name": "disposition", "type": "choice", "source": "outcome",
            "options": [long_label, HAIER_LABEL],
            "values": {"cancel_resolved": long_label, "redispatch": HAIER_LABEL},
        })
        assert spec.options == [long_label, HAIER_LABEL]
        assert spec.values["cancel_resolved"] == long_label

    def test_options_remain_bounded(self):
        from shared.orchestration.goal_engine import SummaryFieldSpec

        spec = SummaryFieldSpec.model_validate({
            "name": "x", "type": "choice", "options": ["a" * 200], "values": {"k": "b" * 200},
        })
        assert len(spec.options[0]) == 80
        assert len(spec.values["k"]) == 80
