"""MID / outlet-name lookup in the bot (``setup/alternate_lookup``) on the
Wordline team's workflow v57.

The LOCAL workflow v57 (``data/workflow_local_v57_snapshot.json``) is
upgraded with ``upgrade_alternate_lookup`` and run through the real workflow
engine; every api node posts to the real merchant API code (FastAPI
TestClient) with the real connection mapping of setup/01 (the harness of
``test_visit_details``).  Caller lines are what the STT would deliver; the
classifier label is passed as ``signal`` where the live classifier gives one.

Not simulated: STT, audio timing, barge-in, the LLM and the classifier itself.

Run:  env/bin/pytest tenant/Wordline/tests/test_alternate_lookup.py -q
"""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

from tenant.Wordline.setup.alternate_lookup import (
    JOURNEYS, MARKER_NODE, PROMPT_DIGITS_NEW, PROMPT_FIELDS_NEW, PROMPT_ROUTING_NEW,
    PROMPT_ROUTING_OLD, upgrade_alternate_lookup, upgrade_prompt_alternate_lookup)
from tenant.Wordline.setup.workflow_upgrade import UpgradeError
from tenant.Wordline.tests.test_visit_details import CONTEXT, _ApiExecutor, _engine, _Result

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
SNAPSHOT = DATA / "workflow_local_v57_snapshot.json"
PROMPT = DATA / "prompt_local_v21_snapshot.txt"   # LOCAL system prompt v21 (published)
TENANT, BOT, WORKFLOW = "tn_ccc488c2c47d", "bot_a9d4caca70dc", "wf_0a6550d061e6"
YES = "affirm"
ASK_MID = "Please tell me your MID, that is your Merchant ID, and your outlet name."


@pytest.fixture(scope="module")
def live() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def upgraded(live) -> dict:
    return upgrade_alternate_lookup(live)[0]


class _DownExecutor(_ApiExecutor):
    """The merchant service is unreachable (→ the api node's failure edge)."""

    async def execute(self, **kw):
        self.calls.append(dict(kw["args"]))
        return _Result(False, {}, "error")


class Call:
    def __init__(self, monkeypatch, definition, session, executor=_ApiExecutor, lang="en-IN",
                 context=None):
        self.engine, self.executor = _engine(monkeypatch, definition, executor)
        self.session, self.lang, self.context = session, lang, context or CONTEXT

    async def say(self, text, signal=None):
        return await self.engine.handle_turn_detailed(
            session_id=self.session, tenant_id=TENANT, bot_id=BOT, workflow_name=WORKFLOW,
            user_text=text, language=self.lang, context_values=self.context, signal=signal)


async def no_tid(call: Call, journey: str = "paper_roll"):
    await call.say("I need paper roll" if journey == "paper_roll" else "battery problem hai")
    return await call.say("TID nahi pata")


# ── the transform ───────────────────────────────────────────────────────────


def test_transform_adds_nodes_and_retargets_four_edges(live, upgraded):
    frozen = copy.deepcopy(live)
    again, changes = upgrade_alternate_lookup(upgraded)
    assert again is upgraded and changes == []                     # idempotent
    assert upgrade_alternate_lookup(live)[0] == upgraded and live == frozen   # pure
    before = {n["id"]: n for n in live["nodes"]}
    after = {n["id"]: n for n in upgraded["nodes"]}
    assert all(after[i] == before[i] for i in before)              # no team node touched
    assert len(after) - len(before) == 16
    old_edges = {e["id"]: e for e in live["edges"]}
    new_edges = {e["id"]: e for e in upgraded["edges"]}
    assert set(old_edges) <= set(new_edges)
    moved = sorted((old_edges[i]["from"], old_edges[i]["to"], new_edges[i]["to"])
                   for i in old_edges if old_edges[i] != new_edges[i])
    assert moved == sorted([
        ("n_pr_merchant_outlet", "n_pr_merchant_noted", "n_pr_mo_api"),
        ("n_bat_merchant_outlet", "n_bat_merchant_noted", "n_bat_mo_api"),
        ("n_ask_tid_given", "n_tid_none_close", "n_pr_no_tid_bridge"),
        ("n_bat_tid_given", "n_tid_none_close", "n_bat_no_tid_bridge"),
    ])
    for i in old_edges:                                            # only targets moved
        assert {k: v for k, v in old_edges[i].items() if k != "to"} == \
               {k: v for k, v in new_edges[i].items() if k != "to"}
    assert MARKER_NODE in after


