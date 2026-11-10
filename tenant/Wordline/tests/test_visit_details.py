"""Step 3 — record-aware request / visit asks, driven end to end offline.

The CURRENT Wordline workflow (DEMO v17 == LOCAL v14, the Wordline team's
latest save, ``data/workflow_demo_v17_snapshot.json``) is upgraded with
``upgrade_all`` and run through the real workflow engine.  The api node's
executor posts the engine's slot args to the REAL merchant API code (FastAPI
TestClient) and applies the REAL connection settings of setup/01 (success
condition, response schema, response mapping) — so the spoken lines below
are exactly what the bot would say on a call, minus the network.

``_OldServiceExecutor`` reproduces DEMO before its code deploy: the previous
response shape (ALL-CAPS values, no address / contact person).  With it the
upgraded workflow must behave exactly like the original one.

Run:  env/bin/pytest tenant/Wordline/tests -q
"""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.tool_executor as te
import shared.orchestration.workflow_engine as wfe
from shared.orchestration.tool_executor import ToolExecutor, validate_response
from tenant.Wordline.api import main as api
from tenant.Wordline.setup import workflow_upgrade as up

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
SNAPSHOT = DATA / "workflow_demo_v17_snapshot.json"
SETUP = pathlib.Path(__file__).resolve().parent.parent / "setup" / "01_api_connection.py"
KEY = "visit-key"
CONTEXT = {"customer_name": "Rahul Sharma", "docket_number": "WL458921"}

AGARWAL_ADDRESS = "Agarwal Public School, Bicholi Mardana Road, Indore, Madhya Pradesh 452016"


def _connection() -> dict:
    spec = importlib.util.spec_from_file_location("wordline_setup_01_visit", SETUP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    c = module.CONNECTION
    return {"success_condition": c["successCondition"], "response_schema": c["responseSchema"],
            "response_mapping": c["responseMapping"]}


class _Result:
    def __init__(self, ok: bool, mapped: dict, status: str = "ok"):
        self.ok, self.mapped, self.status, self.mocked = ok, mapped, status, False


class _ApiExecutor:
    """The engine's api-node call → real merchant API → real mapping."""

    OLD_SHAPE = False

    def __init__(self, client: TestClient):
        self.client, self.connection, self.calls = client, _connection(), []

    def _body(self, args: dict) -> tuple[int, dict]:
        r = self.client.post("/api/v1/merchants/lookup", json={"tid": "{{customer_phone}}", **args},
                             headers={"X-API-Key": KEY})
        return r.status_code, r.json()

    async def execute(self, **kw):
        self.calls.append(dict(kw["args"]))
        status, body = self._body(kw["args"])
        ok = ToolExecutor._success(self.connection, status)
        if ok and validate_response(self.connection["response_schema"], body):
            ok = False
        mapped = ToolExecutor._apply_mapping(self.connection, body) if ok else {}
        return _Result(ok, mapped, "ok" if ok else "error")


class _OldServiceExecutor(_ApiExecutor):
    """DEMO before the code deploy: previous response shape."""

    def _body(self, args: dict) -> tuple[int, dict]:
        status, body = super()._body(args)
        if body.get("lookup_status") == "found":
            record = api.store.find_terminal(body["terminal_id"]).record
            body = {k: v for k, v in body.items() if k not in api.VISIT_FIELDS}
            body.update({k: record[k] for k in ("outlet_name", "asset_type")})   # ALL CAPS
        return status, body


@pytest.fixture(scope="module")
def snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def upgraded(snapshot) -> dict:
    definition, _changes = up.upgrade_all(snapshot)
    return definition


def _engine(monkeypatch, definition, executor_cls):
    monkeypatch.setenv(api.API_KEY_ENV, KEY)
    api.store.refresh(force=True)
    eng = wfe.WorkflowEngine()

    async def _mem(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem)
    monkeypatch.setattr(wfe, "load_workflow_definition",
                        lambda tenant_id, bot_id, name, pinned_versions=None: definition)
    executor = executor_cls(TestClient(api.app))
    monkeypatch.setattr(te, "get_tool_executor", lambda: executor)
    return eng, executor


async def _say(engine, session, text, lang="en-IN"):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_ccc488c2c47d", bot_id="bot_a9d4caca70dc",
        workflow_name="wf_0a6550d061e6", user_text=text, language=lang, context_values=CONTEXT)


async def _to_paper_roll_options(engine, session, tid="91379784", lang="en-IN"):
    turns = [await _say(engine, session, "I need paper roll", lang)]
    turns.append(await _say(engine, session, tid, lang))                  # lookup → outlet read-back
    for answer in ("haan sahi hai", "haan sahi hai", "haan sahi hai"):    # outlet, bank, model
        turns.append(await _say(engine, session, answer, lang))
    return turns


