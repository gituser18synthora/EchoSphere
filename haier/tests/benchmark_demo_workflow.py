"""Compare frozen Haier definitions with the real local WorkflowEngine.

This is a timing harness, not a decision-model replay. It uses MemorySaver,
an in-memory definition-cache hit and no LLM, databases, tools or call audio.
Each variant runs in a fresh process; the order rotates between rounds.
Reported workflow times exclude imports/process startup. JSON decode and graph
build are measured separately. No server configuration is read or changed.

Example::

    env/bin/python -B haier/tests/benchmark_demo_workflow.py \
      --variant original=storage/tmp/private/demo_export.json \
      --variant candidate=storage/tmp/private/candidate.json \
      --rounds 9 --warm 10 --output storage/tmp/private/timings.json
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

BOT = "bot_450a4f7b4832"
TENANT = "tn_fb39b67f5361"
CONTACTS = [
    ("normal", "हाँ, technician का call आया था।", "affirm"),
    ("dissatisfied", "हुई तो थी बट मैं अभी सेटिस्फाई नहीं हूँ।", None),
    ("corrected_no", "नहीं, कॉल आया था।", None),
    ("uncertain", "मुझे याद नहीं, शायद किसी ने बात की होगी।", None),
    ("no_contact", "नहीं।", "refusal"),
]
CONTEXT = {"customer_name": "Test Customer", "product_name": "Refrigerator",
           "l3_tagging": "Customer Service", "rejection_reason": "unit_ok_no_defect",
           "rejection_reason_label": "technician found no defect"}


def definition_from(document):
    definition = document.get("workflow") or document.get("definition")
    if definition is None and "workflows" in document:
        definition = document["workflows"][0]
    if definition is None and "nodes" in document:
        definition = document
    if not definition or definition.get("id") != "wf_1c3e59362c51":
        raise ValueError("Only the requested frozen Haier workflow is allowed")
    if definition.get("bot_id", BOT) != BOT:
        raise ValueError("Unexpected bot")
    if any(n.get("kind") == "api" for n in definition["nodes"]):
        raise ValueError("External API nodes are forbidden in this benchmark")
    return definition


@contextmanager
def isolated_engine(definition, engine_source=None):
    from langgraph.checkpoint.memory import MemorySaver
    if engine_source:
        # A read-only DEMO source snapshot can differ from this workspace.
        # Execute it only inside this disposable worker; never replace files.
        import importlib
        import importlib.util
        package = importlib.import_module("shared.orchestration")
        name = "shared.orchestration.workflow_engine"
        spec = importlib.util.spec_from_file_location(name, engine_source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        setattr(package, "workflow_engine", module)
        spec.loader.exec_module(module)
    else:
        import shared.orchestration.workflow_engine as module
    import shared.orchestration.tool_executor as tool_module

    original_loader = module.load_workflow_definition
    original_checkpoint = module.WorkflowEngine._get_checkpointer
    original_tools = tool_module.get_tool_executor

    class NoTools:
        async def execute(self, **kwargs):
            raise RuntimeError("External tools are forbidden")

    # Real cached-definition reads return the dictionary without deepcopy.
    module.load_workflow_definition = lambda *args, **kwargs: definition
    tool_module.get_tool_executor = lambda: NoTools()

    async def checkpointer(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    module.WorkflowEngine._get_checkpointer = checkpointer
    try:
        yield module.WorkflowEngine()
    finally:
        module.load_workflow_definition = original_loader
        module.WorkflowEngine._get_checkpointer = original_checkpoint
        tool_module.get_tool_executor = original_tools


async def run_worker(args):
    raw = args.worker.read_bytes()
    # Decode the full export only to select the definition. Timed decoding uses
    # that definition alone, so private recording/event size is not a confounder.
    definition = definition_from(json.loads(raw))
    serialized = json.dumps(definition, ensure_ascii=False, separators=(",", ":")).encode()
    decode_samples = []
    decode_cpu_samples = []
    for _ in range(7):
        started = time.perf_counter_ns()
        cpu_started = time.process_time_ns()
        json.loads(serialized)
        decode_samples.append((time.perf_counter_ns() - started) / 1e6)
        decode_cpu_samples.append((time.process_time_ns() - cpu_started) / 1e6)
    metrics = {"workflow_json_bytes": len(serialized),
               "input_sha256": hashlib.sha256(raw).hexdigest(),
               "workflow_sha256": hashlib.sha256(serialized).hexdigest(),
               "version": definition["version"],
               "json_decode_ms": statistics.median(decode_samples),
               "json_decode_cpu_ms": statistics.median(decode_cpu_samples)}
    metrics["engine_source_sha256"] = (
        hashlib.sha256(args.engine_source.read_bytes()).hexdigest()
        if args.engine_source else None)
    with isolated_engine(definition, args.engine_source) as engine:
        started = time.perf_counter_ns()
        cpu_started = time.process_time_ns()
        await engine._get_definition_graph(definition)
        metrics["cold_graph_build_ms"] = (time.perf_counter_ns() - started) / 1e6
        metrics["cold_graph_build_cpu_ms"] = (time.process_time_ns() - cpu_started) / 1e6

        async def turn(session, text, signal=None):
            return await engine.handle_turn_detailed(
                session_id=session, tenant_id=TENANT, bot_id=BOT,
                workflow_name=definition["id"], user_text=text, signal=signal,
                language="hi-IN", context_values=CONTEXT, timeout_seconds=30)

        # Identity establishes the actual pending contact node before timing it.
        started = time.perf_counter_ns()
        cpu_started = time.process_time_ns()
        initial = await turn("cold", "हाँ बोलिए।", "affirm")
        metrics["cold_identity_turn_ms"] = (time.perf_counter_ns() - started) / 1e6
        metrics["cold_identity_turn_cpu_ms"] = (time.process_time_ns() - cpu_started) / 1e6
        if initial["trace"][-1] != "n_intro_unit_ok":
            raise AssertionError("The benchmark did not reach the contact question")
        name, text, signal = CONTACTS[args.cold_index % len(CONTACTS)]
        started = time.perf_counter_ns()
        cpu_started = time.process_time_ns()
        result = await turn("cold", text, signal)
        metrics["cold_contact_ms"] = (time.perf_counter_ns() - started) / 1e6
        metrics["cold_contact_cpu_ms"] = (time.process_time_ns() - cpu_started) / 1e6
        metrics["cold_contact_case"] = name
        metrics["cold_contact_destination"] = result["trace"][-1]

        contact_rows = []
        problem_rows = []
        # Prime every contact answer path before measuring cache-warm timing.
        for index in range(len(CONTACTS) + args.warm):
            case_name, text, signal = CONTACTS[index % len(CONTACTS)]
            session = f"contact-{index}"
            await turn(session, "हाँ बोलिए।", "affirm")
            started = time.perf_counter_ns()
            cpu_started = time.process_time_ns()
            result = await turn(session, text, signal)
            elapsed = (time.perf_counter_ns() - started) / 1e6
            cpu_elapsed = (time.process_time_ns() - cpu_started) / 1e6
            if index >= len(CONTACTS):
                contact_rows.append({"case": case_name, "ms": elapsed,
                                     "cpu_ms": cpu_elapsed,
                                     "destination": result["trace"][-1]})
            # An explicit normal contact keeps both variants on the same node.
            if case_name == "normal":
                if result["trace"][-1] != "n_reason_unit_ok_q":
                    raise AssertionError("Normal contact did not reach product status")
                started = time.perf_counter_ns()
                cpu_started = time.process_time_ns()
                result = await turn(session, "नहीं नहीं, अभी प्रॉब्लम है, सही से काम नहीं कर रहा।")
                elapsed = (time.perf_counter_ns() - started) / 1e6
                cpu_elapsed = (time.process_time_ns() - cpu_started) / 1e6
                if index >= len(CONTACTS):
                    problem_rows.append({"ms": elapsed, "cpu_ms": cpu_elapsed,
                                         "destination": result["trace"][-1]})
        metrics["warm_contact"] = contact_rows
        metrics["warm_problem_status"] = problem_rows
    return metrics


def describe(values):
    ordered = sorted(values)
    # Nearest-rank p95: retain max alongside it because cold samples are few.
    return {"n": len(values), "median_ms": statistics.median(values),
            "p95_ms": ordered[max(0, (95 * len(values) + 99) // 100 - 1)],
            "max_ms": max(values), "min_ms": min(values)}


def orchestrate(args):
    variants = {}
    for item in args.variant:
        name, path = item.split("=", 1)
        variants[name] = Path(path).resolve()
    if not variants:
        raise ValueError("At least one --variant NAME=PATH is required")
    runs = {name: [] for name in variants}
    names = list(variants)
    order_log = []
    for round_index in range(args.rounds):
        order = names[round_index % len(names):] + names[:round_index % len(names)]
        for name in order:
            command = [sys.executable, "-B", str(Path(__file__).resolve()),
                       "--worker", str(variants[name]), "--warm", str(args.warm),
                       "--cold-index", str(round_index)]
            if args.engine_source:
                command += ["--engine-source", str(args.engine_source.resolve())]
            completed = subprocess.run(command, check=True, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            result = json.loads(completed.stdout)
            runs[name].append(result)
            order_log.append({"round": round_index, "variant": name})
            print(f"round={round_index + 1} variant={name} contact={result['cold_contact_case']} "
                  f"cold_ms={result['cold_contact_ms']:.2f}", file=sys.stderr, flush=True)
    report = {"generated_utc": datetime.now(timezone.utc).isoformat(),
              "engine_source": str(args.engine_source) if args.engine_source else "workspace",
              "limitations": ["Local microbenchmark; no DB, LLM, STT, TTS, network or real call latency.",
                              "Workflow cache hit, real interpreter and MemorySaver; production checkpoint I/O excluded.",
                              "Imports/process startup excluded; fresh process for each variant in rotated order.",
                              "Different corrected destinations are recorded; performance equality is not assumed.",
                              "Cold p95 from a small sample is descriptive, not a service-level guarantee."],
              "execution_order": order_log, "summary": {}, "runs": runs}
    for name, rows in runs.items():
        summary = {key: describe([row[key] for row in rows]) for key in (
            "json_decode_ms", "json_decode_cpu_ms", "cold_graph_build_ms",
            "cold_graph_build_cpu_ms", "cold_identity_turn_ms", "cold_identity_turn_cpu_ms",
            "cold_contact_ms", "cold_contact_cpu_ms")}
        summary["workflow_json_bytes"] = rows[0]["workflow_json_bytes"]
        summary["input_sha256"] = rows[0]["input_sha256"]
        for group in ("warm_contact", "warm_problem_status"):
            summary[group + "_ms"] = describe([r["ms"] for row in rows for r in row[group]])
            summary[group + "_cpu_ms"] = describe([r["cpu_ms"] for row in rows for r in row[group]])
        summary["warm_contact_by_case"] = {case: describe([
            r["ms"] for row in rows for r in row["warm_contact"] if r["case"] == case
        ]) for case, _, _ in CONTACTS}
        report["summary"][name] = summary
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", action="append", default=[])
    parser.add_argument("--rounds", type=int, default=9)
    parser.add_argument("--warm", type=int, default=10)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--engine-source", type=Path,
                        help="Execute a frozen engine source only in local worker memory")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cold-index", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        logging.disable(logging.CRITICAL)
        print(json.dumps(asyncio.run(run_worker(args)), ensure_ascii=False))
    else:
        if args.rounds < 1 or args.warm < 5 or args.output is None:
            parser.error("Need --rounds >= 1, --warm >= 5 and --output")
        orchestrate(args)


if __name__ == "__main__":
    main()