def test_transform_refuses_a_moved_anchor(live):
    moved = copy.deepcopy(live)
    edge = next(e for e in moved["edges"] if e["from"] == "n_pr_merchant_outlet"
                and e.get("label") != "fallback")
    edge["to"] = "n_options_unverified"
    with pytest.raises(UpgradeError, match="refusing to guess"):
        upgrade_alternate_lookup(moved)


def test_ambiguity_is_read_only_after_the_first_outlet_lookup(upgraded):
    """setdefault-pinned slots: merchant_ambiguous is read once per journey,
    right after the first lookup that can return several merchants."""
    readers = sorted(n["id"] for n in upgraded["nodes"]
                     if (n.get("config") or {}).get("variable") == "merchant_ambiguous")
    assert readers == ["n_bat_mo_ambiguous", "n_pr_mo_ambiguous"]
    for spec in JOURNEYS.values():
        p = spec["prefix"]
        edges = {(e["from"], e.get("label"), e["to"]) for e in upgraded["edges"]}
        assert {(f"{p}_mo_api", "success", f"{p}_mo_found"),
                (f"{p}_mo_found", "false", f"{p}_mo_ambiguous")} <= edges


# ── paper roll ──────────────────────────────────────────────────────────────


async def test_no_terminal_id_then_mid_and_outlet_finds_the_record(monkeypatch, upgraded):
    call = Call(monkeypatch, upgraded, "mo-mid")
    r = await no_tid(call)
    assert r["trace"][-2:] == ["n_pr_no_tid_bridge", "n_pr_merchant_outlet"]
    assert "n_tid_none_close" not in r["trace"] and not r["done"]
    assert r["reply"] == ("No problem, Sir. I can also verify your details through your MID and "
                          "outlet name. " + ASK_MID)
    r = await call.say("MID 0 1 1 0 6 5 6 A 0 0 0 7 8 5 6, outlet Bhanu Medical")
    assert r["trace"][1:] == ["n_pr_mo_api", "n_pr_mo_found", "n_has_ask_merchant", "n_ask_merchant"]
    assert "your Outlet Name is Bhanu Medical and General Store" in r["reply"]
    slots = r["slots"]
    assert (slots["merchant_found"], slots["customer_verified"]) == (True, True)
    assert (slots["merchant_matched_by"], slots["merchant_id"]) == ("merchant_id", "0110656A0007856")
    assert (slots["terminal_id"], slots["ctx_distance"]) == ("1231410A", 33)
    r = await call.say("haan sahi hai", YES)                        # the team's read-back chain
    assert "Is your bank name IDBI BANK?" in r["reply"]


async def test_terminal_id_not_found_then_outlet_name_only(monkeypatch, upgraded):
    call = Call(monkeypatch, upgraded, "mo-name")
    await call.say("I need paper roll")
    r = await call.say("12345678")                                  # the team's not-found step
    assert "n_pr_tid_not_found" in r["trace"] and r["trace"][-1] == "n_pr_merchant_outlet"
    r = await call.say("MID yaad nahi hai, outlet ka naam Kunal Sweets hai")
    assert "your Outlet Name is Kunal Sweets" in r["reply"]
    assert r["slots"]["merchant_matched_by"] == "merchant_name"


