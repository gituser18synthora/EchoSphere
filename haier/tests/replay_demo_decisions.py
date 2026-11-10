"""Replay a private Haier DEMO export through the real local voice brain.

Only the LLM provider is contacted. Workflow storage/checkpoints and call
recording stay in memory; delivery is a text sink. No database is queried,
no calls are placed and no server configuration is changed. This verifies
text understanding and routing, not STT, TTS or real-time interruption timing.

Usage::

    env/bin/python -B haier/tests/replay_demo_decisions.py \
        --snapshot storage/tmp/private/demo_export.json \
        --candidate storage/tmp/private/candidate.json \
        --output storage/tmp/private/decision_replay.json

The export may include ``resolved_config`` (preferred) and ``intents``.
Candidate JSON may be a full export or have workflow, system_prompt and/or
goal_policy keys. Private call text is read from the export, never embedded
in this source. Generic regression scripts use fictional customer details.
``--engine-source`` loads an explicitly supplied trusted local Python source
in this isolated process before the voice brain imports the workflow engine;
it does not replace any project file or load a server configuration.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime, timezone
import json
import hashlib
import importlib
import importlib.util
import logging
from pathlib import Path
import re
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# A downloaded engine can be replayed exactly without touching shared source.
# Parse this option before importing runtime modules, and compile the source
# bytes directly so an existing bytecode cache cannot substitute another file.
_engine_parser = argparse.ArgumentParser(add_help=False)
_engine_parser.add_argument("--engine-source", type=Path)
_engine_options, _ = _engine_parser.parse_known_args()
if _engine_options.engine_source:
    _engine_path = _engine_options.engine_source.resolve(strict=True)
    _engine_name = "shared.orchestration.workflow_engine"
    if _engine_name in sys.modules:
        raise RuntimeError("The engine source must be selected before runtime imports")
    _engine_parent = importlib.import_module("shared.orchestration")
    _engine_spec = importlib.util.spec_from_file_location(_engine_name, _engine_path)
    if _engine_spec is None or _engine_spec.loader is None:
        raise ValueError("The selected workflow engine must be a Python source file")
    _engine_module = importlib.util.module_from_spec(_engine_spec)
    sys.modules[_engine_name] = _engine_module
    exec(compile(_engine_path.read_bytes(), str(_engine_path), "exec"),
         _engine_module.__dict__)
    setattr(_engine_parent, "workflow_engine", _engine_module)

from langgraph.checkpoint.memory import MemorySaver

from shared.bot_config import ResolvedBotConfig, select_greeting_variant
from shared.providers.base import ProviderConfig
from shared.providers.factory import get_llm_provider
from shared.runtime_context import ContextValue, RuntimeContext
import shared.orchestration.workflow_engine as workflow_module
import shared.orchestration.tool_executor as tool_module
from shared.post_call.structured import derive_structured_fields
from shared.post_call.analyzer import analyze_call, build_analysis_llm, extract_evidence_fields
from shared.post_call.structured import merge_structured_fields
from shared.post_call.nba import decide_next_best_action
from shared.orchestration.placeholders import resolve_placeholders
from voice_runtime.brain import ConversationBrain
from voice_runtime.recording import TurnRecord

BOT = "bot_450a4f7b4832"
TENANT = "tn_fb39b67f5361"


class MemoryRecorder:
    """The recorder interface without persistence or background workers."""

    def __init__(self):
        self.session_id = "haier-replay-" + uuid.uuid4().hex[:12]
        self.events = []
        self.turns = []
        self.usage = defaultdict(int)
        self.workflow_slots = {}
        self.language = "hi-IN"
        self.disposition = None
        self.call_state = {}
        self.runtime_context_record_id = None
        self.customer_context_id = None

    def add_event(self, kind, **data):
        self.events.append({"kind": kind, **data})

    async def flush_event(self, kind, **data):
        self.add_event(kind, **data)

    def flush_event_soon(self, kind, **data):
        self.add_event(kind, **data)

    def add_turn(self, turn):
        self.turns.append(turn)


class NoTools:
    async def execute(self, **kwargs):
        raise RuntimeError("The isolated replay forbids external tool execution")


@contextmanager
def isolated_workflow(definition):
    """Use the real interpreter with a frozen definition and memory saver."""
    if any(node.get("kind") == "api" for node in definition["nodes"]):
        raise ValueError("Export contains an API node; replay refuses network tools")
    saved_loader = workflow_module.load_workflow_definition
    saved_executor = tool_module.get_tool_executor
    saved_checkpoint = workflow_module.WorkflowEngine._get_checkpointer
    workflow_module.load_workflow_definition = lambda *args, **kwargs: deepcopy(definition)
    tool_module.get_tool_executor = lambda: NoTools()

    async def checkpoint(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    workflow_module.WorkflowEngine._get_checkpointer = checkpoint
    try:
        engine = workflow_module.WorkflowEngine()
        engine.replay_results = []
        original_handle = engine.handle_turn_detailed
        async def capture_result(**kwargs):
            result = await original_handle(**kwargs)
            engine.replay_results.append(deepcopy(result))
            return result
        engine.handle_turn_detailed = capture_result
        yield engine
    finally:
        workflow_module.load_workflow_definition = saved_loader
        tool_module.get_tool_executor = saved_executor
        workflow_module.WorkflowEngine._get_checkpointer = saved_checkpoint


def published_text(snapshot, kind):
    prompt = next(p for p in snapshot["prompts"] if p["type"] == kind)
    number = prompt.get("published_version") or prompt.get("active_version")
    version = next(v for v in snapshot["prompt_versions"]
                   if v["prompt_id"] == prompt["id"] and v["version"] == number)
    if kind == "greeting":
        return select_greeting_variant(version.get("variants"), "hi-IN") or ""
    return version.get("compiled_prompt") or version.get("full_prompt") or ""


def replay_inputs(snapshot, candidate):
    if snapshot["bot"]["id"] != BOT or snapshot["bot"]["tenant_id"] != TENANT:
        raise ValueError("This harness is restricted to the requested Haier DEMO bot")
    source = candidate if "workflows" in candidate else snapshot
    definition = deepcopy(candidate.get("workflow") or candidate.get("definition")
                          or source["workflows"][0])
    if definition.get("bot_id", BOT) != BOT:
        raise ValueError("Candidate belongs to a different bot")
    raw_resolved = snapshot.get("resolved_config") or {}
    if isinstance(raw_resolved, str):
        raw_resolved = json.loads(raw_resolved)
    names = {f.name for f in fields(ResolvedBotConfig)}
    settings = snapshot["voice_settings"]
    derived = not raw_resolved
    if raw_resolved:
        config = ResolvedBotConfig(**{k: v for k, v in raw_resolved.items() if k in names})
    else:
        model = settings.get("llm_model") or "gpt-6-luna"
        config = ResolvedBotConfig(
            tenant_id=TENANT, bot_id=BOT, bot_name=snapshot["bot"]["name"],
            use_case=snapshot["bot"].get("use_case") or "", published=True,
            version=snapshot["bot"].get("live_version") or snapshot["bot"]["version"],
            language="hi-IN", languages=["hi-IN", "en-IN"],
            system_prompt=published_text(snapshot, "system"),
            greeting=published_text(snapshot, "greeting"),
            intents=snapshot.get("intents") or [],
            goal_policy=deepcopy(settings.get("goal_policy") or {}),
            tts={"provider": settings["tts_provider"], "model": settings["tts_model"],
                 "voice": "ritu_hi_customer_warm", "voice_name": "Ritu", "voice_gender": "female"},
            llm={"provider": settings["llm_provider"], "model": model,
                 "api_key_reference": "env:OPENAI_API_KEY",
                 "settings": deepcopy(settings.get("llm_settings") or {}),
                 "orchestration": {"provider": settings["llm_provider"], "model": model,
                                   "api_key_reference": "env:OPENAI_API_KEY",
                                   "extra": {"reasoning_effort": "lowest", "service_tier": "fast"}}},
        )
    if "prompts" in candidate:
        config.system_prompt = published_text(candidate, "system")
    config.system_prompt = candidate.get("system_prompt", config.system_prompt)
    config.goal_policy = deepcopy(candidate.get("goal_policy", config.goal_policy))
    config.workflow_pins = {str(definition["id"]): int(definition["version"])}
    if not config.intents:
        raise ValueError("Export must include resolved_config or actual bot intents; refusing invented routes")
    if config.llm.get("provider") != "openai":
        raise ValueError("Only the exported DEMO OpenAI provider is allowed")
    return config, definition, derived


def generic_cases():
    return [
        {"name": "contact_yes_problem_pending", "turns": ["हाँ बोलिए।", "हाँ, technician का call आया था।", "अभी problem है, फ्रिज ठंडा नहीं कर रहा।", "हाँ, service चाहिए।", "ठीक है।"], "outcome": "redispatch"},
        {"name": "contact_no_service_yes", "turns": ["हाँ बोलिए।", "नहीं, किसी technician का call या visit नहीं हुआ।", "हाँ, service चाहिए।", "ठीक है।"], "outcome": "redispatch"},
        {"name": "contact_no_service_no", "turns": ["हाँ बोलिए।", "नहीं, कोई call या visit नहीं हुआ।", "नहीं, service नहीं चाहिए।"], "outcome": "cancel_no_service"},
        {"name": "mixed_no_but_call_received", "turns": ["हाँ बोलिए।", "नहीं, call तो आया था, visit नहीं हुई।", "अभी problem बाकी है।", "हाँ चाहिए।", "ठीक है।"], "outcome": "redispatch"},
        {"name": "visit_but_dissatisfied", "turns": ["हाँ बोलिए।", "Visit हुई थी लेकिन मैं service से संतुष्ट नहीं हूँ।", "अभी भी problem है।", "हाँ, service चाहिए।", "ठीक है।"], "outcome": "redispatch"},
        {"name": "resolved_by_haier", "turns": ["हाँ बोलिए।", "हाँ, technician आए थे।", "Haier के technician ने ठीक कर दिया, अब कोई problem नहीं है।", "Haier के technician ने ठीक किया था।"], "outcome": "redispatch_completion"},
        {"name": "resolved_without_haier", "turns": ["हाँ बोलिए।", "हाँ, technician का call आया था।", "अब अपने आप ठीक हो गया, कोई problem नहीं है।", "अपने आप ठीक हुआ।"], "outcome": "cancel_resolved"},
        {"name": "uncertain_contact", "turns": ["हाँ बोलिए।", "पता नहीं, शायद घर पर किसी ने बात की होगी।"], "expect_open": True, "no_false_no_contact": True},
        {"name": "uncertain_resolution", "turns": ["हाँ बोलिए।", "हाँ, call आया था।", "अभी check नहीं किया, पता नहीं ठीक हुआ है या नहीं।"], "expect_open": True},
        {"name": "completed_installation_by_haier", "context": {"rejection_reason": "customer_refused_visit", "l3_tagging": "Installation"}, "turns": ["हाँ बोलिए।", "हाँ, technician आए थे।", "Installation हो गया है, कोई काम बाकी नहीं है।", "हाँ, सब पूरा हो गया, कोई issue pending नहीं है।", "Haier के technician ने किया था।"], "outcome": "redispatch_completion"},
        {"name": "completed_installation_by_other", "context": {"rejection_reason": "customer_refused_visit", "l3_tagging": "Installation"}, "turns": ["हाँ बोलिए।", "हाँ, technician का call आया था।", "Installation हो गया है।", "हाँ, सब पूरा हो गया, कोई issue pending नहीं है।", "Local technician से करवाया था।"], "outcome": "close_completed_other"},
        {"name": "completed_installation_by_unknown", "context": {"rejection_reason": "customer_refused_visit", "l3_tagging": "Installation"}, "turns": ["हाँ बोलिए।", "हाँ, technician का call आया था।", "Installation हो गया है।", "हाँ, सब पूरा हो गया, कोई issue pending नहीं है।", "पता नहीं किसने किया, मैं घर पर नहीं था।"], "outcome": "close_completed"},
        {"name": "installation_pending", "context": {"rejection_reason": "customer_refused_visit", "l3_tagging": "Installation"}, "turns": ["हाँ बोलिए।", "हाँ, call आया था।", "नहीं, installation अभी नहीं हुआ है।", "हाँ, installation करवाना है।", "ठीक है।"], "outcome": "redispatch"},
        {"name": "late_future_service", "turns": ["हाँ बोलिए।", "हाँ, call आया था।", "Service अभी नहीं चाहिए, 20 दिन बाद चाहिए।", "हाँ, बाद में चाहिए।"], "outcome": "cancel_service_later", "forbidden_nodes": {2: ["n_v2_future_when"]}},
        {"name": "repeated_negation_problem_pending", "turns": ["हाँ बोलिए।", "हाँ, technician आए थे।", "नहीं नहीं, ठीक नहीं हुआ, अभी भी problem है।", "हाँ, service चाहिए।", "ठीक है।"], "outcome": "redispatch"},
    ]


def turn_dict(turn):
    if is_dataclass(turn):
        return asdict(turn)
    if hasattr(turn, "model_dump"):
        return turn.model_dump(mode="json")
    return dict(vars(turn))


async def validate_summary(config, policy, turns, events, slots, case, closed):
    """The actual analyst/evidence/merge stages, without processor persistence."""
    reference = datetime.fromisoformat(str(case.get("started_at") or datetime.now(timezone.utc).isoformat()))
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    final_state = {"end_reason": case.get("end_reason") or "completed",
                   "disposition": None, "language": "hi-IN", "escalated": False,
                   "workflow_stage": "conversation" if closed else "workflow",
                   "duration_sec": 60, "call_state": {}}
    llm = build_analysis_llm(config)
    analysis, usage = await analyze_call(llm, policy=policy, turns=turns, events=events,
                                         final_state=final_state, reference=reference)
    if analysis is None:
        return {"error": "analysis_unavailable"}
    proposed, evidence, evidence_usage = await extract_evidence_fields(
        llm, policy=policy, turns=turns, reference=reference)
    if proposed is None:
        return {"error": "evidence_extraction_unavailable"}
    analysis.structured_fields = {**analysis.structured_fields, **proposed}
    analysis.structured_field_evidence = evidence or {}
    analysis.next_best_action = decide_next_best_action(
        analysis, policy=policy, disposition=None, call_state={},
        escalated=False, workflow_active=not closed)
    if analysis.next_best_action.action in ("do_not_contact", "close_goal_completed", "no_action"):
        analysis.follow_up_required = False
    analysis.structured_fields, analysis.structured_field_sources = merge_structured_fields(
        policy, slots, analysis.structured_fields, evidence=evidence, turns=turns, reference=reference)
    return {"analysis": analysis.model_dump(mode="json"), "usage": usage,
            "evidence_usage": evidence_usage, "final_state": final_state}


async def run_case(config, definition, case, *, summary_llm=False):
    recorder = MemoryRecorder()
    context = {"customer_name": "Test Customer", "product_name": "Refrigerator",
               "l3_tagging": "Customer Service", "rejection_reason": "unit_ok_no_defect",
               "rejection_reason_label": "technician found no defect", **case.get("context", {})}
    runtime = RuntimeContext(tenant_id=TENANT, bot_id=BOT, source_mode="dialer",
                             values={k: ContextValue(k, v, "session") for k, v in context.items()})
    llm_conf = config.llm
    llm = get_llm_provider(ProviderConfig(
        provider=llm_conf["provider"], model=llm_conf["model"],
        api_key_reference=llm_conf.get("api_key_reference") or "env:OPENAI_API_KEY",
        timeout_seconds=30, extra=llm_conf.get("extra") or {"reasoning_effort": "lowest"},
    ))
    rows, failures = [], []
    with isolated_workflow(definition) as engine:
        brain = ConversationBrain(config=deepcopy(config), llm=llm, recorder=recorder,
                                  workflow_engine=engine, runtime_context=runtime)
        brain._tools = NoTools()
        async def push(frame, direction=None):
            return None
        async def notify(payload):
            return None
        brain.push_frame = push
        brain._notify_client = notify
        tasks = []
        def create_task(coro, name=None):
            task = asyncio.create_task(coro, name=name)
            tasks.append(task)
            return task
        async def cancel_task(task, timeout=None):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        brain.create_task = create_task
        brain.cancel_task = cancel_task
        greeting = case.get("greeting") or config.greeting
        greeting = resolve_placeholders(greeting, {**context, "voice_speaker_name": "Ritu"})
        brain._history = [{"role": "assistant", "content": greeting}]
        brain._last_bot_reply = greeting
        recorder.add_turn(TurnRecord(role="bot", text=greeting, timestamp=0))
        try:
            for index, text in enumerate(case["turns"]):
                if brain._closing:
                    break
                offset = len(recorder.events)
                workflow_offset = len(engine.replay_results)
                await brain._handle_turn(text)
                brain._mark_reply_heard()  # All text was delivered; no synthetic barge-in.
                events = recorder.events[offset:]
                decisions = [e for e in events if e["kind"] == "orchestration_decision"]
                row = {"text": text, "reply": brain._last_bot_reply,
                       "active_workflow": brain._active_workflow,
                       "pending_question": brain._pending_workflow_question,
                       "slots": deepcopy(recorder.workflow_slots),
                       "workflow_results": deepcopy(engine.replay_results[workflow_offset:]),
                       "decisions": decisions, "events": events, "closed": brain._closing}
                rows.append(row)
                forbidden = case.get("forbidden_nodes", {}).get(index) or case.get("forbidden_nodes", {}).get(str(index)) or []
                visited = {node for result in row["workflow_results"] for node in result.get("trace", [])}
                if visited.intersection(forbidden):
                    failures.append(f"Turn {index + 1} re-entered forbidden nodes {sorted(visited.intersection(forbidden))}")
                print(f"  {index + 1}: {text} -> {row['reply']} [{row['slots'].get('outcome') or brain._active_workflow}]", flush=True)
            summary = derive_structured_fields(brain._goal_policy, recorder.workflow_slots)
            if case.get("outcome") and recorder.workflow_slots.get("outcome") != case["outcome"]:
                failures.append(f"outcome {recorder.workflow_slots.get('outcome')!r}, expected {case['outcome']!r}")
            if case.get("expect_open") and brain._closing:
                failures.append("Uncertain reply unexpectedly completed the workflow")
            if case.get("no_false_no_contact") and any("कोई भी call या visit नहीं हुई" in row["reply"] for row in rows[1:]):
                failures.append("Unknown contact was asserted as no contact")
            if case.get("confirmed_contact") and len(rows) > 1 and "कोई भी call या visit नहीं हुई" in rows[1]["reply"]:
                failures.append("Affirmed technician contact was incorrectly asserted as no contact")
            if case.get("confirmed_contact") and len(rows) > 1 and rows[1]["slots"].get("haier_technician_contact_route") != "contact_confirmed":
                failures.append("Affirmed technician contact was not captured before the next workflow question")
            result = {"name": case["name"], "rows": rows, "failures": failures,
                      "expected_outcome": case.get("outcome"),
                      "expected_open": case.get("expect_open"),
                      "workflow_slots": deepcopy(recorder.workflow_slots),
                      "structured_from_workflow": summary,
                      "turns": [turn_dict(t) for t in recorder.turns],
                      "usage": dict(recorder.usage), "closed": brain._closing}
            if summary_llm:
                result["summary_validation"] = await validate_summary(
                    config, brain._goal_policy, result["turns"], recorder.events,
                    recorder.workflow_slots, case, brain._closing)
                checked = result["summary_validation"]
                if checked.get("error"):
                    failures.append(checked["error"])
                elif brain._closing and recorder.workflow_slots.get("outcome") and checked["analysis"]["structured_fields"].get("call_completion") != "flow_completed":
                    failures.append("Completed/acknowledged workflow summary did not report flow_completed")
        finally:
            brain._disarm_silence_timer()
            brain._discard_speculation("isolated_replay_done")
            brain._discard_extraction_ahead()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
    return result


async def main(args):
    snapshot_bytes = args.snapshot.read_bytes()
    snapshot = json.loads(snapshot_bytes)
    candidate_bytes = args.candidate.read_bytes() if args.candidate else b""
    candidate = json.loads(candidate_bytes) if candidate_bytes else {}
    config, definition, derived = replay_inputs(snapshot, candidate)
    cases = []
    for call in snapshot.get("calls", []):
        turns = call.get("turns") or []
        greeting = next((t.get("text") for t in turns if t.get("role") != "user"), config.greeting)
        name_match = re.search(r"मेरी बात\s+(.+?)\s+जी", greeting)
        cases.append({"name": call["control_plane_id"],
                      "greeting": greeting, "confirmed_contact": True,
                      "end_reason": call.get("end_reason"), "started_at": call.get("started_at"),
                      "turns": [t["text"] for t in turns if t.get("role") == "user"],
                      "context": {"customer_name": name_match.group(1)} if name_match else {}})
    if not args.calls_only:
        cases += generic_cases()
    if args.cases:
        extra = json.loads(args.cases.read_text())
        cases += extra if isinstance(extra, list) else extra["cases"]
    results = []
    for iteration in range(args.repeat):
        for case in cases:
            if args.case and args.case not in case["name"]:
                continue
            print(f"CASE {case['name']} repeat {iteration + 1}", flush=True)
            result = await run_case(config, definition, case, summary_llm=args.summary_llm)
            result["repeat"] = iteration + 1
            results.append(result)
            print(f"{'FAIL' if result['failures'] else 'PASS'} {case['name']}: {result['failures']}", flush=True)
    output = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
              "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest() if candidate_bytes else None,
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "workflow_engine_source": str(Path(workflow_module.__file__).resolve()),
              "workflow_engine_sha256": hashlib.sha256(Path(workflow_module.__file__).read_bytes()).hexdigest(),
              "workflow_version": definition["version"], "derived_config": derived,
              "limitations": ["Text replay; audio/STT/TTS and real-time interruption behavior are not simulated.",
                              "Original caller utterances are replayed verbatim; a corrected follow-up may change their conversational meaning.",
                              "Structured summary here is workflow-derived; analyst/evidence extraction requires separate validation."],
              "results": results}
    if args.output:
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str) + "\n")
        args.output.chmod(0o600)
    return 1 if any(r["failures"] for r in results) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--engine-source", type=Path, help="Trusted local engine source to load only in this isolated replay process")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", default="")
    parser.add_argument("--cases", type=Path, help="Additional local scenario JSON list with name, turns, context and optional outcome")
    parser.add_argument("--calls-only", action="store_true")
    parser.add_argument("--summary-llm", action="store_true", help="Also run the actual post-call analyst/evidence extraction without persistence")
    parser.add_argument("--repeat", type=int, default=1)
    options = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(asyncio.run(main(options)))
