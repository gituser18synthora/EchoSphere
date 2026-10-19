"""Jeeves Airtel Broadband uninstallation bot (bot_44a50e8c4146): structured
post-call summary (jeeves/setup/01_structured_summary.py).

Two guarantees:

1. The workflow upgrade changes NOTHING the caller hears. Every scripted
   call below is run through the engine on the committed v12 baseline and on
   the upgraded definition, and the replies must be identical turn by turn
   (golden comparison). The markers are silent bookkeeping nodes only.
2. The final slots — and the summary fields derived from them — reflect the
   actual outcome: Yes/No only where a slot records it, null otherwise, the
   appointment date/time only for a confirmed booking, callback details only
   for a callback.
"""

import runpy

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as wfe
from shared.orchestration.goal_engine import BotGoalPolicy, compile_goal_policy
from shared.post_call.structured import derive_structured_fields, merge_structured_fields

SETUP = runpy.run_path("jeeves/setup/01_structured_summary.py")
SUMMARY_FIELDS = SETUP["SUMMARY_FIELDS"]
BASELINE = SETUP["load_baseline"]()
V12 = {"id": BASELINE["id"], "version": 12, "name": BASELINE["name"],
       "nodes": BASELINE["nodes"], "edges": BASELINE["edges"]}
UPGRADED = SETUP["upgraded_definition"](13, stage=1)   # markers only: the v12 → v13 golden

TENANT, BOT, WORKFLOW = "tn_b0e1194a1e1d", "bot_44a50e8c4146", "wf_9685ed07e312"
CONTEXT = {"customer_name": "Rahul", "locality": "Sector 62, Noida",
           "connection_status": "DISCONNECTED"}
NOT_DISCONNECTED = {**CONTEXT, "connection_status": "ACTIVE"}


def _policy() -> BotGoalPolicy:
    return compile_goal_policy({"summaryFields": SUMMARY_FIELDS},
                               bot_name="Customer Support", system_prompt="")


def _snapshot(slots: dict) -> dict:
    """What enqueue_post_call keeps of the slots: scalar, non-empty strings."""
    return {k: str(v) for k, v in slots.items()
            if v is not None and not isinstance(v, (dict, list)) and str(v).strip()}


# ── scripted calls: utterances → expected final slots / fields ───────────────

HI_YES, ADDRESS_YES, FIRST_DATE = "हाँ", "हाँ सही है", "कल सुबह 10 बजे"
OPEN = ["हाँ बोल रहा हूँ"]