# ── structure ───────────────────────────────────────────────────────────────


def test_step3_only_adds_gates_and_record_asks(snapshot):
    # Step 3 alone on top of steps 1-2 (the v17 snapshot already has them).
    upgraded, _changes = up.upgrade_visit_details(snapshot)
    before = {n["id"]: n for n in snapshot["nodes"]}
    after = {n["id"]: n for n in upgraded["nodes"]}
    added = set(after) - set(before)
    assert added == {"n_raise_has_record", "n_raise_record",
                     "n_bat_visit_has_record", "n_bat_visit_record"}
    for node_id, node in before.items():               # every existing node untouched
        assert after[node_id] == node, node_id
    old_edges = {e["id"]: e for e in snapshot["edges"]}
    new_edges = {e["id"]: e for e in upgraded["edges"]}
    assert set(old_edges) <= set(new_edges)
    retargeted = {eid for eid in old_edges if old_edges[eid] != new_edges[eid]}
    for eid in retargeted:                              # only the target changed
        old, new = old_edges[eid], new_edges[eid]
        assert {k: v for k, v in old.items() if k != "to"} == {k: v for k, v in new.items() if k != "to"}
        assert (old["to"], new["to"]) in {("n_raise", "n_raise_has_record"),
                                          ("n_bat_visit", "n_bat_visit_has_record")}
    assert len(retargeted) == 4                         # 3 into n_raise, 1 into n_bat_visit
    out = {}
    for e in upgraded["edges"]:
        out.setdefault(e["from"], {})[e.get("label") or ""] = e["to"]
    assert out["n_raise_has_record"] == {"true": "n_raise_record", "false": "n_raise"}
    assert out["n_bat_visit_has_record"] == {"true": "n_bat_visit_record", "false": "n_bat_visit"}
    assert out["n_raise_record"] == out["n_raise"] == {"": "n_anything_else"}
    assert out["n_bat_visit_record"] == out["n_bat_visit"] == {"": "n_has_docket"}
    for record, original in (("n_raise_record", "n_raise"), ("n_bat_visit_record", "n_bat_visit")):
        cfg, ocfg = after[record]["config"], after[original]["config"]
        assert cfg["variable"] == ocfg["variable"] and cfg["entityType"] == ocfg["entityType"]
        assert cfg["rejectAnswerPatterns"] == ocfg["rejectAnswerPatterns"]
        assert "{" not in cfg["question"]
        assert not any("{" in text for text in cfg["textByLanguage"].values())
    again, changes = up.upgrade_visit_details(upgraded)
    assert again is upgraded and "already upgraded" in changes[-1]


def test_step3_requires_step_one():
    with pytest.raises(up.UpgradeError, match="upgrade_workflow"):
        up.upgrade_visit_details({"nodes": [{"id": "n_start", "kind": "start"}], "edges": []})


# ── engine replay: new service (LOCAL now, DEMO after the code deploy) ──────


async def test_readbacks_speak_tts_ready_values(monkeypatch, upgraded):
    engine, _ = _engine(monkeypatch, upgraded, _ApiExecutor)
    turns = await _to_paper_roll_options(engine, "v3-readback")
    assert "your Outlet Name is Agarwal Public School." in turns[1]["reply"]
    assert "Is your bank name AXIS Bank?" in turns[2]["reply"]                    # unchanged field
    assert "Is your terminal model Paytivo 7210 2G plus WiFi?" in turns[3]["reply"]
    assert "n_options" in turns[4]["trace"]
    slots = turns[4]["slots"]
    assert slots["address"] == AGARWAL_ADDRESS and slots["contact_person"] == "K C Tiwari"


async def test_paper_roll_raise_reads_address_and_contact_person(monkeypatch, upgraded):
    engine, _ = _engine(monkeypatch, upgraded, _ApiExecutor)
    await _to_paper_roll_options(engine, "v3-raise")
    r = await _say(engine, "v3-raise", "aap request raise kar do")
    assert "n_raise_has_record" in r["trace"] and "n_raise_record" in r["trace"]
    assert "n_raise" not in r["trace"]
    assert (f"As per our records, your address is {AGARWAL_ADDRESS}, and the contact person is "
            "K C Tiwari.") in r["reply"]
    assert "First, please share your Merchant or Outlet Name" not in r["reply"]
    assert "{" not in r["reply"]
    r = await _say(engine, "v3-raise", "Sab sahi hai, weekly off Sunday, 10 se 6 baje, 5 rolls chahiye")
    assert r["slots"]["request_details"].startswith("Sab sahi hai")
    assert "n_anything_else" in r["trace"]


