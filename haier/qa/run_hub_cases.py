"""Step-level answer harness for the Haier rejection bot.

One case = one workflow question + one caller reply. The reply goes through
the REAL voice brain (deterministic short answers, the real decision model,
the real workflow engine) with the call paused at that question, and the case
records where the flow went. Nothing is written to a database, no call is
placed; only the LLM provider is contacted.

Use it to answer "what does the bot do when a caller says X at step Y?" for
many X and Y, and to compare two configurations (before / after a change):

    env/bin/python -B haier/qa/run_hub_cases.py --snapshot storage/tmp/haier_qa/snapshot_local.raw \
        --output storage/tmp/haier_qa/hub_local.json
    # the code of another commit instead of the working tree:
    env/bin/python -B haier/qa/run_hub_cases.py --code-root storage/tmp/haier_qa/demo_code \
        --snapshot storage/tmp/haier_qa/snapshot_demo.raw --output storage/tmp/haier_qa/hub_demo.json
    # side by side:
    env/bin/python -B haier/qa/run_hub_cases.py --compare storage/tmp/haier_qa/hub_demo.json \
        storage/tmp/haier_qa/hub_local.json

Options: --cases FILE (default haier_hub_cases.json), --only ID[,ID], --repeat N, --concurrency K,
--decision-timeout S (judge routing with a relaxed decision budget; the default is the bot's own
budget, under which a slow decision falls back to the authored labels exactly as on a live call).

Case keys: ``id``, ``hub`` (node id of the question), ``say`` (the reply as
Sarvam STT returns it), optional ``reason`` (payload key, default
``unit_ok_no_defect``), ``slots`` (workflow slots set before the reply),
``category``, ``source_calls`` and ``expect``:

    "expect": {"to": ["m_n_end_redispatch_thanks"],      # the flow must go to one of these
               "not_to": ["m_n_v2_end_unverified"],      # and to none of these
               "outcome_in": ["redispatch"], "outcome_not_in": ["cancel_no_service"],
               "stay": true}                              # the question must still be pending

``to`` is the first node the flow reached after the question, condition nodes
skipped; ``(stay)`` when the question is still waiting, ``(no workflow turn)``
when the brain answered the turn without the workflow (the question is still
pending) and ``(call_control:<action>)`` for a platform route such as
``do_not_call`` or ``hangup``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

import haier.qa.run_text_cases as text_runner  # noqa: E402  (also puts --code-root first on sys.path)
from shared.orchestration.placeholders import resolve_placeholders  # noqa: E402

DEFAULT_REASON = "unit_ok_no_defect"
IDENTITY_YES = "हाँ जी।"


def _node_prompt(node: dict, values: dict) -> str:
    config = node.get("config") or {}
    text = (config.get("textByLanguage") or {}).get("hi") or config.get("text") or config.get("prompt") or ""
    return resolve_placeholders(text, values)


def landing(trace: list[str], hub: str, nodes: dict) -> str:
    walked = [n for n in trace if n != hub and (nodes.get(n) or {}).get("kind") != "condition"]
    return walked[0] if walked else "(stay)"


async def run_case(config, definition, payload: dict, case: dict, decision_timeout: float = 0.0) -> dict:
    nodes = {str(n["id"]): n for n in definition["nodes"]}
    hub = case["hub"]
    if hub not in nodes:
        return {"error": f"unknown hub {hub}", "to": None}
    brain, recorder, engine, _greeting, tasks = await text_runner.open_session(config, definition, payload)
    if decision_timeout > 0:
        # Judge the routing, not the provider's speed under a parallel test
        # run: a timed-out decision falls back to the authored labels.
        brain._goal_engine._timeout = float(decision_timeout)
    error = None
    to = outcome = None
    row: dict = {}
    try:
        await brain._handle_turn(IDENTITY_YES)
        brain._mark_reply_heard()
        workflow = brain._active_workflow
        if not workflow:
            raise RuntimeError("the workflow did not start after the identity answer")
        graph = await engine._get_definition_graph(definition)
        thread = {"configurable": {"thread_id": f"{recorder.session_id}:{workflow}"}}
        snapshot = await graph.aget_state(thread)
        values = dict(snapshot.values)
        values.update({
            "awaiting": hub, "current_node": hub, "node_retries": {},
            "slots": {**(values.get("slots") or {}), **(case.get("slots") or {})},
        })
        await graph.aupdate_state(thread, values, as_node="step")
        context_values = brain._runtime_context.prompt_values()
        peek = await engine.handle_turn_detailed(
            session_id=recorder.session_id, tenant_id=config.tenant_id, bot_id=config.bot_id,
            workflow_name=workflow, user_text="?", language="hi-IN", pause_for_context=True,
            pinned_versions=config.workflow_pins, context_values=context_values,
        )
        spoken = _node_prompt(nodes[hub], {k: v.value for k, v in brain._runtime_context.values.items()})
        # What the brain keeps after a turn that paused at this question.
        brain._pending_workflow_question = str(peek.get("nodeResumePrompt") or peek.get("nodePrompt") or "")
        brain._pending_ask_shape = None
        signals = peek.get("awaitingSignals")
        brain._pending_hub_signals = frozenset(signals) if isinstance(signals, list) else None
        brain._pending_hub_edges = peek.get("awaitingEdges")
        if hasattr(brain, "_pending_hub_options"):
            brain._pending_hub_options = peek.get("awaitingOptions") or None
        brain._pending_exact_ahead = bool(peek.get("awaitingExactAhead"))
        brain._last_bot_reply = spoken
        brain._history.append({"role": "user", "content": IDENTITY_YES})
        brain._history.append({"role": "assistant", "content": spoken})
        if hub not in brain._heard_nodes:
            brain._heard_nodes.append(hub)
        events_from = len(recorder.events)
        results_from = len(engine.qa_results)
        started = time.perf_counter()
        await brain._handle_turn(case["say"])
        brain._mark_reply_heard()
        events = recorder.events[events_from:]
        results = engine.qa_results[results_from:]
        trace = [n for r in results for n in (r.get("trace") or [])]
        control = next((e.get("action") for e in events if e["kind"] == "call_control"), None)
        to = (landing(trace, hub, nodes) if results
              else f"(call_control:{control})" if control else "(no workflow turn)")
        outcome = recorder.workflow_slots.get("outcome")
        decisions = [e for e in events if e["kind"] == "orchestration_decision"]
        deterministic = [e for e in events if e["kind"] == "decision_skipped_deterministic"]
        row = {
            "reply": brain._last_bot_reply,
            "trace": [n for n in trace if (nodes.get(n) or {}).get("kind") != "condition"],
            "decision": ({k: decisions[-1].get(k) for k in
                          ("intent", "signal", "option", "next_action", "needs_clarification", "reason", "latency_ms")}
                         if decisions else None),
            "deterministic": ({k: deterministic[-1].get(k) for k in ("resolver", "signal", "value")}
                              if deterministic else None),
            "fallback": next((e.get("reason") for e in events if e["kind"] == "orchestration_fallback"), None),
            "still_waiting": brain._active_workflow is not None and bool(results) and not results[-1].get("done"),
            "seconds": round(time.perf_counter() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001 — a crash is a finding
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await text_runner.close_session(brain, tasks)
    return {"to": to, "outcome": outcome, "error": error, **row}


def evaluate(case: dict, result: dict) -> list[str]:
    expect = case.get("expect") or {}
    fails: list[str] = []
    if result.get("error"):
        fails.append(f"crash: {result['error']}")
        return fails
    to, outcome = result.get("to"), result.get("outcome")
    if expect.get("to") and to not in expect["to"]:
        fails.append(f"went to {to}, expected one of {expect['to']}")
    if expect.get("not_to") and to in expect["not_to"]:
        fails.append(f"went to forbidden {to}")
    if expect.get("stay") and to not in ("(stay)", "(no workflow turn)"):
        # "(no workflow turn)": the brain answered the turn itself and the
        # question is still the pending one.
        fails.append(f"left the question (went to {to})")
    if expect.get("outcome_in") and outcome not in expect["outcome_in"]:
        fails.append(f"outcome={outcome!r} expected one of {expect['outcome_in']}")
    if expect.get("outcome_not_in") and outcome in expect["outcome_not_in"]:
        fails.append(f"forbidden outcome {outcome!r}")
    return fails


async def main(opts) -> int:
    snapshot = text_runner.load_snapshot(opts.snapshot)
    config, definition = text_runner.build_config(snapshot)
    text_runner.install_isolation(definition)
    spec = json.loads(opts.cases.read_text(encoding="utf-8"))
    payloads = json.loads(opts.payloads.read_text(encoding="utf-8"))["payloads"]
    only = {s.strip() for s in (opts.only or "").split(",") if s.strip()}
    jobs = [(case, rep) for case in spec["cases"] if not only or case["id"] in only
            for rep in range(1, (opts.repeat or int(case.get("repeat", 1))) + 1)]
    sem = asyncio.Semaphore(max(1, opts.concurrency))
    results = []

    async def run(job):
        case, rep = job
        async with sem:
            res = await run_case(config, definition, payloads[case.get("reason") or DEFAULT_REASON], case,
                                 decision_timeout=opts.decision_timeout)
            res.update({"id": case["id"], "repeat": rep, "hub": case["hub"], "say": case["say"],
                        "category": case.get("category"), "source_calls": case.get("source_calls")})
            res["failures"] = evaluate(case, res)
            res["pass"] = not res["failures"]
            d = res.get("decision") or {}
            print(f"{'PASS' if res['pass'] else 'FAIL'} {case['id']}#{rep} {case['hub']} -> {res.get('to')} "
                  f"[opt={d.get('option')} sig={d.get('signal') or (res.get('deterministic') or {}).get('signal')}"
                  f"{' fallback=' + str(res.get('fallback')) if res.get('fallback') else ''}"
                  f"{' ms=' + str(round(d.get('latency_ms'))) if d.get('latency_ms') else ''}] "
                  f"{'; '.join(res['failures'])[:200]}", flush=True)
            results.append(res)

    await asyncio.gather(*(run(j) for j in jobs))
    results.sort(key=lambda r: (r["id"], r["repeat"]))
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "env": snapshot.get("env"),
        "workflow_version": definition.get("version"), "workflow_md5": snapshot.get("workflow_md5"),
        "code_root": str(text_runner._CODE_ROOT), "results": results,
    }
    if opts.output:
        opts.output.parent.mkdir(parents=True, exist_ok=True)
        opts.output.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    passed = sum(1 for r in results if r["pass"])
    print(f"\n{passed}/{len(results)} step cases passed ({snapshot.get('env')}, wf v{definition.get('version')}, "
          f"code {text_runner._CODE_ROOT})")
    return 0 if passed == len(results) else 1


def compare(before_path: Path, after_path: Path) -> int:
    before = {(r["id"], r["repeat"]): r for r in json.loads(before_path.read_text(encoding="utf-8"))["results"]}
    after = {(r["id"], r["repeat"]): r for r in json.loads(after_path.read_text(encoding="utf-8"))["results"]}
    print("| Case | Step | Reply | Before | After |")
    print("|---|---|---|---|---|")
    for key in sorted(after):
        a, b = after[key], before.get(key, {})
        mark = lambda r: ("PASS " if r.get("pass") else "FAIL ") + str(r.get("to")) if r else "-"  # noqa: E731
        print(f"| {key[0]} | {a['hub']} | {a['say'][:60]} | {mark(b)} | {mark(a)} |")
    for name, rows in (("before", before), ("after", after)):
        print(f"{name}: {sum(1 for r in rows.values() if r.get('pass'))}/{len(rows)} passed")
    return 0


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--code-root", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--cases", type=Path, default=here / "haier_hub_cases.json")
    parser.add_argument("--payloads", type=Path, default=here / "haier_test_cases.json")
    parser.add_argument("--only")
    parser.add_argument("--repeat", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--decision-timeout", type=float, default=0.0,
                        help="seconds; 0 = the bot's own decision budget")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    options = parser.parse_args()
    if options.compare:
        raise SystemExit(compare(*options.compare))
    if not options.snapshot:
        parser.error("--snapshot is required")
    raise SystemExit(asyncio.run(main(options)))
