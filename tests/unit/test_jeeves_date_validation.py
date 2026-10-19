"""Jeeves Airtel uninstallation bot: visit day/time validation (workflow
stages 2-5, jeeves/setup/01_structured_summary.py → v18).

The four date-capturing asks declare ``validation: future_datetime`` in
Asia/Kolkata. A past or impossible day, or a past/ambiguous time for today,
is refused with the configured line and asked again while valid information
already given is kept; unparseable phrasing is accepted as spoken. For valid
inputs the flow speaks exactly what v13 speaks (golden comparison over the 15
structured-summary scenarios). Stage 5 adds the normalized pickup date/time
companions (TestNormalizedSummary) without changing a spoken word."""

import runpy
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.visit_datetime as vd
import shared.orchestration.workflow_engine as wfe
from backend.serializers import serialize_conversation_memory
from shared.orchestration.goal_engine import compile_goal_policy
from shared.post_call.structured import derive_structured_fields, display_structured_fields
from tests.unit.test_jeeves_structured_summary import (
    BOT, CONTEXT, SCENARIOS as SUMMARY_SCENARIOS, TENANT, WORKFLOW, _snapshot,
)

IST = ZoneInfo("Asia/Kolkata")
SETUP = runpy.run_path("jeeves/setup/01_structured_summary.py")
V13 = SETUP["upgraded_definition"](13, stage=1)
V17 = SETUP["upgraded_definition"](17, stage=4)   # markers + validation + time capture + wording
V14 = SETUP["upgraded_definition"](18)            # + normalized date/time companions (target)
# The cases below assert the pickup day/time AS SPOKEN (stage ≤ 4 fields);
# the normalized fields the bot now reports are covered in TestNormalizedSummary.
POLICY = compile_goal_policy({"summaryFields": SETUP["summary_fields"](4)},
                             bot_name="Customer Support", system_prompt="")
NORMALIZED_POLICY = compile_goal_policy({"summaryFields": SETUP["SUMMARY_FIELDS"]},
                                        bot_name="Customer Support", system_prompt="")

MONDAY_NOON = datetime(2026, 10, 19, 12, 30, tzinfo=IST)
OPEN, YES, ADDR_YES = "हाँ बोल रहा हूँ", "हाँ", "हाँ सही है"
PRE = [OPEN, YES, ADDR_YES]
R = SETUP["VALIDATION_REPLIES"]


@pytest.fixture()
def engine(monkeypatch):
    eng = wfe.WorkflowEngine()
    saver = MemorySaver()

    async def _mem_checkpointer(self):
        return saver

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem_checkpointer)
    return eng


def _clock(monkeypatch, now: datetime) -> None:
    monkeypatch.setattr(vd, "now_in", lambda tz=None: now.astimezone(ZoneInfo(tz or "Asia/Kolkata")))


async def _run(engine, monkeypatch, definition, turns, session, lang="hi-IN", context=CONTEXT):
    monkeypatch.setattr(wfe, "load_workflow_definition", lambda t, b, n: definition)
    replies, result = [], None
    for text in turns:
        result = await engine.handle_turn_detailed(
            session_id=session, tenant_id=TENANT, bot_id=BOT, workflow_name=WORKFLOW,
            user_text=text, language=lang, context_values=context,
        )
        replies.append(result["reply"])
        if result["done"]:
            break
    return replies, result


READBACK = "एक बार confirm कर लेते हैं"
DAY_ONLY_NOTE = "exact समय engineer आपको call करके बताएँगे"
CONFIRM_Q = "क्या ये आपके लिए ठीक रहेगा?"
BOOKED = "engineer allocate"
TEAM_DATE = "सही date तय"

