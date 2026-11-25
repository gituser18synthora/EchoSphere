"""Text-level conversation harness for the Haier rejection bot.

Plays the scripted customer turns of ``haier_test_cases.json`` through the REAL
voice brain (``ConversationBrain._handle_turn``) with the REAL decision/reply
LLM and the EXACT exported workflow + prompt of one environment. Workflow
checkpoints use an in-memory saver, external tools are forbidden, nothing is
written to any database, no call is placed. Only the LLM provider is contacted.

What it verifies: understanding, routing, identity/consent rules, workflow
branches, spoken wording and the recorded ``outcome`` slot.
What it does NOT verify: STT, TTS, barge-in/endpointing timing, silence
prompts, hang-ups. Use ``run_voice_cases.py`` for those.

Usage (from the repo root):

    # 1) export the environment's effective config (read-only)
    env/bin/python -B haier/qa/export_config.py local > storage/tmp/haier_qa/snapshot_local.raw
    ssh echosphere-live 'cd /var/www/html/EchoSphere && env/bin/python - demo' \
        < haier/qa/export_config.py > storage/tmp/haier_qa/snapshot_demo.raw
    # 2) run
    env/bin/python -B haier/qa/run_text_cases.py --snapshot storage/tmp/haier_qa/snapshot_local.raw \
        --output storage/tmp/haier_qa/text_local.json
    # demo code (= the deployed commit) instead of the working tree:
    git archive <demo-commit> | tar -x -C /tmp/demo_code
    env/bin/python -B haier/qa/run_text_cases.py --code-root /tmp/demo_code \
        --snapshot storage/tmp/haier_qa/snapshot_demo.raw --output storage/tmp/haier_qa/text_demo.json

Options: --cases FILE, --only ID[,ID], --repeat N (override per-case repeat),
--concurrency K (default 4), --transcripts FILE (human-readable dump),
--decision-timeout S (relaxed decision budget for parallel runs; a decision that
times out falls back to the authored labels, exactly as on a live call).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--code-root", type=Path)
_pre_opts, _ = _pre.parse_known_args()
_REPO = Path(__file__).resolve().parents[2]
_CODE_ROOT = (_pre_opts.code_root or _REPO).resolve()
sys.path.insert(0, str(_CODE_ROOT))

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from collections import defaultdict  # noqa: E402
from copy import deepcopy  # noqa: E402
from dataclasses import fields  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

import shared.orchestration.tool_executor as tool_module  # noqa: E402
import shared.orchestration.workflow_engine as workflow_module  # noqa: E402
from shared.bot_config import ResolvedBotConfig  # noqa: E402
from shared.orchestration.placeholders import resolve_placeholders  # noqa: E402
from shared.providers.base import ProviderConfig  # noqa: E402
from shared.providers.factory import get_llm_provider  # noqa: E402
from shared.runtime_context import ContextValue, RuntimeContext, load_runtime_context  # noqa: E402
from voice_runtime.brain import ConversationBrain  # noqa: E402
from voice_runtime.recording import TurnRecord  # noqa: E402

BOT = "bot_450a4f7b4832"
IDENTITY_RE = re.compile(r"क्या मेरी बात|क्या आप ही|बस इतना confirm कर दीजिए|Am I speaking with|Is this .* speaking")
INTRO_RE = re.compile(r"request के regarding है|request for your")


# ----------------------------------------------------------------------------
# isolation: frozen workflow, memory checkpoints, no tools
# ----------------------------------------------------------------------------
class MemoryRecorder:
    def __init__(self):
        self.session_id = "haier-qa-" + uuid.uuid4().hex[:12]
        self.events: list[dict] = []
        self.turns: list = []
        self.usage = defaultdict(int)
        self.workflow_slots: dict = {}
        self.language = "hi-IN"
        self.disposition = None
        self.call_state: dict = {}
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
        raise RuntimeError("The QA harness forbids external tool execution")


def install_isolation(definition: dict) -> None:
    if any(node.get("kind") == "api" for node in definition["nodes"]):
        raise SystemExit("workflow has API nodes; refusing to run without mocks")
    workflow_module.load_workflow_definition = lambda *a, **k: deepcopy(definition)
    tool_module.get_tool_executor = lambda: NoTools()

    async def checkpoint(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    workflow_module.WorkflowEngine._get_checkpointer = checkpoint


def new_engine():
    engine = workflow_module.WorkflowEngine()
    engine.qa_results = []
    original = engine.handle_turn_detailed

    async def capture(**kwargs):
        result = await original(**kwargs)
        engine.qa_results.append(deepcopy(result))
        return result

    engine.handle_turn_detailed = capture
    return engine


# ----------------------------------------------------------------------------
# config
# ----------------------------------------------------------------------------
def load_snapshot(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    if "===JSON-BEGIN===" in raw:
        raw = raw.split("===JSON-BEGIN===")[1].split("===JSON-END===")[0]
    return json.loads(raw)


def build_config(snapshot: dict) -> tuple[ResolvedBotConfig, dict]:
    names = {f.name for f in fields(ResolvedBotConfig)}
    data = {k: v for k, v in snapshot["config"].items() if k in names}
    config = ResolvedBotConfig(**data)
    definition = snapshot["workflow"]
    config.workflow_pins = {str(definition["id"]): int(definition["version"])}
    return config, definition


def speaker_name(config: ResolvedBotConfig) -> str:
    tts = config.tts or {}
    lm = (tts.get("language_map") or {}).get("hi-IN") or {}
    return str(lm.get("voice_name") or tts.get("voice_name") or "Ritu").split("·")[0].strip()


# ----------------------------------------------------------------------------
# one conversation
# ----------------------------------------------------------------------------
async def open_session(config, definition, payload: dict):
    """One isolated call: (brain, recorder, engine, greeting, background tasks).

    Shared by the conversation runner below and ``run_hub_cases.py``.
    """
    recorder = MemoryRecorder()
    runtime = await load_runtime_context(
        config.tenant_id, config.bot_id, phone=None, session_variables=dict(payload),
        system_values={"call_channel": "freeswitch", "bot_language": config.language},
    )
    if runtime is None:  # no schema: fall back to a plain session context
        runtime = RuntimeContext(tenant_id=config.tenant_id, bot_id=config.bot_id, source_mode="dialer",
                                 values={k: ContextValue(k, v, "session") for k, v in payload.items()})
    llm_conf = config.llm
    llm = get_llm_provider(ProviderConfig(
        provider=llm_conf["provider"], model=llm_conf["model"],
        api_key_reference=llm_conf.get("api_key_reference") or "env:OPENAI_API_KEY",
        timeout_seconds=30, extra=llm_conf.get("extra") or {"reasoning_effort": "lowest"},
    ))
    engine = new_engine()
    brain = ConversationBrain(config=deepcopy(config), llm=llm, recorder=recorder,
                              workflow_engine=engine, runtime_context=runtime)
    brain._tools = NoTools()

    async def push(frame, direction=None):
        return None

    async def notify(payload_):
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
    values = {k: v.value for k, v in runtime.values.items()}
    greeting = resolve_placeholders(config.greeting, {**values, "voice_speaker_name": speaker_name(config)})
    brain._history = [{"role": "assistant", "content": greeting}]
    brain._last_bot_reply = greeting
    recorder.add_turn(TurnRecord(role="bot", text=greeting, timestamp=0))
    return brain, recorder, engine, greeting, tasks


async def close_session(brain, tasks) -> None:
    try:
        brain._disarm_silence_timer()
        brain._discard_speculation("qa_done")
        brain._discard_extraction_ahead()
    except Exception:  # noqa: BLE001
        pass
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_conversation(config, definition, payload: dict, turns: list,
                           decision_timeout: float = 0.0) -> dict:
    brain, recorder, engine, greeting, tasks = await open_session(config, definition, payload)
    if decision_timeout > 0:
        # Judge understanding, not the provider's speed under a parallel run.
        brain._goal_engine._timeout = float(decision_timeout)
    rows = []
    error = None
    workflow_done = False
    try:
        for item in turns:
            text, post_close = (item if isinstance(item, tuple) else (item, False))
            if brain._closing:
                break
            if workflow_done and not post_close:
                # A real call ends after the closing line; only turns marked
                # post_close (a caller cutting into the closing) continue.
                break
            ev_off = len(recorder.events)
            wf_off = len(engine.qa_results)
            t0 = time.perf_counter()
            await brain._handle_turn(text)
            brain._mark_reply_heard()
            elapsed = time.perf_counter() - t0
            events = recorder.events[ev_off:]
            wf = engine.qa_results[wf_off:]
            trace = [n for r in wf for n in (r.get("trace") or [])]
            if any(r.get("done") or r.get("status") == "done" for r in wf):
                workflow_done = True
            decisions = [e for e in events if e["kind"] == "orchestration_decision"]
            rows.append({
                "say": text,
                "reply": brain._last_bot_reply,
                "outcome": recorder.workflow_slots.get("outcome"),
                "active_workflow": brain._active_workflow,
                "closed": workflow_done,
                "trace": trace,
                "decision": ({k: decisions[-1].get(k) for k in ("intent", "signal", "decision", "next_action", "needs_clarification", "reason", "latency_ms")}
                             if decisions else None),
                "event_kinds": sorted({e["kind"] for e in events}),
                "seconds": round(elapsed, 2),
            })
    except Exception as exc:  # noqa: BLE001 — a crash is a finding, keep going
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await close_session(brain, tasks)
    bot_turns = [t.text for t in recorder.turns if getattr(t, "role", "") == "bot"]
    return {
        "greeting": greeting,
        "rows": rows,
        "bot_turns": bot_turns,
        "slots": deepcopy(recorder.workflow_slots),
        "call_state": deepcopy(recorder.call_state),
        "outcome": recorder.workflow_slots.get("outcome"),
        "closed": workflow_done,
        "error": error,
        "event_counts": dict(sorted(((k, sum(1 for e in recorder.events if e["kind"] == k))
                                     for k in {e["kind"] for e in recorder.events}))),
    }


# ----------------------------------------------------------------------------
# evaluation
# ----------------------------------------------------------------------------
def evaluate(case: dict, result: dict, global_none: list[str], mode: str = "text") -> list[str]:
    exp = case.get("expect") or {}
    fails: list[str] = []
    if result.get("error"):
        fails.append(f"crash: {result['error']}")
    outcome = result.get("outcome")
    replies = [r["reply"] for r in result.get("rows", []) if r.get("reply")]
    if mode == "voice":
        replies = result.get("bot_turns", [])[1:]
    joined = "\n".join(replies)
    if exp.get("outcome_in") and outcome not in exp["outcome_in"]:
        if not (exp.get("open_ok") and outcome is None):
            fails.append(f"outcome={outcome!r} expected one of {exp['outcome_in']}")
    if exp.get("outcome_not_in") and outcome in exp["outcome_not_in"]:
        fails.append(f"forbidden outcome {outcome!r}")
    if exp.get("must_close") and not outcome:
        fails.append("call did not reach a terminal outcome")
    if exp.get("never_outcome"):
        # any outcome recorded at any turn (e.g. at the closing, before a
        # post-closing turn contested and blanked it) -- text runs only
        seen = {r.get("outcome") for r in result.get("rows", []) if r.get("outcome")}
        if mode == "voice" and result.get("checkpoint_outcome"):
            seen.add(result["checkpoint_outcome"])
        bad = seen.intersection(exp["never_outcome"])
        if bad:
            fails.append(f"outcome {sorted(bad)} was recorded during the call")
    for pat in list(exp.get("say_none") or []) + list(global_none or []):
        m = re.search(pat, joined)
        if m:
            fails.append(f"said forbidden /{pat}/: …{joined[max(0, m.start()-40):m.end()+40]}…".replace("\n", " "))
    if exp.get("say_any") and not any(re.search(p, joined, re.I) for p in exp["say_any"]):
        fails.append(f"none of {exp['say_any']} was said")
    for pat in exp.get("say_all") or []:
        if not re.search(pat, joined, re.I):
            fails.append(f"never said /{pat}/")
    if "max_identity_asks" in exp:
        n = sum(1 for r in replies if IDENTITY_RE.search(r or ""))
        if n > exp["max_identity_asks"]:
            fails.append(f"identity asked {n} times after greeting (max {exp['max_identity_asks']})")
    if exp.get("intro_has"):
        intro = next((r for r in replies if INTRO_RE.search(r or "")), "")
        if not intro:
            fails.append("intro/purpose line never spoken")
        else:
            for pat in exp["intro_has"]:
                if not re.search(pat, intro, re.I):
                    fails.append(f"intro missing /{pat}/")
    if "max_intro_repeats" in exp:
        n = sum(1 for r in replies if INTRO_RE.search(r or ""))
        if n > exp["max_intro_repeats"]:
            fails.append(f"intro spoken {n} times (max {exp['max_intro_repeats']})")
    if "max_same_question" in exp:
        qs = [re.sub(r"\s+", " ", q.strip()) for r in replies for q in re.findall(r"[^।?.!]*\?", r or "")]
        dup = {q: qs.count(q) for q in set(qs) if len(q) > 12 and qs.count(q) > exp["max_same_question"]}
        if dup:
            fails.append(f"question repeated: {list(dup.items())[:2]}")
    if "max_bot_turns" in exp and len(replies) > exp["max_bot_turns"]:
        fails.append(f"{len(replies)} bot turns (max {exp['max_bot_turns']})")
    if exp.get("nodes_none"):
        visited = {n for r in result.get("rows", []) for n in r.get("trace", [])}
        bad = visited.intersection(exp["nodes_none"])
        if bad:
            fails.append(f"visited forbidden nodes {sorted(bad)}")
    return fails


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
async def main(opts) -> int:
    snapshot = load_snapshot(opts.snapshot)
    config, definition = build_config(snapshot)
    install_isolation(definition)
    spec = json.loads(opts.cases.read_text(encoding="utf-8"))
    payloads = spec["payloads"]
    global_none = spec.get("global_say_none") or []
    only = {s.strip() for s in (opts.only or "").split(",") if s.strip()}
    jobs = []
    for case in spec["cases"]:
        if "text" not in case.get("modes", ["text"]):
            continue
        if only and case["id"] not in only:
            continue
        reps = opts.repeat or int(case.get("repeat", 1))
        turns = [(t["say"], bool(t.get("post_close"))) for t in case["turns"] if t.get("say")]
        for rep in range(1, reps + 1):
            jobs.append((case, rep, turns))
    sem = asyncio.Semaphore(max(1, opts.concurrency))
    results = []

    async def run(job):
        case, rep, turns = job
        async with sem:
            started = time.perf_counter()
            res = await run_conversation(config, definition, payloads[case["reason"]], turns,
                                         decision_timeout=opts.decision_timeout)
            res.update({"id": case["id"], "repeat": rep, "reason": case["reason"], "title": case["title"],
                        "priority": case.get("priority"), "patterns": case.get("patterns"),
                        "seconds": round(time.perf_counter() - started, 1)})
            res["failures"] = evaluate(case, res, global_none)
            res["pass"] = not res["failures"]
            print(f"{'PASS' if res['pass'] else 'FAIL'} {case['id']}#{rep} outcome={res['outcome']} "
                  f"{('; '.join(res['failures']))[:300]}", flush=True)
            results.append(res)

    await asyncio.gather(*(run(j) for j in jobs))
    results.sort(key=lambda r: (r["id"], r["repeat"]))
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "env": snapshot.get("env"), "snapshot_commit": snapshot.get("code_commit"),
        "code_root": str(_CODE_ROOT), "workflow_version": definition.get("version"),
        "workflow_md5": snapshot.get("workflow_md5"), "release": snapshot["config"].get("version"),
        "prompt_version": snapshot["config"].get("prompt_version"),
        "limitations": ["Text-level replay through the real brain + LLM; no STT/TTS/timing."],
        "results": results,
    }
    if opts.output:
        opts.output.parent.mkdir(parents=True, exist_ok=True)
        opts.output.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if opts.transcripts:
        lines = []
        for r in results:
            lines.append("=" * 100)
            lines.append(f"{r['id']}#{r['repeat']} [{r['reason']}] {'PASS' if r['pass'] else 'FAIL'} outcome={r['outcome']} closed={r['closed']}  {r['title']}")
            for f in r["failures"]:
                lines.append(f"   ! {f}")
            lines.append(f"   BOT : {r['greeting']}")
            for row in r["rows"]:
                lines.append(f"   USER: {row['say']}")
                d = row.get("decision") or {}
                tr = [n for n in row.get("trace", []) if not re.search(r"_\d+$", n)]
                lines.append(f"   BOT : {row['reply']}")
                lines.append(f"         [outcome={row['outcome']} sig={d.get('signal')} int={d.get('intent')} next={d.get('next_action')} trace={' > '.join(tr[-6:])}]")
        opts.transcripts.write_text("\n".join(lines) + "\n", encoding="utf-8")
    total = len(results)
    passed = sum(1 for r in results if r["pass"])
    print(f"\n{passed}/{total} conversations passed ({snapshot.get('env')}, wf v{definition.get('version')}, code {_CODE_ROOT})")
    return 0 if passed == total else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--code-root", type=Path)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("haier_test_cases.json"))
    parser.add_argument("--only", default="")
    parser.add_argument("--repeat", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--decision-timeout", type=float, default=0.0,
                        help="seconds; 0 = the bot's own decision budget")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--transcripts", type=Path)
    logging.basicConfig(level=logging.ERROR)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