async def test_several_merchants_are_read_out_and_the_caller_chooses(monkeypatch, upgraded):
    call = Call(monkeypatch, upgraded, "mo-several")
    await no_tid(call)
    r = await call.say("outlet ka naam medical hai")
    assert r["trace"][-1] == "n_pr_mo_pick"
    assert r["reply"] == ("Sir, I found 5 merchants with that name, such as Bhanu Medical and "
                          "General Store, Jayalakshmi Medicals, Shaji Medicals. Please tell me the "
                          "complete registered outlet name, or your MID.")
    assert "merchant_found" not in r["slots"] and "outlet_name" not in r["slots"]
    r = await call.say("Shaji Medicals")
    assert r["trace"][1:3] == ["n_pr_mo_api_retry", "n_pr_mo_found_retry"]
    assert "your Outlet Name is Shaji Medicals" in r["reply"]


async def test_mid_given_when_several_names_match(monkeypatch, upgraded):
    call = Call(monkeypatch, upgraded, "mo-several-mid")
    await no_tid(call)
    await call.say("MID yaad nahi, outlet ka naam hotel hai")
    r = await call.say("MID 0 0 6 3 2 6 0 A 0 0 0 1 9 1 4")
    assert "your Outlet Name is Hotel The Rostral" in r["reply"]
    assert r["slots"]["merchant_matched_by"] == "merchant_id"


async def test_mid_cut_at_a_pause_gets_a_second_chance(monkeypatch, upgraded):
    call = Call(monkeypatch, upgraded, "mo-cut")
    await no_tid(call)
    r = await call.say("0 1 1 0 6 5 6")                             # the STT cut at the pause
    assert r["trace"][-1] == "n_pr_mo_retry"
    assert "Please tell me your MID and outlet name once more, slowly." in r["reply"]
    r = await call.say("0110656 A 0007856, Bhanu Medical")
    assert "your Outlet Name is Bhanu Medical and General Store" in r["reply"]


async def test_not_found_twice_continues_unverified_as_before(monkeypatch, upgraded):
    call = Call(monkeypatch, upgraded, "mo-miss")
    await no_tid(call)
    r = await call.say("MID 1 2 3 4 5 6 7 8 9 0 1 2, Gupta Electronics")
    assert r["trace"][-1] == "n_pr_mo_retry"
    r = await call.say("Gupta Electronics")
    assert "n_pr_merchant_noted" in r["trace"] and "n_options_unverified" in r["trace"]
    assert "I have noted your MID and outlet name." in r["reply"]
    assert not r["done"] and "merchant_found" not in r["slots"]


async def test_several_then_still_ambiguous_continues_unverified(monkeypatch, upgraded):
    call = Call(monkeypatch, upgraded, "mo-several-miss")
    await no_tid(call)
    await call.say("medical")
    r = await call.say("medicals")                                  # still three matches
    assert "n_pr_merchant_noted" in r["trace"] and "merchant_found" not in r["slots"]


async def test_service_down_takes_the_teams_noted_step(monkeypatch, upgraded):
    call = Call(monkeypatch, upgraded, "mo-down", executor=_DownExecutor)
    await no_tid(call)
    r = await call.say("MID 0110656A0007856, Bhanu Medical")
    assert r["trace"][1:3] == ["n_pr_mo_api", "n_pr_merchant_noted"]


async def test_hindi(monkeypatch, upgraded):
    call = Call(monkeypatch, upgraded, "mo-hi", lang="hi-IN")
    await call.say("paper roll chahiye")
    r = await call.say("मेरे पास टीआईडी नहीं है")
    assert r["reply"].startswith("कोई बात नहीं Sir। मैं आपकी details आपके MID और outlet के नाम से "
                                 "भी verify कर सकता हूँ।")
    r = await call.say("एमआईडी याद नहीं, आउटलेट होटल है")
    assert ("Sir, इस नाम से मुझे 2 merchants मिले, जैसे Hotel Ida, Hotel The Rostral। कृपया अपने "
            "outlet का पूरा registered नाम, या अपना MID बताइए।") == r["reply"]
    r = await call.say("होटल आईडा")                                      # the second answer decides
    assert "n_pr_mo_found_retry" in r["trace"] and "Hotel Ida" in r["reply"]
    call2 = Call(monkeypatch, upgraded, "mo-hi-2", lang="hi-IN")
    await call2.say("paper roll chahiye")
    await call2.say("टीआईडी याद नहीं है")
    r = await call2.say("मेरा आउटलेट शाजी मेडिकल्स है, एमआईडी नहीं पता")
    assert "n_has_ask_merchant" in r["trace"] and "Shaji Medicals" in r["reply"]