# name → (turns, expected reply fragments per turn after the preamble, final slots, final fields)
CASES = {
    "past date refused, then kal": (
        PRE + ["10 October", "कल", YES],
        [R["day_past"]["hi"], READBACK, BOOKED],
        {"visit_status": "booked", "visit_day": "कल"},
        {"appointment_booked": "Yes", "appointment_date": "कल", "appointment_time": None}),
    "past date refused, valid time kept": (
        PRE + ["10 October 4 PM", "कल", YES],
        [R["day_past"]["hi"], "कल 4 PM", BOOKED],
        {"visit_day": "कल", "visit_time": "4 PM"},
        {"appointment_date": "कल", "appointment_time": "4 PM"}),
    "impossible dates refused twice, then parso": (
        PRE + ["31 February", "32 October", "परसों", YES],
        [R["day_invalid"]["hi"], R["day_invalid"]["hi"], READBACK, BOOKED],
        {"visit_day": "परसों"},
        {"appointment_date": "परसों"}),
    "today past time refused, day kept, evening completes": (
        PRE + ["आज सुबह 9 बजे", "शाम 5 बजे", YES],
        [R["time_past"]["hi"], "आज शाम 5 बजे", BOOKED],
        {"visit_day": "आज", "visit_time": "शाम 5 बजे"},
        {"appointment_date": "आज", "appointment_time": "शाम 5 बजे"}),
    "today past noon refused": (
        PRE + ["आज दोपहर 12 बजे", "शाम 6 बजे", YES],
        [R["time_past"]["hi"], "आज शाम 6 बजे", BOOKED],
        {"visit_time": "शाम 6 बजे"}, {"appointment_time": "शाम 6 बजे"}),
    "today ambiguous hour asks morning or evening": (
        PRE + ["आज 4 baje", "शाम 4 बजे", YES],
        [R["time_ambiguous"]["hi"], "आज शाम 4 बजे", BOOKED],
        {"visit_day": "आज", "visit_time": "शाम 4 बजे"},
        {"appointment_time": "शाम 4 बजे"}),
    "bare hour on a future day: period answer completes the time": (
        PRE + ["कल 4 बजे", "शाम", YES],
        [R["time_ambiguous"]["hi"], "कल शाम 4 बजे", BOOKED],
        {"visit_day": "कल", "visit_time": "शाम 4 बजे"},
        {"appointment_date": "कल", "appointment_time": "शाम 4 बजे"}),
    "live expression: कल सुबह नौ बजे is captured whole": (
        PRE + ["कल सुबह नौ बजे", YES], ["कल सुबह नौ बजे", BOOKED],
        {"visit_day": "कल", "visit_time": "सुबह नौ बजे"}, {"appointment_time": "सुबह नौ बजे"}),
    "live expression: कल सुबह दस बजे": (
        PRE + ["कल सुबह दस बजे", YES], ["कल सुबह दस बजे", BOOKED],
        {"visit_time": "सुबह दस बजे"}, {"appointment_time": "सुबह दस बजे"}),
    "live expression: ढाई बजे alone → day → period": (
        PRE + ["ढाई बजे", "कल", "दोपहर", YES],
        [R["day_missing"]["hi"], R["time_ambiguous"]["hi"], "कल दोपहर ढाई बजे", BOOKED],
        {"visit_day": "कल", "visit_time": "दोपहर ढाई बजे"}, {"appointment_time": "दोपहर ढाई बजे"}),
    "fraction with PM answer": (
        PRE + ["कल साढ़े चार बजे", "PM", YES],
        [R["time_ambiguous"]["hi"], "साढ़े चार बजे PM", BOOKED],
        {"visit_time": "साढ़े चार बजे PM"}, {"appointment_time": "साढ़े चार बजे PM"}),
    "parked hour replaced by a full time": (
        PRE + ["कल 4 बजे", "कल सुबह 10 बजे", YES],
        [R["time_ambiguous"]["hi"], "कल सुबह 10 बजे", BOOKED],
        {"visit_time": "सुबह 10 बजे"}, {"appointment_time": "सुबह 10 बजे"}),
    "romanised spoken numbers": (
        PRE + ["kal subah das baje", YES], ["kal subah das baje", BOOKED],
        {"visit_time": "subah das baje"}, {"appointment_time": "subah das baje"}),
    "romanised day-of-month word": (
        PRE + ["pachees tarikh subah 10 baje", YES], ["pachees tarikh subah 10 baje", BOOKED],
        {"visit_day": "pachees tarikh", "visit_time": "subah 10 baje"}, {"appointment_date": "pachees tarikh"}),
    "today past spoken time then a later spoken time": (
        PRE + ["आज सुबह नौ बजे", "शाम सात बजे", YES],
        [R["time_past"]["hi"], "आज शाम सात बजे", BOOKED],
        {"visit_day": "आज", "visit_time": "शाम सात बजे"}, {"appointment_time": "शाम सात बजे"}),
    "reschedule with a romanised fraction, period answer composes into both slots": (
        PRE + ["कल सुबह 10 बजे", "नहीं", "parso sadhe paanch baje", "sham", YES],
        [READBACK, "किस दिन", R["time_ambiguous"]["hi"], "parso sham sadhe paanch baje", BOOKED],
        {"visit_day": "parso", "visit_time": "sham sadhe paanch baje", "visit_time_2": "sham sadhe paanch baje"},
        {"appointment_date": "parso", "appointment_time": "sham sadhe paanch baje"}),
    "three bare hours take the authored fallback": (
        PRE + ["कल 4 बजे", "5 बजे", "6 बजे"],
        [R["time_ambiguous"]["hi"], R["time_ambiguous"]["hi"], TEAM_DATE],
        {"visit_status": "date_pending"}, {"appointment_booked": "No", "appointment_time": None, "call_result": "date_pending"}),
    "day with a period only is accepted as spoken": (
        PRE + ["कल शाम", YES], ["कल शाम", BOOKED], {"visit_time": "शाम"}, {"appointment_time": "शाम"}),
    "के connector: period, के, hour captured whole": (
        PRE + ["कल शाम के 5 बजे", YES], ["कल शाम के 5 बजे", BOOKED],
        {"visit_day": "कल", "visit_time": "शाम के 5 बजे"}, {"appointment_time": "शाम के 5 बजे"}),
    "ke connector romanised": (
        PRE + ["kal sham ke 5 baje", YES], ["kal sham ke 5 baje", BOOKED],
        {"visit_time": "sham ke 5 baje"}, {"appointment_time": "sham ke 5 baje"}),
    "के connector with a fraction is exact, no clarification": (
        PRE + ["कल शाम के साढ़े पाँच बजे", YES], ["कल शाम के साढ़े पाँच बजे", BOOKED],
        {"visit_time": "शाम के साढ़े पाँच बजे"}, {"appointment_time": "शाम के साढ़े पाँच बजे"}),
    "के time alone asks for the day": (
        PRE + ["सुबह के 10 बजे", "परसों", YES],
        [R["day_missing"]["hi"], "परसों सुबह के 10 बजे", BOOKED],
        {"visit_day": "परसों", "visit_time": "सुबह के 10 बजे"}, {"appointment_time": "सुबह के 10 बजे"}),
    "live utterance cv_37ce8d991411: past date refused, के time kept": (
        PRE + ["हाँ, यह कर दो 17 अक्टूबर को। शाम के 5 बजे।", "कल", YES],
        [R["day_past"]["hi"], "कल शाम के 5 बजे", BOOKED],
        {"visit_day": "कल", "visit_time": "शाम के 5 बजे"},
        {"appointment_date": "कल", "appointment_time": "शाम के 5 बजे"}),
    "live utterance cv_43dd2e621291: past date refused": (
        PRE + ["18 अक्टूबर कर दो। शाम के 5 बजे।", "परसों", YES],
        [R["day_past"]["hi"], "परसों शाम के 5 बजे", BOOKED],
        {"visit_day": "परसों", "visit_time": "शाम के 5 बजे"}, {}),
    "natural yes answers take the booked edge": (
        PRE + ["कल शाम के 5 बजे", "ठीक रहेगा"], [READBACK, BOOKED], {"visit_status": "booked"}, {}),
    "natural no answer reopens the date": (
        PRE + ["कल शाम के 5 बजे", "ठीक नहीं रहेगा", "परसों सुबह के 10 बजे", "haan chalega"],
        [READBACK, "किस दिन", READBACK, BOOKED],
        {"visit_status": "booked", "visit_day": "परसों", "visit_time": "सुबह के 10 बजे"}, {}),
    "callback keeps the whole spoken time": (
        [OPEN, "नहीं अभी busy हूँ", "परसों शाम 5 बजे call karna"], ["call back", "call back"],
        {"callback_time": "शाम 5 बजे"}, {"callback_time": "शाम 5 बजे", "call_result": "callback_requested"}),
    "today past time, then a different day": (
        PRE + ["आज सुबह 9 बजे", "कल सुबह 10 बजे", YES],
        [R["time_past"]["hi"], "कल सुबह 10 बजे", BOOKED],
        {"visit_day": "कल", "visit_time": "सुबह 10 बजे"},
        {"appointment_date": "कल", "appointment_time": "सुबह 10 बजे"}),
    "today with a later time is accepted": (
        PRE + ["आज रात 9 बजे", YES], [READBACK, BOOKED],
        {"visit_day": "आज", "visit_time": "रात 9 बजे"}, {"appointment_time": "रात 9 बजे"}),
    "today without a time is accepted": (
        PRE + ["आज", YES], [READBACK, BOOKED], {"visit_day": "आज"}, {"appointment_time": None}),
    "weekday, tarikh and numeric days are accepted": (
        PRE + ["25 tarikh", YES], [READBACK, BOOKED], {"visit_day": "25 tarikh"}, {}),
    "week-only phrase asks for a specific day": (
        PRE + ["agle hafte", "somvar", YES],
        [R["day_unclear"]["hi"], "somvar", BOOKED],
        {"visit_day": "somvar"}, {"appointment_date": "somvar"}),
    "week-only phrase in Devanagari asks for a specific day": (
        PRE + ["अगले हफ्ते", "परसों", YES],
        [R["day_unclear"]["hi"], "परसों", BOOKED], {"visit_day": "परसों"}, {}),
    "time without a day keeps the time, asks the day, then the period": (
        PRE + ["4 baje", "parso", "शाम", YES],
        [R["day_missing"]["hi"], R["time_ambiguous"]["hi"], "parso शाम 4 baje", BOOKED],
        {"visit_day": "parso", "visit_time": "शाम 4 baje"},
        {"appointment_date": "parso", "appointment_time": "शाम 4 baje"}),
    "reschedule with a time-only answer never reaches the model": (
        PRE + ["कल सुबह 10 बजे", "नहीं", "4 baje", "parso", "शाम", YES],
        [READBACK, "किस दिन", R["day_missing"]["hi"], R["time_ambiguous"]["hi"], "parso शाम 4 baje", BOOKED],
        {"visit_day": "parso", "visit_time": "शाम 4 baje", "visit_day_2": "parso", "visit_time_2": "शाम 4 baje"},
        {"appointment_date": "parso", "appointment_time": "शाम 4 baje"}),
    "retry prompt no longer suggests a past example date": (
        PRE + ["pata nahi", "कल", YES],
        ["जैसे कल, या परसों", READBACK, BOOKED], {"visit_day": "कल"}, {}),
    "date change to a past date is refused on the second ask": (
        PRE + ["कल सुबह 10 बजे", "नहीं", "10 October", "25 October", YES],
        [READBACK, "किस दिन", R["day_past"]["hi"], "25 October", BOOKED],
        {"visit_day": "25 October", "visit_day_2": "25 October", "visit_time": ""},
        {"appointment_date": "25 October", "appointment_time": None}),
    "today branch: past time refused at the hub, evening completes": (
        [OPEN, "आज सुबह 9 बजे भेज दो", "शाम 5 बजे", "ठीक है", ADDR_YES, YES],
        [R["time_past"]["hi"], "technician", "location सही", "आज शाम 5 बजे", BOOKED],
        {"visit_day": "आज", "visit_time": "शाम 5 बजे"},
        {"appointment_date": "आज", "appointment_time": "शाम 5 बजे", "call_result": "booked"}),
    "three refusals take the authored fallback": (
        PRE + ["31 February", "10 October", "32 October"],
        [R["day_invalid"]["hi"], R["day_past"]["hi"], TEAM_DATE],
        {"visit_status": "date_pending"},
        {"appointment_booked": "No", "appointment_date": None, "call_result": "date_pending"}),
}


