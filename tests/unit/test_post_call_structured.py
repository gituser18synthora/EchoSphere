"""Structured post-call summary fields: config-driven derivation from the
final workflow slots, analyst fill-in for the rest, vocabulary clamping, and
the rule that summary-only goal policies never change live behavior."""

from datetime import datetime, timezone

from shared.orchestration.goal_engine import BotGoalPolicy, compile_goal_policy
from shared.post_call.analyzer import _build_system, fallback_analysis
from shared.post_call.schema import parse_analysis
from shared.post_call.structured import (
    derive_structured_fields,
    merge_structured_fields,
    normalize_yes_no,
    summary_fields_prompt_block,
)

MDND_FIELDS = [
    {"name": "call_customer", "type": "yes_no", "source": "m_called_customer"},
    {"name": "reach_customer_location", "type": "yes_no",
     "source": "m_reached_location"},
    {"name": "hand_over_product", "type": "yes_no",
     "source": "m_handover_recipient",
     "values": {"not handed over": "No", "*": "Yes"}},
    {"name": "hand_over_to", "type": "choice", "source": "m_handover_recipient",
     "options": ["customer", "security_guard", "mother", "father", "brother",
                 "relative", "doorstep", "someone_else"],
     "values": {"guard / security": "security_guard",
                "customer (direct)": "customer", "mother": "mother",
                "father": "father", "brother": "brother",
                "relative (other)": "relative", "left at door": "doorstep",
                "someone else": "someone_else", "not handed over": ""}},
    {"name": "call_cx", "type": "yes_no", "source": "m_cx_support_call"},
    {"name": "guard_name", "type": "text", "source": "m_guard_name",
     "allowLlm": False},
]


def _policy() -> BotGoalPolicy:
    return BotGoalPolicy.model_validate({"summaryFields": MDND_FIELDS})


class TestNormalizeYesNo:
    def test_slot_canonicals_and_plain_words(self):
        assert normalize_yes_no("yes (called the customer)") == "Yes"
        assert normalize_yes_no("no (did not call)") == "No"
        assert normalize_yes_no("haan") == "Yes"
        assert normalize_yes_no("nahi") == "No"
        assert normalize_yes_no("हाँ") == "Yes"
        assert normalize_yes_no("नहीं") == "No"
        assert normalize_yes_no("YES") == "Yes"

    def test_unknown_or_empty_is_none(self):
        assert normalize_yes_no("") is None
        assert normalize_yes_no(None) is None
        assert normalize_yes_no("maybe") is None
        # "no" as a prefix of an unrelated word is not a negative answer.
        assert normalize_yes_no("nothing") is None


class TestDeriveFromSlots:
    def test_full_mdnd_slots_map_onto_the_reporting_vocabulary(self):
        slots = {
            "m_reached_location": "yes (reached the location)",
            "m_called_customer": "yes (called the customer)",
            "m_handover_recipient": "guard / security",
            "m_cx_support_call": "no (no CX support call)",
            "m_guard_name": "Ramesh",
        }
        assert derive_structured_fields(_policy(), slots) == {
            "call_customer": "Yes",
            "reach_customer_location": "Yes",
            "hand_over_product": "Yes",
            "hand_over_to": "security_guard",
            "call_cx": "No",
            "guard_name": "Ramesh",
        }

    def test_every_recipient_canonical_has_an_output_value(self):
        for canonical, expected in (
            ("customer (direct)", "customer"), ("mother", "mother"),
            ("father", "father"), ("brother", "brother"),
            ("relative (other)", "relative"), ("left at door", "doorstep"),
            ("someone else", "someone_else"),
        ):
            out = derive_structured_fields(
                _policy(), {"m_handover_recipient": canonical}
            )
            assert out["hand_over_to"] == expected, canonical
            assert out["hand_over_product"] == "Yes", canonical

    def test_not_handed_over_is_no_with_no_recipient(self):
        out = derive_structured_fields(
            _policy(), {"m_handover_recipient": "not handed over"}
        )
        assert out["hand_over_product"] == "No"
        assert out["hand_over_to"] is None

    def test_missing_slots_stay_none_but_keys_are_stable(self):
        out = derive_structured_fields(_policy(), {})
        assert set(out) == {f["name"] for f in MDND_FIELDS}
        assert all(value is None for value in out.values())

    def test_slot_lookup_is_case_insensitive(self):
        out = derive_structured_fields(
            _policy(), {"m_handover_recipient": "Guard / Security"}
        )
        assert out["hand_over_to"] == "security_guard"

    def test_no_policy_or_no_fields_yields_empty(self):
        assert derive_structured_fields(None, {"a": "b"}) == {}
        assert derive_structured_fields(BotGoalPolicy(), {"a": "b"}) == {}