# ── battery: the record's distance times the engineer visit ─────────────────


async def _battery_to_docket(call: Call, answer: str) -> dict:
    await no_tid(call, "battery")
    r = await call.say(answer)
    assert "n_bat_mo_api" in r["trace"] and "n_has_bat_details" in r["trace"]
    for text, signal in (("haan sahi hai", YES),) * 4 + (
            ("haan switch off kar diya", YES), ("battery phool gayi hai, swollen hai", None),
            ("haan aage badho", YES), ("haan sahi hai", YES), ("Sunday off hai, 11 se 6", YES),
            ("haan sahi hai", YES)):
        r = await call.say(text, signal)
    return await call.say("haan", YES)


@pytest.mark.parametrize("answer, distance, visit", [
    ("MID 0791372A0121679, outlet Eye World Optics", 62, "n_bat_visit_far"),       # > 50
    ("outlet Snacks Centre, MID yaad nahi", 29, "n_bat_visit_near"),               # <= 50
])
async def test_battery_visit_timing_uses_the_records_distance(monkeypatch, upgraded, answer,
                                                              distance, visit):
    call = Call(monkeypatch, upgraded, f"mo-bat-{distance}")
    r = await _battery_to_docket(call, answer)
    assert r["slots"]["ctx_distance"] == distance
    assert visit in r["trace"] and "n_bat_visit_default" not in r["trace"]
    timing = "within 24 to 48 hours" if visit.endswith("near") else "in 5 working days"
    assert f"Our Field Service Engineer will visit {timing}" in r["reply"]


async def test_call_data_distance_still_used_without_a_found_record(monkeypatch, upgraded):
    """Unverified caller: the team's prefill from the call data decides."""
    call = Call(monkeypatch, upgraded, "mo-bat-ctx", context={**CONTEXT, "distance": 12})
    await no_tid(call, "battery")
    r = await call.say("MID 1 2 3 4 5 6 7 8 9 0 1 2, Gupta Electronics")
    r = await call.say("Gupta Electronics")
    assert "n_bat_merchant_noted" in r["trace"] and "ctx_distance" not in r["slots"]


# ── the Terminal ID path is unchanged ───────────────────────────────────────


@pytest.mark.parametrize("opening, script", [
    ("I need paper roll", [("91379784", None), ("haan sahi hai", YES), ("haan sahi hai", YES)]),
    ("I need paper roll", [("12345678", None)]),
    ("battery problem hai", [("1974857A", None), ("haan sahi hai", YES)]),
])
async def test_terminal_id_conversations_identical_before_and_after(monkeypatch, live, upgraded,
                                                                    opening, script):
    replies = {}
    for name, definition in (("before", live), ("after", upgraded)):
        call = Call(monkeypatch, definition, f"same-{name}-{opening[:5]}-{len(script)}")
        turns = [await call.say(opening)]
        for text, signal in script:
            turns.append(await call.say(text, signal))
        replies[name] = [(t["reply"], t["trace"]) for t in turns]
    assert replies["before"] == replies["after"]


# ── system prompt ───────────────────────────────────────────────────────────


def test_prompt_edits_are_exact_and_idempotent():
    text = PROMPT.read_text(encoding="utf-8")
    new = upgrade_prompt_alternate_lookup(text)
    assert new is not None and PROMPT_ROUTING_OLD not in new
    for part in (PROMPT_ROUTING_NEW, PROMPT_DIGITS_NEW, PROMPT_FIELDS_NEW):
        assert new.count(part) == 1
    assert upgrade_prompt_alternate_lookup(new) is None
    assert len(new.splitlines()) == len(text.splitlines()) + 1      # one added rule line
    with pytest.raises(UpgradeError):
        upgrade_prompt_alternate_lookup(text.replace("Every journey requires Terminal ID",
                                                     "Every journey needs Terminal ID"))
