"""Jeeves Airtel uninstallation bot: visit day/time validation (workflow
stage 2, jeeves/setup/01_structured_summary.py → v14).

The four date-capturing asks declare ``validation: future_datetime`` in
Asia/Kolkata. A past or impossible day, or a past/ambiguous time for today,
is refused with the configured line and asked again while valid information
already given is kept; unparseable phrasing is accepted as spoken. For valid
inputs the flow speaks exactly what v13 speaks (golden comparison over the 15
structured-summary scenarios)."""

import runpy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.visit_datetime as vd
import shared.orchestration.workflow_engine as wfe
from shared.orchestration.goal_engine import compile_goal_policy
from shared.post_call.structured import derive_structured_fields
from tests.unit.test_jeeves_structured_summary import (
    BOT, CONTEXT, SCENARIOS as SUMMARY_SCENARIOS, TENANT, WORKFLOW, _snapshot,
)

IST = ZoneInfo("Asia/Kolkata")
SETUP = runpy.run_path("jeeves/setup/01_structured_summary.py")
V13 = SETUP["upgraded_definition"](13, stage=1)
V14 = SETUP["upgraded_definition"](14, stage=2)
POLICY = compile_goal_policy({"summaryFields": SETUP["SUMMARY_FIELDS"]},
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


READBACK = "details confirm"
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
    "today past time, then a different day": (
        PRE + ["आज सुबह 9 बजे", "कल सुबह 10 बजे", YES],
        [R["time_past"]["hi"], "कल सुबह 10 बजे", BOOKED],
        {"visit_day": "कल", "visit_time": "सुबह 10 बजे"},
        {"appointment_date": "कल", "appointment_time": "सुबह 10 बजे"}),
    "future day with bare hour is accepted as spoken": (
        PRE + ["कल 4 बजे", YES], ["कल 4 बजे", BOOKED],
        {"visit_day": "कल", "visit_time": "4 बजे"}, {"appointment_time": "4 बजे"}),
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
    "time without a day keeps the time and asks for the day": (
        PRE + ["4 baje", "parso", YES],
        [R["day_missing"]["hi"], "parso 4 baje", BOOKED],
        {"visit_day": "parso", "visit_time": "4 baje"},
        {"appointment_date": "parso", "appointment_time": "4 baje"}),
    "reschedule with a time-only answer never reaches the model": (
        PRE + ["कल सुबह 10 बजे", "नहीं", "4 baje", "parso", YES],
        [READBACK, "किस दिन", R["day_missing"]["hi"], "parso 4 baje", BOOKED],
        {"visit_day": "parso", "visit_time": "4 baje", "visit_day_2": "parso", "visit_time_2": "4 baje"},
        {"appointment_date": "parso", "appointment_time": "4 baje"}),
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


class TestNoChangeForValidInput:
    """Clock set to 2026-10-10 so every date the 15 summary scenarios use
    (कल, parso, आज, 16 October, tomorrow) lies ahead: v14 must speak exactly
    what v13 speaks and derive the same fields."""

    @pytest.mark.parametrize("name", sorted(SUMMARY_SCENARIOS))
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
            assert got == want, (name, i, want, got)
        assert len(new) == len(old)
        assert derive_structured_fields(POLICY, _snapshot(r14["slots"])) == \
            derive_structured_fields(POLICY, _snapshot(r13["slots"]))
        for key, expected in case["fields"].items():
            assert derive_structured_fields(POLICY, _snapshot(r14["slots"]))[key] == expected


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
                                                       "day_invalid", "time_past",
                                                       "time_ambiguous", "time_invalid"}
                assert "10 अक्टूबर" not in n["config"]["unmatchedReplyByLanguage"]["hi"]
                assert "10 October" not in n["config"]["unmatchedReplyByLanguage"]["en"]
        assert validate_definition(V14["nodes"], V14["edges"]) == ([], [])
        # Nothing but the validation key and the example date differs from v13.
        assert SETUP["strip_stage2"](V14["nodes"]) == V13["nodes"]
        assert V14["edges"] == V13["edges"]
