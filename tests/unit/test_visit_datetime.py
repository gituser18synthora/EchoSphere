"""shared.orchestration.visit_datetime — spoken day/time → calendar check.

Clock for every case: Monday 2026-10-19 12:30 IST (Asia/Kolkata)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from shared.orchestration.visit_datetime import (
    parse_day, parse_time, rejection_reply, validate_ask, validate_visit,
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
    ])
    def test_exact(self, text, minutes):
        result = parse_time(text)
        assert (result.kind, result.candidates) == ("exact", (minutes,))

    def test_bare_hour_is_ambiguous(self):
        result = parse_time("4 baje")
        assert result.kind == "ambiguous" and result.candidates == (240, 960)

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
        ("आज", "4 baje", (False, "time_ambiguous")),   # 4 AM is past, 4 PM is ahead
        ("आज", "2 baje", (False, "time_ambiguous")),
        ("आज", "25 baje", (False, "time_invalid")),
        ("आज", "शाम 4 बजे", (True, None)),
        ("आज", "रात 9 बजे", (True, None)),
        ("आज", "शाम", (True, None)),
        ("आज", "", (True, None)),                      # no time given: nothing to refuse
        ("कल", "4 बजे", (True, None)),                 # not today: a bare hour is fine
        ("कल", "सुबह 9 बजे", (True, None)),
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

    def test_both_readings_ahead_is_fine(self):
        early = datetime(2026, 10, 19, 1, 0, tzinfo=IST)
        assert validate_visit("आज", "4 baje", early) == (True, None)

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
