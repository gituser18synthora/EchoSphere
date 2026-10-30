"""Structured post-call summary fields: config-driven derivation from the
final workflow slots, analyst fill-in for the rest, vocabulary clamping, and
the rule that summary-only goal policies never change live behavior."""

from datetime import datetime, timezone

from shared.orchestration.goal_engine import BotGoalPolicy, compile_goal_policy
from shared.post_call.analyzer import _build_system, fallback_analysis
from shared.post_call.schema import parse_analysis
from shared.post_call.structured import (
    derive_structured_fields,
    display_structured_fields,
    display_value,
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


DATE_FIELDS = [
    {"name": "appointment_booked", "type": "yes_no", "source": "visit_status",
     "values": {"booked": "Yes", "*": "No"}, "allowLlm": False},
    {"name": "appointment_date", "type": "date", "source": "visit_date_iso", "allowLlm": False,
     "requires": {"appointment_booked": ["Yes"]}},
    {"name": "appointment_time", "type": "time", "source": "visit_time_24h",
     "timezone": "Asia/Kolkata", "allowLlm": True},
    {"name": "note", "type": "text", "source": "note"},
]
BOOKED = {"visit_status": "booked"}


def _date_policy() -> BotGoalPolicy:
    return BotGoalPolicy.model_validate({"summaryFields": DATE_FIELDS})


class TestDateTimeFields:
    def test_normalized_slot_values_pass_through(self):
        fields = derive_structured_fields(_date_policy(), {**BOOKED, "visit_date_iso": "2026-10-20",
                                                           "visit_time_24h": "17:00"})
        assert (fields["appointment_date"], fields["appointment_time"]) == ("2026-10-20", "17:00")

    def test_spoken_or_malformed_values_are_not_determined(self):
        for bad_date, bad_time in [("कल", "शाम 5 बजे"), ("2026-02-31", "24:00"), ("20 Oct 2026", "5 PM"),
                                   ("2026-10-20T17:00", "7:05"), ("", "")]:
            fields = derive_structured_fields(_date_policy(), {**BOOKED, "visit_date_iso": bad_date,
                                                               "visit_time_24h": bad_time})
            assert fields["appointment_date"] is None and fields["appointment_time"] is None, (bad_date, bad_time)

    def test_window_words_are_values(self):
        fields = derive_structured_fields(_date_policy(), {**BOOKED, "visit_date_iso": "2026-10-20",
                                                           "visit_time_24h": " Evening "})
        assert fields["appointment_time"] == "evening"

    def test_requires_still_blanks_a_normalized_date(self):
        fields = derive_structured_fields(_date_policy(), {"visit_status": "date_pending",
                                                           "visit_date_iso": "2026-10-20"})
        assert fields["appointment_booked"] == "No" and fields["appointment_date"] is None

    def test_analyst_fill_is_clamped_to_the_shape(self):
        fields, sources = merge_structured_fields(
            _date_policy(), BOOKED, {"appointment_time": "5 PM", "appointment_date": "2026-10-20"})
        assert fields["appointment_time"] is None            # not in the normalized shape
        assert fields["appointment_date"] is None            # allowLlm is off
        fields, sources = merge_structured_fields(_date_policy(), BOOKED, {"appointment_time": "17:00"})
        assert fields["appointment_time"] == "17:00" and sources["appointment_time"] == "analysis"
        fields, sources = merge_structured_fields(
            _date_policy(), {**BOOKED, "visit_time_24h": "evening"}, {"appointment_time": "17:00"})
        assert fields["appointment_time"] == "evening" and sources["appointment_time"] == "workflow"

    def test_display_forms(self):
        assert display_value("date", "2026-10-20") == "20 Oct 2026"
        assert display_value("date", "2026-01-05") == "5 Jan 2026"
        assert display_value("time", "17:00", "Asia/Kolkata") == "05:00 PM IST"
        assert display_value("time", "00:05", "Asia/Kolkata") == "12:05 AM IST"
        assert display_value("time", "12:30") == "12:30 PM"
        assert display_value("time", "evening", "Asia/Kolkata") == "Evening (time window)"
        assert display_value("date", "कल") is None and display_value("time", "5 PM") is None
        assert display_value("text", "2026-10-20") is None and display_value("date", None) is None

    def test_display_map_follows_the_raw_specs(self):
        fields = {"appointment_booked": "Yes", "appointment_date": "2026-10-20",
                  "appointment_time": "17:00", "note": "2026-10-20"}
        assert display_structured_fields(DATE_FIELDS, fields) == {
            "appointment_date": "20 Oct 2026", "appointment_time": "05:00 PM IST"}
        assert display_structured_fields(DATE_FIELDS, {"appointment_time": "morning"}) == {
            "appointment_time": "Morning (time window)"}
        assert display_structured_fields(DATE_FIELDS, {"appointment_date": None}) == {}
        assert display_structured_fields([None, "x", {"type": "date"}], fields) == {}

    def test_spec_keeps_the_new_types_and_timezone(self):
        policy = _date_policy()
        by_name = {f.name: f for f in policy.summary_fields}
        assert by_name["appointment_date"].type == "date" and by_name["appointment_date"].timezone == ""
        assert by_name["appointment_time"].type == "time" and by_name["appointment_time"].timezone == "Asia/Kolkata"
        weird = BotGoalPolicy.model_validate({"summaryFields": [{"name": "x", "type": "datetime", "timezone": " UTC "}]})
        assert weird.summary_fields[0].type == "text" and weird.summary_fields[0].timezone == "UTC"

    def test_prompt_block_describes_the_shape(self):
        block = summary_fields_prompt_block(_date_policy())
        assert '"appointment_date": "YYYY-MM-DD" | null' in block
        assert '"appointment_time": "HH:MM" (24-hour) | "morning" | "afternoon" | "evening" | "night" | null' in block
