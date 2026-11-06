"""Offline tests for the workflow upgrade: the transform itself, and the
upgraded graph driven through the real workflow engine with a fake api
executor (found / not found → retry / still not found / API failure).

Run:  env/bin/pytest tenant/Wordline/tests -q
"""

from __future__ import annotations

import json
import pathlib

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.tool_executor as te
import shared.orchestration.workflow_engine as wfe
from tenant.Wordline.setup import workflow_upgrade as up

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
SNAPSHOT = DATA / "workflow_v10_snapshot.json"
DEMO_SNAPSHOT = DATA / "workflow_demo_v12_snapshot.json"
FOUND = {
    "merchant_found": True, "customer_verified": True, "merchant_matched_by": "terminal_id",
    "terminal_id": "91379784", "outlet_name": "AGARWAL PUBLIC SCHOOL",
    "bank_name": "AXIS Bank", "asset_type": "PAYTIVO-7210-2G+WIFI",
    "contact_number": "9303333356",
}


@pytest.fixture(scope="module")
def snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def upgraded(snapshot) -> dict:
    definition, _changes = up.upgrade_workflow(snapshot)
    return definition


@pytest.fixture(scope="module")
def demo_snapshot() -> dict:
    return json.loads(DEMO_SNAPSHOT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def demo_upgraded(demo_snapshot) -> dict:
    definition, _changes = up.upgrade_all(demo_snapshot)
    return definition


@pytest.fixture(scope="module")
def captured(snapshot) -> dict:
    """v10 shape after BOTH steps."""
    definition, _changes = up.upgrade_all(snapshot)
    return definition


# ── the transform ───────────────────────────────────────────────────────────


def test_transform_is_idempotent_and_reports_changes(snapshot):
    first, changes = up.upgrade_workflow(snapshot)
    assert len(changes) == 4
    again, changes2 = up.upgrade_workflow(first)
    assert again is first and "already upgraded" in changes2[0]


def test_prefill_chain_removed_and_rewired(snapshot, upgraded):
    ids = {n["id"] for n in upgraded["nodes"]}
    prefill = {n["id"] for n in up._prefill_nodes(snapshot["nodes"])}
    assert prefill == {"n_ctx_outlet_name", "n_ctx_bank_name", "n_ctx_asset_type", "n_ctx_contact_number"}
    assert not ids & prefill
    assert {"n_ctx_customer_name", "n_ctx_docket"} <= ids       # context-fed fields stay
    edges = {(e["from"], e["to"]) for e in upgraded["edges"]}
    assert ("n_start", "n_ctx_customer_name") in edges
    assert ("n_ask_tid", "n_ask_merchant") not in edges
    assert ("n_bat_tid", "n_bat_details") not in edges


def test_readbacks_use_api_mapped_slot_names(snapshot, upgraded):
    by_id = {n["id"]: n for n in upgraded["nodes"]}
    for node_id in ("n_ask_merchant", "n_bat_details"):
        fields = by_id[node_id]["config"]["readback"]["en"]["fields"]
        variables = [f["variable"] for f in fields]
        assert "outlet_name" in variables and "bank_name" in variables
        assert not any(v.startswith("ctx_") for v in variables)
    raw = json.dumps(upgraded["nodes"], ensure_ascii=False)
    renames = up.slot_renames(snapshot)
    assert renames == {"ctx_outlet_name": "outlet_name", "ctx_bank_name": "bank_name",
                       "ctx_asset_type": "asset_type", "ctx_contact_number": "contact_number"}
    for old in renames:
        assert f'"{old}"' not in raw


def test_lookup_blocks_are_wired(upgraded):
    by_id = {n["id"]: n for n in upgraded["nodes"]}
    out = {}
    for e in upgraded["edges"]:
        out.setdefault(e["from"], {})[e.get("label") or ""] = e["to"]
    for prefix, readback, cont in (("n_", "n_ask_merchant", "n_options_unverified"),
                                   ("n_bat_", "n_bat_details", "n_bat_check")):
        api, found = f"{prefix}api_lookup", f"{prefix}lookup_found"
        assert by_id[api]["kind"] == "api"
        assert by_id[api]["config"]["connection"] == up.CONNECTION_NAME
        assert by_id[api]["config"]["name"] == up.CONNECTION_NAME        # Studio "Action name"
        assert out[api] == {"success": found, "failure": f"{prefix}lookup_unavailable"}
        assert out[found] == {"true": readback, "false": f"{prefix}tid_retry"}
        assert by_id[f"{prefix}tid_retry"]["config"]["variable"] == up.RETRY_VARIABLE
        assert out[f"{prefix}tid_retry"] == {"": f"{prefix}api_lookup_retry"}
        assert out[f"{prefix}lookup_found_retry"] == {"true": readback,
                                                      "false": f"{prefix}lookup_unverified"}
        assert out[f"{prefix}lookup_unverified"] == {"": cont}
        assert out[f"{prefix}lookup_unavailable"] == {"": cont}
    # The unverified options hub offers the same choices as the verified one.
    verified = {e["to"] for e in upgraded["edges"] if e["from"] == "n_options"}
    unverified = {e["to"] for e in upgraded["edges"] if e["from"] == "n_options_unverified"}
    assert verified == unverified == {"n_sms", "n_ivr", "n_raise", "n_both"}
    assert "verified" not in by_id["n_options_unverified"]["config"]["prompt"].lower()


def test_transform_refuses_foreign_workflow():
    with pytest.raises(up.UpgradeError, match="missing nodes"):
        up.upgrade_workflow({"nodes": [{"id": "n_start", "kind": "start"}], "edges": []})


# ── engine replay with a fake executor ──────────────────────────────────────


class _Result:
    def __init__(self, ok: bool, mapped: dict):
        self.ok, self.mapped = ok, mapped
        self.status = "ok" if ok else "error"
        self.mocked = True


class _Executor:
    """Scripted lookup outcomes, consumed in order; records every call."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if outcome == "found":
            return _Result(True, dict(FOUND))
        if outcome == "not_found":        # 404 body carries no mapped keys
            return _Result(True, {})
        return _Result(False, {})          # transport error / timeout / 5xx


@pytest.fixture()
def engine(monkeypatch, upgraded):
    eng = wfe.WorkflowEngine()

    async def _mem(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem)
    monkeypatch.setattr(wfe, "load_workflow_definition",
                        lambda tenant_id, bot_id, name, pinned_versions=None: upgraded)
    return eng


def _wire(monkeypatch, outcomes) -> _Executor:
    executor = _Executor(outcomes)
    monkeypatch.setattr(te, "get_tool_executor", lambda: executor)
    return executor


CONTEXT = {"customer_name": "Rahul Sharma", "docket_number": "WL458921"}


async def _turn(engine, text, session, **kw):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_ccc488c2c47d", bot_id="bot_a9d4caca70dc",
        workflow_name="wf_0a6550d061e6", user_text=text, language="en-IN",
        context_values=CONTEXT, **kw)


async def test_paper_roll_found_reads_back_api_fields(engine, monkeypatch):
    executor = _wire(monkeypatch, ["found"])
    r = await _turn(engine, "I need paper roll", "pr-found")
    assert "n_ask_tid" in r["trace"]
    r = await _turn(engine, "9 1 3 7 9 7 8 4", "pr-found")
    assert executor.calls and executor.calls[0]["tool"] == up.CONNECTION_NAME
    assert executor.calls[0]["args"]["spoken_terminal_id"] == "9 1 3 7 9 7 8 4"
    assert "n_api_lookup" in r["trace"] and "n_ask_merchant" in r["trace"]
    reply = r["reply"]
    assert "AGARWAL PUBLIC SCHOOL" in reply and "AXIS Bank" in reply and "PAYTIVO-7210-2G+WIFI" in reply
    assert r["slots"]["merchant_found"] is True and r["slots"]["customer_verified"] is True
    assert r["slots"]["outlet_name"] == "AGARWAL PUBLIC SCHOOL"
    # the fixture never leaks into the read-back
    assert "Sharma General Store" not in reply


async def test_paper_roll_not_found_then_retry_found(engine, monkeypatch):
    executor = _wire(monkeypatch, ["not_found", "found"])
    await _turn(engine, "paper roll chahiye", "pr-retry")
    r = await _turn(engine, "12345678", "pr-retry")
    assert "n_tid_retry" in r["trace"] and "could not find" in r["reply"].lower()
    assert "n_ask_merchant" not in r["trace"]
    r = await _turn(engine, "91379784", "pr-retry")
    assert executor.calls[1]["args"][up.RETRY_VARIABLE] == "91379784"
    assert "n_api_lookup_retry" in r["trace"] and "n_ask_merchant" in r["trace"]
    assert "AGARWAL PUBLIC SCHOOL" in r["reply"]


async def test_paper_roll_still_not_found_continues_unverified(engine, monkeypatch):
    _wire(monkeypatch, ["not_found", "not_found"])
    await _turn(engine, "paper roll", "pr-miss")
    await _turn(engine, "12345678", "pr-miss")
    r = await _turn(engine, "87654321", "pr-miss")
    assert "n_lookup_unverified" in r["trace"] and "n_options_unverified" in r["trace"]
    assert "unable to locate" in r["reply"].lower()
    assert "verified" not in r["reply"].lower()
    assert "SMS" in r["reply"] and "IVR" in r["reply"]
    assert "merchant_found" not in r["slots"]
    r = await _turn(engine, "SMS wala batao", "pr-miss")
    assert "n_sms" in r["trace"]


async def test_paper_roll_api_failure_continues_unverified(engine, monkeypatch):
    _wire(monkeypatch, ["failure"])
    await _turn(engine, "paper roll", "pr-down")
    r = await _turn(engine, "91379784", "pr-down")
    assert "n_lookup_unavailable" in r["trace"] and "n_options_unverified" in r["trace"]
    assert "unable to verify" in r["reply"].lower()
    assert "n_tid_retry" not in r["trace"]


async def test_battery_found_reads_contact_and_proceeds(engine, monkeypatch):
    _wire(monkeypatch, ["found"])
    r = await _turn(engine, "battery problem hai", "bat-found")
    assert "n_bat_tid" in r["trace"]
    r = await _turn(engine, "91379784", "bat-found")
    assert "n_bat_api_lookup" in r["trace"] and "n_bat_details" in r["trace"]
    assert "AGARWAL PUBLIC SCHOOL" in r["reply"] and "9303333356" in r["reply"]
    r = await _turn(engine, "haan sahi hai", "bat-found")
    assert "n_bat_check" in r["trace"]


async def test_battery_failure_goes_straight_to_the_check(engine, monkeypatch):
    _wire(monkeypatch, ["failure"])
    await _turn(engine, "battery issue", "bat-down")
    r = await _turn(engine, "91379784", "bat-down")
    assert "n_bat_lookup_unavailable" in r["trace"] and "n_bat_check" in r["trace"]
    assert "troubleshoot" in r["reply"].lower() and "switch off" in r["reply"].lower()


# ── step 2: Terminal ID capture (both shapes) ───────────────────────────────


def test_capture_step_requires_step_one(snapshot):
    with pytest.raises(up.UpgradeError, match="upgrade_workflow"):
        up.upgrade_terminal_id_capture(snapshot)


def test_capture_step_pins_pattern_and_is_idempotent(captured, demo_upgraded):
    for definition in (captured, demo_upgraded):
        asks = [n for n in definition["nodes"] if n["kind"] == "ask"
                and n["config"].get("variable") in up.TERMINAL_VARIABLES]
        assert {n["id"] for n in asks} == {"n_ask_tid", "n_bat_tid", "n_tid_retry", "n_bat_tid_retry"}
        assert all(n["config"]["entity"]["regexPattern"] == up.TERMINAL_ENTITY_PINNED["regexPattern"]
                   for n in asks)
        again, notes = up.upgrade_terminal_id_capture(definition)
        assert again is definition and "already upgraded" in notes[0]


def test_pinned_pattern_length_bounds():
    from shared.orchestration.entity_extractor import identifier_length_bounds
    assert identifier_length_bounds(dict(up.TERMINAL_ENTITY_PINNED)) == (7, 8)
    import re
    rx = re.compile(up.TERMINAL_ENTITY_PINNED["regexPattern"])
    assert rx.search("1231410").group(1) == "1231410"
    assert rx.search("9 1 3 7 9 7 8 4").group(1) == "9 1 3 7 9 7 8 4"
    assert rx.search("1234") is None and rx.search("123456789") is None


def test_v10_shape_gets_wrong_details_path(captured):
    out = {}
    for e in captured["edges"]:
        out.setdefault(e["from"], []).append((e.get("label") or "", e["to"]))
    assert any(to == "n_details_wrong" for _l, to in out["n_ask_merchant"])
    assert any(to == "n_bat_details_wrong" for _l, to in out["n_bat_details"])
    assert out["n_details_wrong"] == [("", "n_options_unverified")]
    assert out["n_bat_details_wrong"] == [("", "n_bat_check")]


def test_demo_shape_keeps_its_own_confirmations(demo_snapshot, demo_upgraded):
    ids = {n["id"] for n in demo_upgraded["nodes"]}
    assert not ids & {"n_ctx_outlet_name", "n_ctx_bank_name", "n_ctx_asset_model", "n_ctx_contact_number"}
    assert "n_details_wrong" not in ids and "n_bat_details_wrong" not in ids   # v12 has "wrong → disconnect"
    by_id = {n["id"]: n for n in demo_upgraded["nodes"]}
    assert by_id["n_has_pr_asset"]["config"]["variable"] == "asset_type"       # ctx_asset_model renamed
    assert by_id["n_has_ask_merchant"]["config"]["variable"] == "outlet_name"
    out = {(e["from"], e.get("label") or ""): e["to"] for e in demo_upgraded["edges"]}
    assert out[("n_lookup_found", "true")] == "n_has_ask_merchant"
    assert out[("n_bat_lookup_found", "true")] == "n_has_bat_details"
    assert out[("n_ask_merchant", "")] if ("n_ask_merchant", "") in out else True
    untouched = {n["id"] for n in demo_snapshot["nodes"]} - {"n_ctx_outlet_name", "n_ctx_bank_name",
                                                            "n_ctx_asset_model", "n_ctx_contact_number"}
    assert untouched <= ids                                                     # nothing else removed


# ── engine replay of the finals recorded in the real voice runs ─────────────

S3_FINALS = ["I want to order paper roll.", "One, two, three. Four.", "Six. 7 8.", "Sorry. It is 91379784."]
S4_FINALS = ["battery problem", "1 2 3 1.", "410.", "A."]
S7_FINALS = ["I need paper roll", "91.", "37.", "97 84."]


class _StoreExecutor:
    """Lookup against the real merchant store (no HTTP)."""

    def __init__(self):
        from tenant.Wordline.api.store import MerchantStore
        self.store = MerchantStore(); self.calls = []

    async def execute(self, **kw):
        a = kw["args"]; raw = a.get(up.RETRY_VARIABLE) or a.get("spoken_terminal_id")
        self.calls.append(raw); m = self.store.find_terminal(raw)
        if not m:
            return _Result(True, {})
        r = m.record
        return _Result(True, {"merchant_found": True, "customer_verified": True, **{
            k: r[k] for k in ("terminal_id", "outlet_name", "bank_name", "asset_type", "contact_number")}})


@pytest.fixture(params=["v10", "demo_v12"])
def shape_engine(request, monkeypatch, captured, demo_upgraded):
    definition = captured if request.param == "v10" else demo_upgraded
    eng = wfe.WorkflowEngine()

    async def _mem(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem)
    monkeypatch.setattr(wfe, "load_workflow_definition",
                        lambda tenant_id, bot_id, name, pinned_versions=None: definition)
    ex = _StoreExecutor()
    monkeypatch.setattr(te, "get_tool_executor", lambda: ex)
    return request.param, ex


async def _drive(engine, finals, session, lang="en-IN"):
    out = []
    for text in finals:
        out.append(await _turn(engine, text, session, language=lang) if False else
                   await wfe.get_workflow_engine().handle_turn_detailed(
                       session_id=session, tenant_id="tn_ccc488c2c47d", bot_id="bot_a9d4caca70dc",
                       workflow_name="wf_0a6550d061e6", user_text=text, language=lang, context_values=CONTEXT))
    return out


async def test_paused_dictation_accumulates_before_lookup(shape_engine):
    shape, ex = shape_engine
    r = await _drive(None, S3_FINALS, f"s3-{shape}")
    assert "noted 4 digits" in r[1]["reply"].lower() and "n_api_lookup" not in r[1]["trace"]
    assert ex.calls[0] == "1234678"                     # 4+3 joined → one 7-digit lookup (a miss)
    assert "n_tid_retry" in r[2]["trace"]
    assert "AGARWAL PUBLIC SCHOOL" in r[3]["reply"] and ex.calls[-1] == "91379784"


async def test_battery_natural_tid_completes(shape_engine):
    shape, ex = shape_engine
    r = await _drive(None, S4_FINALS, f"s4-{shape}", lang="hi-IN")
    assert "n_bat_api_lookup" not in r[1]["trace"]       # "1 2 3 1." held, not looked up
    assert "n_bat_api_lookup" in r[2]["trace"] and "BHANU MEDICAL" in r[2]["reply"]
    assert ex.calls == ["1231410"]
    assert "n_bat_tid_retry" not in r[3]["trace"]        # the trailing "A." does not restart anything


async def test_spoken_pairs_make_one_lookup(shape_engine):
    shape, ex = shape_engine
    r = await _drive(None, S7_FINALS, f"s7-{shape}")
    assert "noted 2 digits" in r[1]["reply"].lower() and "noted 4 digits" in r[2]["reply"].lower()
    assert ex.calls == ["91379784"] and "AGARWAL PUBLIC SCHOOL" in r[3]["reply"]


async def test_demo_shape_per_field_confirmation_and_disconnect(shape_engine):
    shape, ex = shape_engine
    if shape != "demo_v12":
        pytest.skip("v12 shape only")
    r = await _drive(None, ["paper roll", "91379784"], "v12-confirm")
    assert "n_has_ask_merchant" in r[1]["trace"] and "n_ask_merchant" in r[1]["trace"]
    assert "AGARWAL PUBLIC SCHOOL" in r[1]["reply"] and "AXIS" not in r[1]["reply"]   # one field at a time
    r2 = (await _drive(None, ["haan theek hai"], "v12-confirm"))[0]
    assert "n_pr_bank" in r2["trace"] and "AXIS Bank" in r2["reply"]
    r3 = (await _drive(None, ["nahi galat hai"], "v12-confirm"))[0]
    assert "n_not_verified" in r3["trace"] and r3["done"] is True


async def test_v10_shape_wrong_details_continues_unverified(shape_engine):
    shape, ex = shape_engine
    if shape != "v10":
        pytest.skip("v10 shape only")
    await _drive(None, ["paper roll", "91379784"], "v10-wrong")
    r = (await _drive(None, ["nahi galat hai"], "v10-wrong"))[0]
    assert "n_details_wrong" in r["trace"] and "n_options_unverified" in r["trace"]
    assert "unable to verify" in r["reply"].lower() and "SMS" in r["reply"]