class TestValidation:
    @pytest.mark.parametrize("name", list(CASES))
    async def test_scenario(self, engine, monkeypatch, name):
        turns, fragments, slots_expected, fields_expected = CASES[name]
        _clock(monkeypatch, MONDAY_NOON)
        replies, result = await _run(engine, monkeypatch, V14, turns, f"val-{name}")
        tail = replies[len(turns) - len(fragments):]
        for fragment, reply in zip(fragments, tail):
            assert fragment in reply, (name, fragment, reply)
        slots = result["slots"]
        for key, value in slots_expected.items():
            assert slots.get(key) == value, (name, key, slots)
        fields = derive_structured_fields(POLICY, _snapshot(slots))
        for key, value in fields_expected.items():
            assert fields[key] == value, (name, key, fields)

    async def test_english_caller_hears_the_english_line(self, engine, monkeypatch):
        _clock(monkeypatch, MONDAY_NOON)
        replies, result = await _run(
            engine, monkeypatch, V14,
            ["yes speaking", "yes", "yes that's correct", "10 October", "next week", "4 PM",
             "Monday", "yes"],
            "val-en", lang="en-IN")
        assert replies[3] == R["day_past"]["en"]
        assert replies[4] == R["day_unclear"]["en"]
        assert replies[5] == R["day_missing"]["en"]
        assert "Monday at 4 PM" in replies[6]
        assert result["slots"]["visit_status"] == "booked"
        assert result["slots"]["visit_time"] == "4 PM"

    @pytest.mark.parametrize("turns, composed", [
        (["tomorrow at four", "evening"], "four in the evening"),
        (["half past four tomorrow", "pm"], "half past four PM"),
        (["four thirty tomorrow", "in the morning"], "four thirty in the morning"),
    ])
    async def test_english_spoken_times_are_clarified_then_composed(self, engine, monkeypatch, turns, composed):
        _clock(monkeypatch, MONDAY_NOON)
        replies, result = await _run(
            engine, monkeypatch, V14,
            ["yes speaking", "yes", "yes that's correct", *turns, "yes"], f"val-en-{turns[0]}", lang="en-IN")
        assert replies[3] == R["time_ambiguous"]["en"]
        assert composed in replies[4]
        assert result["slots"]["visit_time"] == composed and result["slots"]["visit_status"] == "booked"

    async def test_rejection_is_audited_and_slot_cleared(self, engine, monkeypatch):
        _clock(monkeypatch, MONDAY_NOON)
        monkeypatch.setattr(wfe, "load_workflow_definition", lambda t, b, n: V14)
        for text in PRE:
            await engine.handle_turn_detailed(session_id="val-audit", tenant_id=TENANT, bot_id=BOT,
                                              workflow_name=WORKFLOW, user_text=text,
                                              language="hi-IN", context_values=CONTEXT)
        result = await engine.handle_turn_detailed(session_id="val-audit", tenant_id=TENANT, bot_id=BOT,
                                                   workflow_name=WORKFLOW, user_text="10 October 4 PM",
                                                   language="hi-IN", context_values=CONTEXT)
        assert result["done"] is False
        assert "visit_day" not in result["slots"] and result["slots"].get("visit_time") == "4 PM"
        assert result["status"] == "collecting"