async def test_paper_roll_raise_in_hindi(monkeypatch, upgraded):
    engine, _ = _engine(monkeypatch, upgraded, _ApiExecutor)
    await _to_paper_roll_options(engine, "v3-raise-hi", lang="hi-IN")
    r = await _say(engine, "v3-raise-hi", "aap request raise kar do", "hi-IN")
    assert "n_raise_record" in r["trace"]
    assert f"हमारे records के अनुसार आपका address {AGARWAL_ADDRESS} है" in r["reply"]
    assert "contact person K C Tiwari हैं" in r["reply"]


async def test_battery_visit_reads_address_and_contact_person(monkeypatch, upgraded):
    engine, _ = _engine(monkeypatch, upgraded, _ApiExecutor)
    session = "v3-battery"
    await _say(engine, session, "battery problem hai")
    r = await _say(engine, session, "1974857A")                         # EYE WORLD OPTICS
    assert "your Outlet Name is Eye World Optics." in r["reply"]
    for answer in ("haan sahi hai", "haan sahi hai", "haan sahi hai", "haan sahi hai"):
        r = await _say(engine, session, answer)                         # outlet, bank, model, contact
    assert "n_bat_switch_off" in r["trace"]
    r = await _say(engine, session, "haan switch off kar diya")
    assert "n_bat_check" in r["trace"]
    r = await _say(engine, session, "battery phool gayi hai, swollen hai")
    assert "n_bat_damaged" in r["trace"]
    r = await _say(engine, session, "haan aage badho")
    assert "n_bat_visit_record" in r["trace"] and "n_bat_visit" not in r["trace"]
    assert ("your address is Shop Number 17, Saifee Park, 680 Dr. Mascarenhas Road, Mazgaon, "
            "Mumbai, Maharashtra 400010, and the contact person is Ali H.") in r["reply"]
    r = await _say(engine, session, "address sahi hai, Sunday off hai, subah 11 baje aa jaiye")
    assert "n_has_docket" in r["trace"] and "n_bat_docket" in r["trace"]
    assert r["slots"]["visit_details"].startswith("address sahi hai")


async def test_unverified_caller_still_gets_the_original_ask(monkeypatch, upgraded):
    engine, _ = _engine(monkeypatch, upgraded, _ApiExecutor)
    session = "v3-unverified"
    await _say(engine, session, "I need paper roll")
    await _say(engine, session, "12345678")                            # miss → retry ask
    r = await _say(engine, session, "87654321")                        # miss again → unverified
    assert "n_options_unverified" in r["trace"]
    r = await _say(engine, session, "aap request raise kar do")
    assert "n_raise_has_record" in r["trace"] and "n_raise" in r["trace"]
    assert "n_raise_record" not in r["trace"]
    assert "First, please share your Merchant or Outlet Name" in r["reply"]


# ── engine replay: DEMO before its code deploy (previous service) ───────────


async def test_previous_service_keeps_the_original_behaviour(monkeypatch, snapshot, upgraded):
    """Same conversation on the ORIGINAL v17 and on the upgraded workflow with
    the previous service: identical replies at every turn."""
    script = ["I need paper roll", "91379784", "haan sahi hai", "haan sahi hai", "haan sahi hai",
              "aap request raise kar do", "weekly off Sunday, 5 rolls"]
    replies = {}
    for name, definition in (("original", snapshot), ("upgraded", upgraded)):
        engine, _ = _engine(monkeypatch, definition, _OldServiceExecutor)
        replies[name] = [(await _say(engine, f"v3-old-{name}", text))["reply"] for text in script]
    assert replies["original"] == replies["upgraded"]
    assert "AGARWAL PUBLIC SCHOOL" in replies["upgraded"][1]            # old service: caps values
    assert "First, please share your Merchant or Outlet Name" in replies["upgraded"][5]


async def test_previous_service_battery_visit_unchanged(monkeypatch, snapshot, upgraded):
    script = ["battery problem hai", "1974857A", "haan sahi hai", "haan sahi hai", "haan sahi hai",
              "haan sahi hai", "haan switch off kar diya", "battery phool gayi hai, swollen hai",
              "haan aage badho"]
    replies = {}
    for name, definition in (("original", snapshot), ("upgraded", copy.deepcopy(upgraded))):
        engine, _ = _engine(monkeypatch, definition, _OldServiceExecutor)
        replies[name] = [(await _say(engine, f"v3-oldbat-{name}", text))["reply"] for text in script]
    assert replies["original"] == replies["upgraded"]
    assert "Complete Address" in replies["upgraded"][-1]