class TestMergeWithAnalyst:
    def test_workflow_slot_beats_a_conflicting_analyst_value(self):
        fields, sources = merge_structured_fields(
            _policy(),
            {"m_called_customer": "no (did not call)"},
            {"call_customer": "Yes", "call_cx": "yes"},
        )
        assert fields["call_customer"] == "No"
        assert sources["call_customer"] == "workflow"
        # The analyst fills only what the flow never collected — normalized.
        assert fields["call_cx"] == "Yes"
        assert sources["call_cx"] == "analysis"

    def test_analyst_values_are_clamped_to_the_vocabulary(self):
        fields, sources = merge_structured_fields(
            _policy(), {},
            {"hand_over_to": "the neighbour", "call_customer": "probably",
             "reach_customer_location": "No", "hand_over_product": "yes"},
        )
        assert fields["hand_over_to"] is None          # not an option
        assert fields["call_customer"] is None         # not yes/no
        assert fields["reach_customer_location"] == "No"
        assert fields["hand_over_product"] == "Yes"
        assert "hand_over_to" not in sources

    def test_allow_llm_false_ignores_the_analyst(self):
        fields, sources = merge_structured_fields(
            _policy(), {}, {"guard_name": "Suresh"}
        )
        assert fields["guard_name"] is None
        assert "guard_name" not in sources

    def test_analyst_keys_match_case_insensitively(self):
        fields, _ = merge_structured_fields(
            _policy(), {}, {"Call_CX": "No"}
        )
        assert fields["call_cx"] == "No"


class TestPolicyCompilation:
    def test_summary_fields_alone_keep_the_derived_live_policy(self):
        policy = compile_goal_policy(
            {"summaryFields": MDND_FIELDS}, bot_name="Zepto MDND Support",
            intents=[{"name": "mdnd_concern"}, {"name": "policy_question"}],
            system_prompt="You are Kavya.",
        )
        assert policy.source == "derived"
        assert policy.allowed_topics == ["mdnd_concern", "policy_question"]
        assert [f.name for f in policy.summary_fields] == [
            f["name"] for f in MDND_FIELDS
        ]

    def test_authored_policy_with_summary_fields_is_configured(self):
        policy = compile_goal_policy(
            {"role": "collector", "summaryFields": MDND_FIELDS[:1]},
            bot_name="x",
        )
        assert policy.source == "configured"
        assert policy.summary_fields[0].name == "call_customer"

    def test_spec_validation_clamps_junk(self):
        policy = BotGoalPolicy.model_validate({"summaryFields": [
            {"name": "  weird ", "type": "banana", "options": ["a", "a", ""],
             "values": {" Guard ": "security_guard", "": "x"}},
        ]})
        spec = policy.summary_fields[0]
        assert spec.name == "weird"
        assert spec.type == "text"
        assert spec.options == ["a"]
        assert spec.values == {"guard": "security_guard"}


class TestAnalystPrompt:
    def test_prompt_lists_fields_with_allowed_values(self):
        block = summary_fields_prompt_block(_policy())
        assert '"call_customer": "Yes" | "No" | null' in block
        assert '"security_guard"' in block and '"doorstep"' in block
        system = _build_system(_policy(), reference=datetime(2026, 9, 3, tzinfo=timezone.utc))
        assert '"structured_fields"' in system

    def test_prompt_is_silent_without_fields(self):
        assert summary_fields_prompt_block(BotGoalPolicy()) == ""
        system = _build_system(BotGoalPolicy(), reference=datetime.now(timezone.utc))
        assert "structured_fields" not in system

    def test_parse_analysis_accepts_structured_fields(self):
        analysis = parse_analysis({
            "summary": "x", "structuredFields": {"call_cx": "Yes", "k": None,
                                                 "bad": {"nested": 1}},
        })
        assert analysis.structured_fields == {"call_cx": "Yes", "k": None,
                                              "bad": None}


class TestFallback:
    def test_fallback_reports_workflow_slots_without_an_llm(self):
        analysis = fallback_analysis(
            final_state={"disposition": "", "workflow_slots": {
                "m_reached_location": "no (did not reach the location)",
                "m_handover_recipient": "left at door",
            }},
            policy=_policy(),
        )
        assert analysis.source == "fallback"
        assert analysis.structured_fields["reach_customer_location"] == "No"
        assert analysis.structured_fields["hand_over_to"] == "doorstep"
        assert analysis.structured_fields["call_cx"] is None
        assert analysis.structured_field_sources == {
            "reach_customer_location": "workflow",
            "hand_over_product": "workflow",
            "hand_over_to": "workflow",
        }
