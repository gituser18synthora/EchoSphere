"""shared.orchestration.visit_datetime — spoken day/time → calendar check.

Clock for every case: Monday 2026-10-19 12:30 IST (Asia/Kolkata)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from shared.orchestration.visit_datetime import (
    compose_time, parse_day, parse_time, rejection_reply, validate_ask, validate_visit,
)

IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 10, 19)                    # a Monday
NOW = datetime(2026, 10, 19, 12, 30, tzinfo=IST)


class TestParseDay:
    @pytest.mark.parametrize("text, expected", [
        ("आज", date(2026, 10, 19)), ("aaj", date(2026, 10, 19)), ("today", date(2026, 10, 19)),
        ("कल", date(2026, 10, 20)), ("kal", date(2026, 10, 20)), ("tomorrow", date(2026, 10, 20)),
        ("परसों", date(2026, 10, 21)), ("parso", date(2026, 10, 21)),
        ("day after tomorrow", date(2026, 10, 21)),
        ("Monday", date(2026, 10, 26)),        # said on a Monday → next Monday
        ("सोमवार", date(2026, 10, 26)), ("mangalvar", date(2026, 10, 20)), ("Sunday", date(2026, 10, 25)),
        ("25 tarikh", date(2026, 10, 25)), ("10 tarikh", date(2026, 11, 10)),  # passed → next month
        ("pachees tarikh", date(2026, 10, 25)), ("das tarikh", date(2026, 11, 10)),
        ("25 October", date(2026, 10, 25)), ("October 25", date(2026, 10, 25)),
        ("25th of October", date(2026, 10, 25)), ("25 अक्टूबर", date(2026, 10, 25)),
        ("5 January", date(2027, 1, 5)),       # > 6 months back → next year
        ("25/10", date(2026, 10, 25)), ("25/10/2026", date(2026, 10, 25)), ("2026-10-25", date(2026, 10, 25)),
    ])
    def test_future_or_today_resolves(self, text, expected):
        result = parse_day(text, TODAY)
        assert (result.kind, result.value) == ("ok", expected)

    @pytest.mark.parametrize("text", ["10 October", "16 October", "yesterday is not parsed", "18/10"])
    def test_past_dates(self, text):
        result = parse_day(text, TODAY)
        assert result.kind in ("past", "unknown")
        if result.kind == "past":
            assert result.value < TODAY

    @pytest.mark.parametrize("text", ["31 February", "32 October", "29 February", "40/10", "25/13"])
    def test_impossible_dates(self, text):
        assert parse_day(text, TODAY).kind == "invalid"

    def test_unparseable_is_unknown_not_rejected(self):
        for text in ("agle hafte", "next week", "jaldi", ""):
            assert parse_day(text, TODAY).kind == "unknown"

    def test_devanagari_digits(self):
        assert parse_day("२५ अक्टूबर", TODAY).value == date(2026, 10, 25)


class TestParseTime:
    @pytest.mark.parametrize("text, minutes", [
        ("सुबह 9 बजे", 540), ("subah 10 baje", 600), ("10 in the morning", 600),
        ("दोपहर 2 बजे", 840), ("dopahar 12 baje", 720), ("शाम 5 बजे", 1020), ("4 baje sham", 960),
        ("रात 9 बजे", 1260), ("4 PM", 960), ("4 p.m.", 960), ("12 PM", 720), ("12 AM", 0),
        ("16:30", 990), ("12 बजे", 720), ("noon", 720), ("4:15 pm", 975),
        # spoken numbers, as Sarvam writes them on live calls
        ("सुबह नौ बजे", 540), ("सुबह दस बजे", 600), ("शाम पाँच बजे", 1020), ("रात नौ बजे", 1260),
        ("subah das baje", 600), ("sham paanch baje", 1020), ("nau baje subah", 540),
        ("कल दोपहर बारह बजे", 720), ("दोपहर ढाई बजे", 870), ("शाम साढ़े चार बजे", 990),
        ("सुबह पौने पाँच बजे", 285), ("subah sawa das baje", 615), ("शाम 4 baje", 960),
        ("4 baje PM", 960), ("four in the evening", 960), ("half past four PM", 990),
        ("four thirty in the morning", 270), ("half past four in the evening", 990),
        ("at four pm", 960), ("ten in the morning", 600),
        ("शाम के 5 बजे", 1020), ("सुबह के 10 बजे", 600), ("sham ke 5 baje", 1020),
        ("शाम के साढ़े पाँच बजे", 1050), ("subah ke das baje", 600),
    ])
    def test_exact(self, text, minutes):
        result = parse_time(text)
        assert (result.kind, result.candidates) == ("exact", (minutes,))

    @pytest.mark.parametrize("text, candidates", [
        ("4 baje", (240, 960)), ("चार बजे", (240, 960)), ("char baje", (240, 960)),
        ("ढाई बजे", (150, 870)), ("dhai baje", (150, 870)), ("डेढ़ बजे", (90, 810)),
        ("साढ़े चार बजे", (270, 990)), ("sadhe char baje", (270, 990)), ("पौने पाँच बजे", (285, 1005)),
        ("सवा दस बजे", (615, 1335)), ("half past four", (270, 990)), ("quarter past four", (255, 975)),
        ("quarter to five", (285, 1005)), ("four thirty", (270, 990)), ("at four", (240, 960)),
    ])
    def test_bare_hours_and_fractions_are_ambiguous(self, text, candidates):
        result = parse_time(text)
        assert (result.kind, result.candidates) == ("ambiguous", candidates)

    def test_bare_am_pm_is_a_period(self):
        assert parse_time("PM").kind == "period" and parse_time("PM").period == "p"
        assert parse_time("a.m.").period == "a"

    def test_compose_clarification_with_parked_hour(self):
        assert compose_time("शाम", "4 baje") == "शाम 4 baje"
        assert compose_time("evening", "half past four") == "half past four in the evening"
        assert compose_time("in the morning", "four thirty") == "four thirty in the morning"
        assert compose_time("sham", "4 baje") == "sham 4 baje"
        assert compose_time("PM", "4 baje") == "4 baje PM"
        assert compose_time("शाम 5 बजे", "4 baje") is None      # a full time replaces, no compose
        assert compose_time("haan", "4 baje") is None
        assert parse_time(compose_time("दोपहर", "ढाई बजे")).candidates == (870,)

    def test_period_alone(self):
        assert parse_time("शाम").kind == "period" and parse_time("शाम").period == "evening"
        assert parse_time("morning").period == "morning"

    def test_invalid_and_unknown(self):
        assert parse_time("25 baje").kind == "invalid"
        assert parse_time("4:70").kind == "invalid"
        assert parse_time("jaldi").kind == "unknown"
        assert parse_time("").kind == "unknown"


class TestValidateVisit:
    @pytest.mark.parametrize("day, time, expected", [
        ("आज", "सुबह 9 बजे", (False, "time_past")),
        ("आज", "दोपहर 12 बजे", (False, "time_past")),
        ("आज", "सुबह", (False, "time_past")),          # the morning is over at 12:30
        ("आज", "4 baje", (False, "time_ambiguous")),
        ("आज", "2 baje", (False, "time_ambiguous")),
        ("आज", "सुबह नौ बजे", (False, "time_past")),      # spoken number, same rule
        ("कल", "सुबह नौ बजे", (True, None)),
        ("आज", "25 baje", (False, "time_invalid")),
        ("आज", "शाम 4 बजे", (True, None)),
        ("आज", "रात 9 बजे", (True, None)),
        ("आज", "शाम", (True, None)),
        ("आज", "", (True, None)),                      # no time given: nothing to refuse
        ("कल", "4 बजे", (False, "time_ambiguous")),     # a bare hour is clarified on any day
        ("कल", "साढ़े चार बजे", (False, "time_ambiguous")),
        ("कल", "half past four", (False, "time_ambiguous")),
        ("कल", "सुबह 9 बजे", (True, None)),
        ("कल", "शाम", (True, None)),                   # a period alone is what was said, not a guess
        ("कल", "jaldi", (False, "time_unclear")),      # captured but not understood
        ("10 October", "4 PM", (False, "day_past")),
        ("31 February", "", (False, "day_invalid")),
        ("agle hafte", "4 baje", (False, "day_unclear")),  # no specific day → ask for one
        ("next week", "", (False, "day_unclear")),
        ("", "", (True, None)),                        # no day yet: nothing to judge
    ])
    def test_rules(self, day, time, expected):
        assert validate_visit(day, time, NOW) == expected

    def test_unparsed_day_can_be_accepted_by_config(self):
        assert validate_visit("agle hafte", "", NOW, accept_unparsed_day=True) == (True, None)

    def test_both_readings_ahead_is_still_ambiguous(self):
        early = datetime(2026, 10, 19, 1, 0, tzinfo=IST)
        assert validate_visit("आज", "4 baje", early) == (False, "time_ambiguous")

    def test_both_readings_past(self):
        late = datetime(2026, 10, 19, 23, 30, tzinfo=IST)
        assert validate_visit("आज", "11 baje", late) == (False, "time_past")


CONFIG = {
    "type": "future_datetime", "timezone": "Asia/Kolkata",
    "dayVariables": ["visit_day_2", "visit_day"], "timeVariables": ["visit_time_2", "visit_time"],
    "replyByLanguage": {"day_past": {"hi": "निकल गई", "en": "passed"}},
}


class TestValidateAsk:
    def test_clears_only_the_offending_slots(self):
        verdict = validate_ask(CONFIG, {"visit_day_2": "10 October", "visit_day": "10 October",
                                        "visit_time_2": "4 PM", "visit_time": "4 PM"}, now=NOW)
        assert not verdict.ok and verdict.reason == "day_past"
        assert verdict.clear == ("visit_day_2", "visit_day")
        verdict = validate_ask(CONFIG, {"visit_day": "आज", "visit_time": "सुबह 9 बजे"}, now=NOW)
        assert verdict.reason == "time_past" and verdict.clear == ("visit_time_2", "visit_time")

    def test_ambiguous_hour_is_parked_and_composed_with_the_clarification(self):
        slots = {"visit_day_2": "parso", "visit_day": "parso", "visit_time_2": "4 baje", "visit_time": "4 baje"}
        verdict = validate_ask(CONFIG, slots, now=NOW)
        assert verdict.reason == "time_ambiguous" and verdict.clear == ("visit_time_2", "visit_time")
        assert slots["visit_time_2_pending"] == "4 baje"
        for name in verdict.clear:
            slots.pop(name)
        # the caller answers with the period only → composed into every time slot
        slots.update({"visit_time_2": "शाम", "visit_time": "शाम"})
        assert validate_ask(CONFIG, slots, now=NOW).ok
        assert slots["visit_time_2"] == slots["visit_time"] == "शाम 4 baje"
        assert "visit_time_2_pending" not in slots
        # a full time after a parked hour replaces it and drops the park
        slots = {"visit_day": "कल", "visit_time": "4 baje"}
        validate_ask({**CONFIG, "dayVariables": ["visit_day"], "timeVariables": ["visit_time"]}, slots, now=NOW)
        slots.pop("visit_time"); slots["visit_time"] = "सुबह 10 बजे"
        assert validate_ask({**CONFIG, "dayVariables": ["visit_day"], "timeVariables": ["visit_time"]}, slots, now=NOW).ok
        assert slots["visit_time"] == "सुबह 10 बजे" and "visit_time_pending" not in slots

    def test_first_filled_variable_is_the_one_checked(self):
        verdict = validate_ask(CONFIG, {"visit_day_2": "कल", "visit_day": "10 October"}, now=NOW)
        assert verdict.ok

    def test_unknown_type_or_missing_config_is_a_pass(self):
        assert validate_ask({"type": "something_else"}, {"visit_day": "10 October"}, now=NOW).ok
        assert validate_ask({}, {"visit_day": "10 October"}, now=NOW).ok

    def test_unclear_day_clears_the_day_slots_unless_accepted(self):
        verdict = validate_ask(CONFIG, {"visit_day": "agle hafte", "visit_time": "4 PM"}, now=NOW)
        assert verdict.reason == "day_unclear" and verdict.clear == ("visit_day_2", "visit_day")
        assert validate_ask({**CONFIG, "acceptUnparsedDay": True},
                            {"visit_day": "agle hafte"}, now=NOW).ok
        assert validate_ask(CONFIG, {"visit_time": "4 PM"}, now=NOW).ok  # no day yet

    def test_reply_language_and_defaults(self):
        assert rejection_reply(CONFIG, "day_past", "hi-IN") == "निकल गई"
        assert rejection_reply(CONFIG, "day_past", "en-IN") == "passed"
        assert "समय" in rejection_reply(CONFIG, "time_past", "hi-IN")   # built-in default
        assert "time" in rejection_reply(CONFIG, "time_past", "en-US")
        assert "दिन" in rejection_reply(CONFIG, "day_unclear", "hi-IN")
        assert "which day" in rejection_reply(CONFIG, "day_missing", "en-IN")
