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

SNAPSHOT = pathlib.Path(__file__).resolve().parent.parent / "data" / "workflow_v10_snapshot.json"
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


# ── the transform ───────────────────────────────────────────────────────────


def test_transform_is_idempotent_and_reports_changes(snapshot):
    first, changes = up.upgrade_workflow(snapshot)
    assert len(changes) == 4
    again, changes2 = up.upgrade_workflow(first)
    assert again is first and "already upgraded" in changes2[0]


def test_prefill_chain_removed_and_rewired(upgraded):
    ids = {n["id"] for n in upgraded["nodes"]}
    assert not ids & set(up.PREFILL_NODES)
    assert {"n_ctx_customer_name", "n_ctx_docket"} <= ids       # context-fed fields stay
    edges = {(e["from"], e["to"]) for e in upgraded["edges"]}
    assert ("n_start", "n_ctx_customer_name") in edges
    assert ("n_ask_tid", "n_ask_merchant") not in edges
    assert ("n_bat_tid", "n_bat_details") not in edges


def test_readbacks_use_api_mapped_slot_names(upgraded):
    by_id = {n["id"]: n for n in upgraded["nodes"]}
    for node_id in ("n_ask_merchant", "n_bat_details"):
        fields = by_id[node_id]["config"]["readback"]["en"]["fields"]
        variables = [f["variable"] for f in fields]
        assert "outlet_name" in variables and "bank_name" in variables
        assert not any(v.startswith("ctx_") for v in variables)
    raw = json.dumps(upgraded["nodes"], ensure_ascii=False)
    for old in up.SLOT_RENAMES:
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