BARE_HOUR_SCENARIOS = {"date_changed_day_and_time", "date_changed_time_then_day"}


class TestConfirmationWording:
    async def test_day_and_time_sentence(self, engine, monkeypatch):
        _clock(monkeypatch, MONDAY_NOON)
        replies, _ = await _run(engine, monkeypatch, V14, PRE + ["कल शाम के 5 बजे"], "wording-1")
        assert replies[3] == (
            "ठीक है, एक बार confirm कर लेते हैं। हमारे engineer कल शाम के 5 बजे आपके registered address "
            "Sector 62, Noida पर Airtel Broadband device के uninstallation और pickup के लिए आएँगे। "
            "क्या ये आपके लिए ठीक रहेगा?")

    async def test_day_only_sentence_promises_no_time(self, engine, monkeypatch):
        _clock(monkeypatch, MONDAY_NOON)
        replies, _ = await _run(engine, monkeypatch, V14, PRE + ["परसों"], "wording-2")
        assert replies[3] == (
            "ठीक है, एक बार confirm कर लेते हैं। हमारे engineer परसों आपके registered address "
            "Sector 62, Noida पर Airtel Broadband device के uninstallation और pickup के लिए आएँगे। "
            "visit का exact समय engineer आपको call करके बताएँगे। क्या ये आपके लिए ठीक रहेगा?")

    async def test_period_only_reads_as_spoken(self, engine, monkeypatch):
        _clock(monkeypatch, MONDAY_NOON)
        replies, _ = await _run(engine, monkeypatch, V14, PRE + ["कल शाम"], "wording-3")
        assert "हमारे engineer कल शाम आपके registered address" in replies[3]
        assert DAY_ONLY_NOTE not in replies[3]

    async def test_english_sentences(self, engine, monkeypatch):
        _clock(monkeypatch, MONDAY_NOON)
        en = ["yes speaking", "yes", "yes that's correct"]
        replies, _ = await _run(engine, monkeypatch, V14, en + ["tomorrow at 4 PM"], "wording-4", lang="en-IN")
        assert replies[3] == (
            "Let me confirm the details. Our engineer will come to your registered address in Sector 62, Noida "
            "tomorrow at 4 PM for the Airtel Broadband device uninstallation and pickup. Does that work for you?")
        replies, _ = await _run(engine, monkeypatch, V14, en + ["Monday"], "wording-5", lang="en-IN")
        assert replies[3].endswith("The engineer will call you to confirm the exact time. Does that work for you?")

    async def test_second_confirmation_uses_the_same_wording(self, engine, monkeypatch):
        _clock(monkeypatch, MONDAY_NOON)
        replies, result = await _run(engine, monkeypatch, V14,
                                     PRE + ["कल शाम के 5 बजे", "नहीं", "परसों", "सही है"], "wording-6")
        assert replies[5].startswith("ठीक है, एक बार confirm कर लेते हैं। हमारे engineer परसों आपके registered address")
        assert DAY_ONLY_NOTE in replies[5] and result["slots"]["visit_status"] == "booked"


