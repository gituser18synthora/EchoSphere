"""shared.orchestration.visit_datetime — spoken day/time → calendar check.

Clock for every case: Monday 2026-10-19 12:30 IST (Asia/Kolkata)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import shared.orchestration.visit_datetime as vd
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


class TestResolveVisit:
    @pytest.mark.parametrize("day, time, iso, clock", [
        ("कल", "शाम के 5 बजे", "2026-10-20", "17:00"),
        ("tomorrow", "4 PM", "2026-10-20", "16:00"),
        ("kal", "subah das baje", "2026-10-20", "10:00"),
        ("परसों", "दोपहर ढाई बजे", "2026-10-21", "14:30"),
        ("कल", "साढ़े चार बजे PM", "2026-10-20", "16:30"),
        ("आज", "रात 9 बजे", "2026-10-19", "21:00"),
        ("somvar", "", "2026-10-26", ""),
        ("25 October", "16:30", "2026-10-25", "16:30"),
        ("कल", "noon", "2026-10-20", "12:00"),
        ("20/10/2026", "9 AM", "2026-10-20", "09:00"),
    ])
    def test_resolved_values(self, day, time, iso, clock):
        r = vd.resolve_visit(day, time, NOW)
        assert r.ok and (r.iso_date, r.normalized_time) == (iso, clock)

    def test_a_window_stays_a_window(self):
        assert vd.resolve_visit("कल", "शाम", NOW).normalized_time == "evening"
        assert vd.resolve_visit("tomorrow", "in the morning", NOW).normalized_time == "morning"
        assert vd.normalize_time(vd.TimeResult("period", period="p")) == ""   # a bare "PM" is no window

    def test_nothing_is_resolved_without_a_day(self):
        r = vd.resolve_visit("", "4 PM", NOW)
        assert r.ok and r.iso_date == "" and r.normalized_time == ""
        r = vd.resolve_visit("agle hafte", "", NOW, accept_unparsed_day=True)
        assert r.ok and r.iso_date == ""

    def test_rejections_carry_the_reason_and_no_values(self):
        r = vd.resolve_visit("10 October", "", NOW)
        assert (r.ok, r.reason, r.iso_date) == (False, "day_past", "")
        r = vd.resolve_visit("कल", "4 baje", NOW)
        assert (r.ok, r.reason, r.normalized_time) == (False, "time_ambiguous", "")
        for args in (("10 October", ""), ("कल", "4 baje"), ("आज", "सुबह 9 बजे"), ("31 February", "")):
            r = vd.resolve_visit(*args, NOW)
            assert vd.validate_visit(*args, NOW) == (r.ok, r.reason)

    def test_relative_days_follow_the_given_clock(self):
        after_midnight = datetime(2026, 10, 20, 0, 10, tzinfo=IST)
        assert vd.resolve_visit("कल", "", after_midnight).iso_date == "2026-10-21"
        assert vd.resolve_visit("आज", "", after_midnight).iso_date == "2026-10-20"
        assert vd.resolve_visit("कल", "", NOW).iso_date == "2026-10-20"


NORMALIZED = {**CONFIG, "dayVariables": ["visit_day"], "timeVariables": ["visit_time"],
              "normalizedDayVariable": "visit_date_iso", "normalizedTimeVariable": "visit_time_24h"}


class TestNormalizedCompanions:
    def test_written_on_acceptance_without_touching_the_spoken_slots(self):
        slots = {"visit_day": "कल", "visit_time": "शाम के 5 बजे"}
        assert validate_ask(NORMALIZED, slots, now=NOW).ok
        assert slots == {"visit_day": "कल", "visit_time": "शाम के 5 बजे",
                         "visit_date_iso": "2026-10-20", "visit_time_24h": "17:00"}

    def test_day_alone_writes_an_empty_time_even_over_a_stale_one(self):
        slots = {"visit_day": "परसों", "visit_time_24h": "10:00"}
        assert validate_ask(NORMALIZED, slots, now=NOW).ok
        assert (slots["visit_date_iso"], slots["visit_time_24h"]) == ("2026-10-21", "")

    def test_window_and_english(self):
        slots = {"visit_day": "कल", "visit_time": "शाम"}
        assert validate_ask(NORMALIZED, slots, now=NOW).ok and slots["visit_time_24h"] == "evening"
        slots = {"visit_day": "Monday", "visit_time": "4 PM"}
        assert validate_ask(NORMALIZED, slots, now=NOW).ok
        assert (slots["visit_date_iso"], slots["visit_time_24h"]) == ("2026-10-26", "16:00")

    def test_cleared_on_every_rejection(self):
        slots = {"visit_day": "10 October", "visit_time": "4 PM",
                 "visit_date_iso": "2026-10-20", "visit_time_24h": "10:00"}
        verdict = validate_ask(NORMALIZED, slots, now=NOW)
        assert verdict.reason == "day_past"
        assert verdict.clear == ("visit_day", "visit_date_iso", "visit_time_24h")
        verdict = validate_ask(NORMALIZED, {"visit_day": "कल", "visit_time": "4 baje"}, now=NOW)
        assert verdict.reason == "time_ambiguous"
        assert verdict.clear == ("visit_time", "visit_date_iso", "visit_time_24h")

    def test_clarification_completes_the_normalized_time(self):
        slots = {"visit_day": "कल", "visit_time": "4 baje"}
        verdict = validate_ask(NORMALIZED, slots, now=NOW)
        for name in verdict.clear:
            slots.pop(name, None)
        slots["visit_time"] = "शाम"
        assert validate_ask(NORMALIZED, slots, now=NOW).ok
        assert (slots["visit_time"], slots["visit_time_24h"], slots["visit_date_iso"]) == \
            ("शाम 4 baje", "16:00", "2026-10-20")

    def test_nothing_without_the_config_keys_or_a_day(self):
        slots = {"visit_day": "कल", "visit_time": "4 PM"}
        assert validate_ask(CONFIG, slots, now=NOW).ok
        assert "visit_date_iso" not in slots and "visit_time_24h" not in slots
        slots = {"visit_time": "4 PM"}
        assert validate_ask(NORMALIZED, slots, now=NOW).ok and slots == {"visit_time": "4 PM"}


LIVE_UTTERANCE = "कल कर दो शाम के पाँच बजे।"   # cv_0b1e4d99ac69 (DEMO LIVE, 2026-10-19)


class TestExtractTime:
    """The parser chooses among ALL candidate spans; the regex only finds them."""

    def test_live_regression_the_verb_particle_is_not_an_hour(self):
        # Leftmost regex match was "दो शाम" ("कर दो" + the period) → booked 2 PM.
        assert vd._TIME_CAPTURE_RE.search(LIVE_UTTERANCE).group(0) == "दो शाम"
        assert vd.extract_time(LIVE_UTTERANCE) == "शाम के पाँच बजे"
        assert vd.parse_time("शाम के पाँच बजे").candidates == (17 * 60,)

    @pytest.mark.parametrize("text, expected", [
        ("बता दो सुबह", "सुबह"),                        # verb particle + period: window only
        ("ek baar sham ko aa jao", "sham"),               # "ek" is a determiner here
        ("कल सुबह नौ बजे कर दो", "सुबह नौ बजे"),
        ("kal sham ke 5 baje", "sham ke 5 baje"),
        ("दोपहर ढाई बजे", "दोपहर ढाई बजे"),
        ("कल साढ़े चार", "साढ़े चार"),                     # bare fraction hour → still ambiguous
        ("25 October शाम 6 बजे", "शाम 6 बजे"),           # the date's number is not a time
        ("four thirty in the morning", "four thirty in the morning"),
        ("tomorrow at four", "four"),
        ("कल शाम", "शाम"),
        ("शाम 5 बजे, कोई दिक्कत नहीं", "शाम 5 बजे"),    # "नहीं" not adjacent: no negation
        ("5 बजे शाम को, हाँ शाम 5 बजे", "5 बजे शाम को"), # same time twice is not a conflict; the fuller mention wins
        ("हाँ ठीक है", None),
    ])
    def test_choice_among_candidates(self, text, expected):
        assert vd.extract_time(text) == expected

    @pytest.mark.parametrize("text, expected", [
        ("5 बजे नहीं 6 बजे", "6 बजे"),
        ("not at 5, at 6 pm", "6 pm"),
        ("subah nahi, sham ko", "sham"),
        ("सुबह 10 बजे, शाम को नहीं", "सुबह 10 बजे"),      # "शाम को नहीं" negates the window
    ])
    def test_negation_and_corrections(self, text, expected):
        assert vd.extract_time(text) == expected

    def test_bare_hour_and_period_said_apart_are_composed(self):
        assert vd.extract_time("कल 5 बजे आ जाना शाम को") == "शाम 5 बजे"
        assert vd.parse_time(vd.extract_time("कल 5 बजे आ जाना शाम को")).candidates == (17 * 60,)
        assert vd.extract_time("five o'clock tomorrow, evening") == "five o'clock in the evening"

    @pytest.mark.parametrize("text", ["5 बजे या 6 बजे", "सुबह या शाम", "4 PM or 6 PM", "सुबह 10 बजे या शाम 5 बजे", "कल 5 बजे या 6 बजे"])
    def test_two_readings_that_disagree_are_kept_as_said_for_validation_to_refuse(self, text):
        kept = vd.extract_time(text)
        assert kept and kept in text and vd.parse_time(kept).kind == "unknown"
        assert vd.validate_visit("कल", kept, NOW) == (False, "time_unclear")

    def test_the_same_time_said_twice_is_not_a_conflict(self):
        assert vd.extract_time("5 बजे आ जाना, हाँ शाम 5 बजे") == "शाम 5 बजे"
        assert vd.extract_time("at 5, no, at 6 pm") == "6 pm"

    @pytest.mark.parametrize("text, kind", [
        ("रात 12 बजे", "unknown"), ("कल रात 12 बजे", "unknown"), ("12 AM", "exact"), ("दोपहर 12 बजे", "exact"),
        ("4 30", "unknown"), ("four forty five", "ambiguous"), ("ten to five", "unknown"), ("रात 11 बजे", "exact"),
    ])
    def test_midnight_and_two_hours_are_not_understood(self, text, kind):
        assert vd.parse_time(text).kind == kind
        if text == "four forty five":
            assert vd.parse_time(text).candidates == (4 * 60 + 45, 16 * 60 + 45)

    def test_a_refused_full_reading_never_degrades_into_its_sub_span(self):
        assert vd.extract_time("कल रात 12 बजे") == "रात 12 बजे"          # not "12 बजे" (noon)
        assert vd.validate_visit("कल", vd.extract_time("कल रात 12 बजे"), NOW) == (False, "time_unclear")
        assert vd.extract_time("रात 12:30") == "रात 12:30"


class TestExtractDay:
    @pytest.mark.parametrize("text, expected", [
        (LIVE_UTTERANCE, "कल"),
        ("कल नहीं परसों", "परसों"),
        ("not tomorrow, Monday", "Monday"),
        ("आज नहीं हो पाएगा, कल", "कल"),
        ("नहीं, कल कर दो", "कल"),                        # Hindi नहीं before = the answer "no"
        ("Monday, no problem", "Monday"),                # bare English "no" after is not a negation…
        ("Tomorrow, no, make it Monday", "Monday"),      # …unless another day follows: a correction
        ("कल तो नहीं", None),
        ("agle hafte", "agle hafte"),                    # kept so validation asks for a specific day
        ("next week Monday", "Monday"),
        ("इस हफ्ते शुक्रवार", "शुक्रवार"),
        ("25 tarikh subah 10 baje", "25 tarikh"),
        ("pachees tarikh", "pachees tarikh"),
        ("day after tomorrow", "day after tomorrow"),
        ("10 October 4 PM", "10 October"),
        ("25 अक्टूबर को", "25 अक्टूबर"),
        ("20/10/2026", "20/10/2026"),
        ("pata nahi", None),
    ])
    def test_choice_among_candidates(self, text, expected):
        assert vd.extract_day(text, TODAY) == expected

    @pytest.mark.parametrize("text", ["कल या परसों", "Monday or Tuesday", "kal ya parso"])
    def test_two_different_days_capture_nothing(self, text):
        assert vd.extract_day(text, TODAY) is None

    def test_relocated_capture_regex_still_reads_the_connector(self):
        regex = vd.time_capture_regex()
        assert "के" in regex and "ke" in regex and "[\\u0900-\\u097f]" in regex