SCENARIOS = {
    "booked_hindi": dict(
        turns=OPEN + [HI_YES, ADDRESS_YES, FIRST_DATE, HI_YES],
        slots={"visit_status": "booked", "address_verified": "yes",
               "visit_day": "कल", "visit_time": "सुबह 10 बजे"},
        fields={"address_verified": "Yes", "appointment_booked": "Yes",
                "appointment_date": "कल", "appointment_time": "सुबह 10 बजे",
                "call_result": "booked"},
    ),
    "booked_english": dict(
        turns=["yes speaking", "yes", "yes that's correct", "tomorrow at 4 PM", "yes go ahead"],
        slots={"visit_status": "booked", "address_verified": "yes",
               "visit_day": "tomorrow", "visit_time": "4 PM"},
        fields={"appointment_booked": "Yes", "appointment_date": "tomorrow",
                "appointment_time": "4 PM", "call_result": "booked"},
    ),
    "date_changed_day_and_time": dict(
        turns=OPEN + [HI_YES, ADDRESS_YES, FIRST_DATE, "नहीं date change karni hai",
                      "16 October 5 baje", HI_YES],
        slots={"visit_status": "booked", "visit_day": "16 October", "visit_time": "5 baje",
               "visit_day_2": "16 October", "visit_time_2": "5 baje"},
        fields={"appointment_booked": "Yes", "appointment_date": "16 October",
                "appointment_time": "5 baje", "call_result": "booked"},
    ),
    "date_changed_day_only_drops_old_time": dict(
        turns=OPEN + [HI_YES, ADDRESS_YES, FIRST_DATE, "नहीं", "parso", HI_YES],
        slots={"visit_status": "booked", "visit_day": "parso", "visit_time": ""},
        fields={"appointment_booked": "Yes", "appointment_date": "parso",
                "appointment_time": None, "call_result": "booked"},
    ),
    "date_changed_time_then_day": dict(
        turns=OPEN + [HI_YES, ADDRESS_YES, FIRST_DATE, "नहीं", "4 baje", "parso", HI_YES],
        slots={"visit_status": "booked", "visit_day": "parso", "visit_time": "4 baje"},
        fields={"appointment_booked": "Yes", "appointment_date": "parso",
                "appointment_time": "4 baje", "call_result": "booked"},
    ),
    "second_confirmation_declined": dict(
        turns=OPEN + [HI_YES, ADDRESS_YES, "कल", "नहीं", "parso", "नहीं"],
        slots={"visit_status": "date_pending", "address_verified": "yes", "visit_day": "parso"},
        fields={"address_verified": "Yes", "appointment_booked": "No",
                "appointment_date": None, "appointment_time": None,
                "call_result": "date_pending"},
    ),
    "no_usable_date_falls_back": dict(
        turns=OPEN + [HI_YES, ADDRESS_YES, "pata nahi", "kuch bhi", "hmm", "abc"],
        slots={"visit_status": "date_pending", "address_verified": "yes"},
        fields={"appointment_booked": "No", "appointment_date": None,
                "call_result": "date_pending"},
    ),
    "callback": dict(
        turns=OPEN + ["नहीं अभी busy हूँ", "परसों शाम 5 बजे call karna"],
        slots={"visit_status": "callback_requested", "callback_date": "परसों",
               "callback_time": "शाम", "callback_preference": "परसों शाम 5 बजे call karna"},
        fields={"address_verified": None, "appointment_booked": "No",
                "appointment_date": None, "call_result": "callback_requested",
                "callback_date": "परसों", "callback_time": "शाम",
                "callback_preference": "परसों शाम 5 बजे call karna"},
    ),
    "wrong_person": dict(
        turns=["नहीं गलत नंबर है"],
        slots={"visit_status": "wrong_person", "identity_answer": "no"},
        fields={"address_verified": None, "appointment_booked": "No",
                "call_result": "wrong_person"},
    ),
    "address_mismatch": dict(
        turns=OPEN + [HI_YES, "नहीं मैं shift हो गया हूँ"],
        slots={"visit_status": "address_mismatch", "address_verified": "no",
               "sub_disposition": "Address Mismatch"},
        fields={"address_verified": "No", "appointment_booked": "No",
                "appointment_date": None, "call_result": "address_mismatch"},
    ),
    "today_branch": dict(
        turns=OPEN + ["आज कर दो", "ठीक है", ADDRESS_YES, HI_YES],
        slots={"visit_status": "booked", "address_verified": "yes", "visit_day": "आज"},
        fields={"address_verified": "Yes", "appointment_booked": "Yes",
                "appointment_date": "आज", "appointment_time": None,
                "call_result": "booked"},
    ),
    "not_disconnected": dict(
        context=NOT_DISCONNECTED,
        turns=OPEN + [HI_YES],
        slots={"visit_status": "not_disconnected"},
        fields={"address_verified": None, "appointment_booked": "No",
                "call_result": "not_disconnected"},
    ),
    "call_dropped_at_address_question": dict(
        turns=OPEN + [HI_YES],
        done=False,
        slots={"visit_status": "incomplete"},
        fields={"address_verified": None, "appointment_booked": "No",
                "appointment_date": None, "call_result": "incomplete"},
    ),
    "address_answer_unclear_is_not_verified": dict(
        turns=OPEN + [HI_YES, "मेरा router kharab hai"],
        done=False,
        slots={"visit_status": "incomplete"},
        fields={"address_verified": None, "call_result": "incomplete"},
    ),
}


@pytest.fixture()
def engine(monkeypatch):
    eng = wfe.WorkflowEngine()
    saver = MemorySaver()

    async def _mem_checkpointer(self):
        return saver

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem_checkpointer)
    return eng


async def _run(engine, monkeypatch, definition, turns, session, context):
    monkeypatch.setattr(wfe, "load_workflow_definition",
                        lambda tenant_id, bot_id, name: definition)
    replies, result = [], None
    for text in turns:
        result = await engine.handle_turn_detailed(
            session_id=session, tenant_id=TENANT, bot_id=BOT, workflow_name=WORKFLOW,
            user_text=text, language="hi-IN", context_values=context,
        )
        replies.append(result["reply"])
        if result["done"]:
            break
    return replies, result