OLD_INTROS = ("हम जल्दी से एक बार details confirm कर लेते हैं।", "Let me quickly confirm the details.")
NEW_INTROS = ("ठीक है, एक बार confirm कर लेते हैं।", "Let me confirm the details.")


class TestNoChangeForValidInput:
    """Clock set to 2026-10-10 so every date the 15 summary scenarios use
    (कल, parso, आज, 16 October, tomorrow) lies ahead: v16 must speak exactly
    what v13 speaks and derive the same fields. The two scenarios that answer
    with a bare hour ("5 baje", "4 baje") now get a morning-or-evening
    question by design and are covered in TestBareHourScenarios."""

    @pytest.mark.parametrize("name", sorted(set(SUMMARY_SCENARIOS) - BARE_HOUR_SCENARIOS))
    async def test_replies_and_fields_identical_to_v13(self, engine, monkeypatch, name):
        case = SUMMARY_SCENARIOS[name]
        _clock(monkeypatch, datetime(2026, 10, 10, 10, 0, tzinfo=IST))
        context = case.get("context", CONTEXT)
        old, r13 = await _run(engine, monkeypatch, V13, case["turns"], f"g13-{name}", context=context)
        new, r14 = await _run(engine, monkeypatch, V14, case["turns"], f"g14-{name}", context=context)
        # Two intended differences: the retry prompt's example date is relative
        # now, and a time-only turn is answered by the flow (v13: off-script,
        # empty engine reply) instead of the model.
        fix = SETUP["EXAMPLE_DATE_FIX"]
        expected = [next((r.replace(a, b) for a, b in fix.items() if a in r), r) for r in old]
        for i, (want, got) in enumerate(zip(expected, new)):
            if want == "" and got == R["day_missing"]["hi"]:
                continue
            if want.startswith(OLD_INTROS) and got.startswith(NEW_INTROS):
                continue   # intended: the confirmation is reworded (TestConfirmationWording)
            assert got == want, (name, i, want, got)
        assert len(new) == len(old)
        new_fields = derive_structured_fields(POLICY, _snapshot(r14["slots"]))
        old_fields = derive_structured_fields(POLICY, _snapshot(r13["slots"]))
        if name == "callback":
            # Intended improvement: the callback time is captured whole now.
            assert (old_fields.pop("callback_time"), new_fields.pop("callback_time")) == ("शाम", "शाम 5 बजे")
        assert new_fields == old_fields
        for key, expected in case["fields"].items():
            if name == "callback" and key == "callback_time":
                continue   # asserted above: "शाम 5 बजे" instead of the v13 "शाम"
            assert derive_structured_fields(POLICY, _snapshot(r14["slots"]))[key] == expected


