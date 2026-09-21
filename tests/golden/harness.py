"""Golden replay harness: run recorded caller turns through the REAL
workflow engine and router with every external dependency stubbed.

A case is ``{"bot": <bot id>, "name": ..., "turns": [{"text", "signal"?,
"mocks"?, "options"?}], "expected": [...]}``. Definitions, intents,
connections and context samples come from ``fixtures/definitions/<bot>.json``
(read-only dumps of the control plane) so the corpus is frozen: a later edit
to a tenant's live workflow never silently changes what these tests assert.

What is captured per turn (the engine/router CONTRACT):
  - deterministic signal classification of the utterance (router)
  - question shape / hang-up / leading affirmation (router helpers)
  - TurnRouter.decide kind + reason + intent (router, bot intents)
  - engine: node trace, reply, done, status, offScript, slots
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.tool_executor as te
import shared.orchestration.workflow_engine as we
from shared.orchestration.router import (
    RouteKind,
    TurnRouter,
    classify_user_signal,
    detect_hangup,
    leading_affirmation,
    looks_like_question,
)

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "definitions"
CASES = HERE / "cases"


def load_fixture(bot_id: str) -> dict:
    return json.loads((FIXTURES / f"{bot_id}.json").read_text(encoding="utf-8"))


def primary_workflow(fixture: dict) -> dict:
    """Highest-version non-trivial workflow of the bot."""
    workflows = [w for w in fixture["workflows"] if (w.get("nodes") or [])]
    workflows.sort(key=lambda w: (-len(w.get("nodes") or []), -int(w.get("version") or 0)))
    return workflows[0]


def context_values(fixture: dict) -> dict:
    for schema in fixture.get("context_schemas") or []:
        payload = schema.get("test_payload")
        if isinstance(payload, dict) and payload:
            return {k: v for k, v in payload.items() if not isinstance(v, (dict, list))}
    return {}


class _FakeResult:
    def __init__(self, tool: str, mapped: dict):
        self.tool = tool
        self.ok = True
        self.status = "ok"
        self.mocked = True
        self.mapped = mapped
        self.data = None
        self.error = None
        self.latency_ms = 0
        self.trace = {}

    def as_event(self) -> dict:
        return {"tool": self.tool, "ok": True, "status": "ok", "mocked": True}


class FakeExecutor:
    """Deterministic api-node executor: success, mapped from the connection's
    documented response example (no network, no DB)."""

    def __init__(self, connections: list[dict]):
        self._by_name = {c["name"]: c for c in connections}
        self.calls: list[dict] = []

    async def execute(self, **kwargs) -> _FakeResult:
        self.calls.append(kwargs)
        tool = kwargs.get("tool") or ""
        conn = self._by_name.get(tool) or {}
        example = ((conn.get("response_schema") or {}).get("example")
                   if isinstance(conn.get("response_schema"), dict) else None)
        mapped: dict = {}
        if isinstance(example, dict):
            mapping = conn.get("response_mapping")
            if isinstance(mapping, (list, dict)) and mapping:
                try:
                    mapped = te.ToolExecutor._apply_mapping(
                        {"response_mapping": mapping}, example)
                except Exception:  # noqa: BLE001 — fall back to flat scalars
                    mapped = {}
            if not mapped:
                mapped = {k: v for k, v in example.items() if not isinstance(v, (dict, list))}
        return _FakeResult(tool, mapped)


@contextlib.contextmanager
def patched(definition: dict, connections: list[dict]):
    """Engine wired to one frozen definition + fake executor + memory saver."""
    saved_loader = we.load_workflow_definition
    saved_exec = te.get_tool_executor
    saved_get_cp = we.WorkflowEngine._get_checkpointer
    executor = FakeExecutor(connections)
    we.load_workflow_definition = lambda tenant_id, bot_id, name: definition
    te.get_tool_executor = lambda: executor

    async def _mem(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    we.WorkflowEngine._get_checkpointer = _mem
    try:
        yield we.WorkflowEngine(), executor
    finally:
        we.load_workflow_definition = saved_loader
        te.get_tool_executor = saved_exec
        we.WorkflowEngine._get_checkpointer = saved_get_cp


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def build_router(fixture: dict, workflow: dict) -> TurnRouter:
    intents = []
    for intent in fixture.get("intents") or []:
        intents.append({
            "name": intent.get("name"), "samples": intent.get("samples") or [],
            "route": intent.get("route"), "workflow_id": intent.get("workflow_id"),
            "confidence_threshold": intent.get("confidence_threshold"),
        })
    return TurnRouter(intents=intents, has_knowledge_bases=False,
                      workflows={workflow["id"]: workflow["name"]})


async def replay_case(case: dict, fixture: dict | None = None) -> list[dict]:
    fixture = fixture or load_fixture(case["bot"])
    definition = case.get("definition") or primary_workflow(fixture)
    ctx = case.get("context") if case.get("context") is not None else context_values(fixture)
    router = build_router(fixture, definition)
    default_language = case.get("language") or "hi-IN"
    observed: list[dict] = []
    with patched(definition, fixture.get("connections") or []) as (engine, _executor):
        active = None
        for index, turn in enumerate(case["turns"]):
            text = turn["text"]
            options = turn.get("options") or {}
            language = options.get("language") or default_language
            signal = turn["signal"] if "signal" in turn else classify_user_signal(text)
            decision = router.decide(text, active_workflow=active)
            mocks = turn.get("mocks") or None
            result = await engine.handle_turn_detailed(
                session_id=f"golden-{case['bot']}-{abs(hash(case['name']))}",
                tenant_id=fixture["bot"]["tenant_id"], bot_id=case["bot"],
                workflow_name=definition["name"], user_text=text,
                language=language, signal=signal, context_values=ctx,
                reset_state=(index == 0),
                mock_tool_results=({k: (v or {}) for k, v in mocks.items()} if mocks else None),
            )
            active = None if result.get("done") else definition["name"]
            observed.append({
                "text": text,
                "signal": signal,
                "question_shape": looks_like_question(text),
                "hangup": detect_hangup(text),
                "leading_affirm": leading_affirmation(text),
                "route": {"kind": decision.kind.value if isinstance(decision.kind, RouteKind) else str(decision.kind),
                          "reason": decision.reason, "intent": decision.intent,
                          "action": decision.action},
                "trace": list(result.get("trace") or []),
                "reply": result.get("reply") or "",
                "done": bool(result.get("done")),
                "status": result.get("status"),
                "offScript": bool(result.get("offScript")),
                "slots": _jsonable({k: v for k, v in sorted((result.get("slots") or {}).items())}),
            })
    return observed


def replay_case_sync(case: dict, fixture: dict | None = None) -> list[dict]:
    return asyncio.run(replay_case(case, fixture))


def iter_case_files() -> list[pathlib.Path]:
    return sorted(CASES.glob("*.json"))


def diff_turns(expected: list[dict], observed: list[dict]) -> list[str]:
    lines: list[str] = []
    for i, (exp, obs) in enumerate(zip(expected, observed)):
        for key in exp:
            if exp.get(key) != obs.get(key):
                lines.append(f"turn {i} ({exp.get('text', '')[:50]!r}) {key}: "
                             f"expected {json.dumps(exp.get(key), ensure_ascii=False)[:300]} "
                             f"got {json.dumps(obs.get(key), ensure_ascii=False)[:300]}")
    if len(expected) != len(observed):
        lines.append(f"turn count expected {len(expected)} got {len(observed)}")
    return lines