class TestUpgradeIsSilent:
    """The upgraded definition speaks exactly what v12 speaks."""

    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    async def test_replies_identical_to_v12(self, engine, monkeypatch, name):
        case = SCENARIOS[name]
        context = case.get("context", CONTEXT)
        old, _ = await _run(engine, monkeypatch, V12, case["turns"], f"{name}-v12", context)
        new, result = await _run(engine, monkeypatch, UPGRADED, case["turns"], f"{name}-new", context)
        assert new == old
        assert result["done"] is case.get("done", True)

    def test_markers_are_silent_message_nodes_and_structure_validates(self):
        from backend.routers.workflows import validate_definition

        nodes = {n["id"]: n for n in UPGRADED["nodes"]}
        for marker_id in SETUP["MARKER_IDS"]:
            node = nodes[marker_id]
            assert node["kind"] == "message"
            assert node["config"]["silent"] is True
            assert not node["config"].get("text")
        errors, issues = validate_definition(UPGRADED["nodes"], UPGRADED["edges"])
        assert errors == [] and issues == []

    def test_upgrade_is_idempotent_and_refuses_a_foreign_workflow(self):
        nodes, edges = SETUP["upgrade_workflow"](UPGRADED["nodes"], UPGRADED["edges"])
        assert nodes == UPGRADED["nodes"] and edges == UPGRADED["edges"]
        with pytest.raises(ValueError):
            SETUP["upgrade_workflow"]([{"id": "n_start", "kind": "start"}], [])

    def test_summary_only_policy_keeps_the_live_engine_derived(self):
        policy = _policy()
        assert policy.source == "derived"
        assert [f.name for f in policy.summary_fields] == [f["name"] for f in SUMMARY_FIELDS]
        assert all(f.allow_llm is False for f in policy.summary_fields)


class TestOutcomeSlotsAndFields:
    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    async def test_final_slots_and_structured_fields(self, engine, monkeypatch, name):
        case = SCENARIOS[name]
        _, result = await _run(engine, monkeypatch, UPGRADED, case["turns"], f"{name}-slots",
                               case.get("context", CONTEXT))
        slots = result["slots"]
        for key, expected in case["slots"].items():
            assert slots.get(key) == expected, (key, slots)
        fields = derive_structured_fields(_policy(), _snapshot(slots))
        assert set(fields) == {f["name"] for f in SUMMARY_FIELDS}
        for key, expected in case["fields"].items():
            assert fields[key] == expected, (key, fields)


class TestDerivationRules:
    def test_no_workflow_at_all_leaves_every_field_null(self):
        fields = derive_structured_fields(_policy(), {})
        assert all(value is None for value in fields.values())

    def test_date_and_time_never_shown_without_a_confirmed_booking(self):
        # A day was captured but the call ended elsewhere: no appointment date.
        for status in ("date_pending", "callback_requested", "incomplete", "address_mismatch"):
            fields = derive_structured_fields(
                _policy(), {"visit_status": status, "visit_day": "kal", "visit_time": "4 PM"}
            )
            assert fields["appointment_booked"] == "No"
            assert fields["appointment_date"] is None and fields["appointment_time"] is None
        # A booking with no time still shows the day.
        fields = derive_structured_fields(_policy(), {"visit_status": "booked", "visit_day": "kal"})
        assert fields["appointment_date"] == "kal" and fields["appointment_time"] is None

    def test_callback_details_only_for_a_callback(self):
        fields = derive_structured_fields(
            _policy(), {"visit_status": "booked", "callback_date": "parso", "visit_day": "kal"}
        )
        assert fields["callback_date"] is None
        fields = derive_structured_fields(
            _policy(), {"visit_status": "callback_requested", "callback_date": "parso"}
        )
        assert fields["callback_date"] == "parso" and fields["callback_time"] is None

    def test_analyst_can_never_fill_a_field(self):
        """All fields are slot-only: an LLM guess is ignored even where the
        slot is empty, so nothing is reported without workflow evidence."""
        fields, sources = merge_structured_fields(
            _policy(), {"visit_status": "incomplete"},
            {"address_verified": "Yes", "appointment_date": "tomorrow",
             "appointment_booked": "Yes", "call_result": "booked"},
        )
        assert fields["address_verified"] is None
        assert fields["appointment_booked"] == "No"
        assert fields["appointment_date"] is None
        assert fields["call_result"] == "incomplete"
        assert set(sources.values()) == {"workflow"}

    def test_unknown_visit_status_value_is_not_a_result(self):
        fields = derive_structured_fields(_policy(), {"visit_status": "something_new"})
        assert fields["call_result"] is None
        # ...but it is still "not booked".
        assert fields["appointment_booked"] == "No"