class TestBareHourScenarios:
    async def test_change_date_with_bare_hour_is_clarified_then_booked(self, engine, monkeypatch):
        _clock(monkeypatch, datetime(2026, 10, 10, 10, 0, tzinfo=IST))
        turns = PRE + ["कल सुबह 10 बजे", "नहीं date change karni hai", "16 October 5 baje", "शाम", YES]
        replies, result = await _run(engine, monkeypatch, V14, turns, "bare-1")
        assert replies[5] == R["time_ambiguous"]["hi"]
        assert "16 October शाम 5 baje" in replies[6]
        fields = derive_structured_fields(POLICY, _snapshot(result["slots"]))
        assert (fields["appointment_date"], fields["appointment_time"]) == ("16 October", "शाम 5 baje")

    async def test_time_first_then_day_then_period(self, engine, monkeypatch):
        _clock(monkeypatch, datetime(2026, 10, 10, 10, 0, tzinfo=IST))
        turns = PRE + ["कल सुबह 10 बजे", "नहीं", "4 baje", "parso", "शाम", YES]
        replies, result = await _run(engine, monkeypatch, V14, turns, "bare-2")
        assert replies[5] == R["day_missing"]["hi"]
        assert replies[6] == R["time_ambiguous"]["hi"]
        assert "parso शाम 4 baje" in replies[7]
        assert result["slots"]["visit_status"] == "booked"


EN_PRE = ["yes speaking", "yes", "yes that's correct"]
TODAY_BRANCH = [OPEN, "आज सुबह 9 बजे भेज दो", "शाम 5 बजे", "ठीक है", ADDR_YES, YES]

# name → (turns, language, normalized date, normalized time) at MONDAY_NOON (Mon 19 Oct 2026)
NORMALIZED_CASES = {
    "hindi: kal sham ke 5 baje": (PRE + ["कल शाम के 5 बजे", YES], "hi-IN", "2026-10-20", "17:00"),
    "hindi spoken numbers: parso subah nau baje": (PRE + ["परसों सुबह नौ बजे", YES], "hi-IN", "2026-10-21", "09:00"),
    "hindi fraction then PM": (PRE + ["कल साढ़े चार बजे", "PM", YES], "hi-IN", "2026-10-20", "16:30"),
    "hindi special time: dopahar dhai baje": (PRE + ["कल दोपहर ढाई बजे", YES], "hi-IN", "2026-10-20", "14:30"),
    "hinglish: kal subah das baje": (PRE + ["kal subah das baje", YES], "hi-IN", "2026-10-20", "10:00"),
    "hinglish tarikh: 25 tarikh subah 10 baje": (PRE + ["25 tarikh subah 10 baje", YES], "hi-IN", "2026-10-25", "10:00"),
    "english: tomorrow at 4 PM": (EN_PRE + ["tomorrow at 4 PM", "yes"], "en-IN", "2026-10-20", "16:00"),
    "english: Monday, half past four, evening": (EN_PRE + ["Monday half past four", "evening", "yes"], "en-IN", "2026-10-26", "16:30"),
    "english: 25 October 16:30": (EN_PRE + ["25 October 16:30", "yes"], "en-IN", "2026-10-25", "16:30"),
    "today with a later time": (PRE + ["आज रात 9 बजे", YES], "hi-IN", "2026-10-19", "21:00"),
    "today branch via the hub": (TODAY_BRANCH, "hi-IN", "2026-10-19", "17:00"),
    "date only: Pickup time stays null": (PRE + ["परसों", YES], "hi-IN", "2026-10-21", None),
    "weekday only": (PRE + ["somvar", YES], "hi-IN", "2026-10-26", None),
    "period only is a window, not a clock time": (PRE + ["कल शाम", YES], "hi-IN", "2026-10-20", "evening"),
    "english period only": (EN_PRE + ["tomorrow in the morning", "yes"], "en-IN", "2026-10-20", "morning"),
    "bare hour clarified then composed": (PRE + ["कल 4 बजे", "शाम", YES], "hi-IN", "2026-10-20", "16:00"),
    "past date refused, then kal keeps the valid time": (PRE + ["10 October 4 PM", "कल", YES], "hi-IN", "2026-10-20", "16:00"),
    "reschedule: new day and time": (PRE + ["कल सुबह 10 बजे", "नहीं", "परसों शाम के 5 बजे", YES], "hi-IN", "2026-10-21", "17:00"),
    "reschedule to a day only drops the old time": (PRE + ["कल सुबह 10 बजे", "नहीं", "परसों", YES], "hi-IN", "2026-10-21", None),
    "reschedule: time first, then day, then period": (PRE + ["कल सुबह 10 बजे", "नहीं", "4 baje", "parso", "शाम", YES], "hi-IN", "2026-10-21", "16:00"),
    "reschedule in english": (EN_PRE + ["tomorrow at 10 AM", "no", "day after tomorrow at 5 PM", "yes"], "en-IN", "2026-10-21", "17:00"),
    "not booked: nothing reported": (PRE + ["कल 4 बजे", "5 बजे", "6 बजे"], "hi-IN", None, None),
    "date pending (team callback) reports no date": (PRE + ["कल सुबह 10 बजे", "नहीं", "pata nahi", "pata nahi", "pata nahi"], "hi-IN", None, None),
}


class TestNormalizedSummary:
    """Stage 5: appointment_date / appointment_time are the parser's resolved
    values (ISO date, 24-hour clock or a window word), derived through the
    bot's real summaryFields from the final slots exactly as the post-call
    pipeline sees them; the spoken slots and every reply are untouched."""

    @pytest.mark.parametrize("name", list(NORMALIZED_CASES))
    async def test_scenario(self, engine, monkeypatch, name):
        turns, lang, date_expected, time_expected = NORMALIZED_CASES[name]
        _clock(monkeypatch, MONDAY_NOON)
        _, result = await _run(engine, monkeypatch, V14, turns, f"norm-{name}", lang=lang)
        fields = derive_structured_fields(NORMALIZED_POLICY, _snapshot(result["slots"]))
        assert (fields["appointment_date"], fields["appointment_time"]) == (date_expected, time_expected), (name, fields, result["slots"])
        assert set(fields) == {f["name"] for f in SETUP["SUMMARY_FIELDS"]}

    @pytest.mark.parametrize("name", list(CASES))
    async def test_nothing_spoken_changes_between_stage_4_and_5(self, engine, monkeypatch, name):
        turns = CASES[name][0]
        _clock(monkeypatch, MONDAY_NOON)
        old, r4 = await _run(engine, monkeypatch, V17, turns, f"s4-{name}")
        new, r5 = await _run(engine, monkeypatch, V14, turns, f"s5-{name}")
        assert new == old, name
        companions = set(SETUP["NORMALIZED_SLOTS"].values())
        assert {k: v for k, v in r5["slots"].items() if k not in companions} == r4["slots"], name
        # the spoken fields are identical too; only the normalized ones differ
        assert derive_structured_fields(POLICY, _snapshot(r5["slots"])) == \
            derive_structured_fields(POLICY, _snapshot(r4["slots"]))

    async def test_relative_day_is_resolved_at_the_utterance_not_the_call_start(self, engine, monkeypatch):
        """A call that starts at 23:58 and hears "कल" at 00:02: tomorrow is
        the 21st (the clock at that utterance), not the 20th."""
        _clock(monkeypatch, datetime(2026, 10, 19, 23, 58, tzinfo=IST))
        await _run(engine, monkeypatch, V14, PRE, "midnight-1")
        _clock(monkeypatch, datetime(2026, 10, 20, 0, 2, tzinfo=IST))
        _, result = await _run(engine, monkeypatch, V14, ["कल शाम 5 बजे", YES], "midnight-1")
        fields = derive_structured_fields(NORMALIZED_POLICY, _snapshot(result["slots"]))
        assert (fields["appointment_date"], fields["appointment_time"]) == ("2026-10-21", "17:00")
        assert result["slots"]["visit_day"] == "कल"   # the spoken word is kept for the readback

    async def test_today_just_after_midnight(self, engine, monkeypatch):
        _clock(monkeypatch, datetime(2026, 10, 20, 0, 10, tzinfo=IST))
        _, result = await _run(engine, monkeypatch, V14, PRE + ["आज सुबह 9 बजे", YES], "midnight-2")
        fields = derive_structured_fields(NORMALIZED_POLICY, _snapshot(result["slots"]))
        assert (fields["appointment_date"], fields["appointment_time"]) == ("2026-10-20", "09:00")

    async def test_api_payload_shows_the_display_forms_and_keeps_the_values(self, engine, monkeypatch):
        _clock(monkeypatch, MONDAY_NOON)
        _, result = await _run(engine, monkeypatch, V14, PRE + ["कल शाम के 5 बजे", YES], "display-1")
        fields = derive_structured_fields(NORMALIZED_POLICY, _snapshot(result["slots"]))
        row = SimpleNamespace(status="completed", call_outcome=None, summary="", memory={"structured_fields": fields},
                              next_best_action=None, next_action=None, follow_up_required=False,
                              follow_up_at=None, confidence=None, generated_at=None, error=None)
        payload = serialize_conversation_memory(row, SETUP["SUMMARY_FIELDS"])
        assert payload["structuredFields"]["appointment_date"] == "2026-10-20"
        assert payload["structuredFields"]["appointment_time"] == "17:00"
        assert payload["structuredFieldLabels"]["appointment_date"] == "Pickup date"
        assert payload["structuredFieldLabels"]["appointment_time"] == "Pickup time"
        assert payload["structuredFieldDisplay"] == {"appointment_date": "20 Oct 2026",
                                                     "appointment_time": "05:00 PM IST"}
        assert list(payload["structuredFields"]) == [f["name"] for f in SETUP["SUMMARY_FIELDS"]]
        window = dict(fields, appointment_time="evening")
        assert display_structured_fields(SETUP["SUMMARY_FIELDS"], window)["appointment_time"] == "Evening (time window)"


class TestDefinition:
    def test_validation_declared_on_the_four_date_asks_only(self):
        from backend.routers.workflows import validate_definition

        declared = {n["id"] for n in V14["nodes"]
                    if isinstance((n.get("config") or {}).get("validation"), dict)}
        assert declared == set(SETUP["VALIDATED_ASKS"])
        for n in V14["nodes"]:
            cfg = (n.get("config") or {}).get("validation")
            if cfg:
                assert cfg["type"] == "future_datetime" and cfg["timezone"] == "Asia/Kolkata"
                assert set(cfg["replyByLanguage"]) == {"day_unclear", "day_missing", "day_past",
                                                       "day_invalid", "time_past", "time_ambiguous",
                                                       "time_unclear", "time_invalid"}
                assert "10 अक्टूबर" not in n["config"]["unmatchedReplyByLanguage"]["hi"]
                assert "10 October" not in n["config"]["unmatchedReplyByLanguage"]["en"]
        assert validate_definition(V14["nodes"], V14["edges"]) == ([], [])
        # Beyond stage 1, the upgrade only touches the keys the normaliser
        # restores; everything else must equal the stage-1 definition.
        norm_nodes, norm_edges = SETUP["normalise_to_stage1"](V14["nodes"], V14["edges"])
        assert norm_nodes == V13["nodes"] and norm_edges == V13["edges"]
        regex = SETUP["build_time_regex"]()
        assert "के" in regex and "ke" in regex
        for n in V14["nodes"]:
            if n["id"] in SETUP["CONFIRM_NODES"]:
                assert n["config"]["readback"] == SETUP["build_readback"](*SETUP["CONFIRM_NODES"][n["id"]])
            if n["id"] == SETUP["RESET_MARKER"]:
                assert n["config"]["setSlots"] == {"visit_day": "", "visit_time": "",
                                                   "visit_time_pending": "", "visit_time_2_pending": "",
                                                   "visit_date_iso": "", "visit_time_24h": ""}
            if n["id"] in SETUP["VALIDATED_ASKS"]:
                assert n["config"]["validation"]["normalizedDayVariable"] == "visit_date_iso"
                assert n["config"]["validation"]["normalizedTimeVariable"] == "visit_time_24h"
        for spec in SETUP["SUMMARY_FIELDS"]:
            if spec["name"] == "appointment_date":
                assert (spec["type"], spec["source"], spec["label"]) == ("date", "visit_date_iso", "Pickup date")
            if spec["name"] == "appointment_time":
                assert (spec["type"], spec["source"], spec["label"], spec["timezone"]) == \
                    ("time", "visit_time_24h", "Pickup time", "Asia/Kolkata")
        assert [f["name"] for f in SETUP["SUMMARY_FIELDS"]] == [f["name"] for f in SETUP["summary_fields"](1)]
        assert [f["label"] for f in SETUP["SUMMARY_FIELDS"]] == [f["label"] for f in SETUP["summary_fields"](1)]
        yes_edges = [e for e in V14["edges"] if e["from"] in SETUP["CONFIRM_NODES"] and e["to"] == "n_mark_booked"]
        assert len(yes_edges) == 2 and all("ठीक रहेगा" in e["label"] and "chalega" in e["label"] for e in yes_edges)
