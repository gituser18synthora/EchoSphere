"""Offline transcript replay: the REAL workflow engine + deterministic router
signal classifier, with the CRM hooks mocked (success, sample responses from
the connection config) and no database — the definition comes straight from
haier/setup/03_workflow.py, so this runs BEFORE anything is applied.

What it proves: node routing, exact spoken text, slot/disposition capture,
5-day lexicon behaviour. What it cannot show: LLM wording on off-script
turns (marked "(off-script → LLM reply from the system prompt)"), the
greeting turn, platform-level DNC/transfer interception, and the
no-response ladder — those are covered by the simulate suite / live notes.

Run: env/bin/python haier/tests/replay_offline.py [name-filter] [--md out.md]
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "haier", "setup"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import haier_config as H  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402
from tests.golden.harness import patched  # noqa: E402
from shared.orchestration.router import classify_user_signal  # noqa: E402


def load_definition() -> dict:
    spec = importlib.util.spec_from_file_location(
        "haier_wf", os.path.join(ROOT, "haier", "setup", "03_workflow.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = mod.build_definition()
    problems = mod.lint(d)
    if problems:
        raise SystemExit("LINT FAIL: " + "; ".join(problems))
    return d


def harness_connections() -> list[dict]:
    return [{"name": c["name"], "response_schema": c["responseSchema"],
             "response_mapping": c["responseMapping"]} for c in H.CONNECTIONS]


def resolve(text: str, context: dict) -> str:
    out = text
    for k, v in context.items():
        out = out.replace("{{" + k + "}}", str(v))
    out = out.replace("{{voice_speaker_name}}", "Shubh")
    return out


async def replay(definition: dict, scenario: dict) -> tuple[list[dict], list[str]]:
    failures: list[str] = []
    observed: list[dict] = []
    ctx = scenario["context"]
    with patched(definition, harness_connections()) as (engine, executor):
        session = f"haier-offline-{abs(hash(scenario['name']))}"
        for index, item in enumerate(scenario["turns"]):
            text, expect = (item, {}) if isinstance(item, str) else item
            signal = classify_user_signal(text)
            result = await engine.handle_turn_detailed(
                session_id=session, tenant_id=H.TENANT, bot_id=H.BOT,
                workflow_name=definition["name"], user_text=text, language="hi-IN",
                signal=signal, context_values=ctx, reset_state=(index == 0),
                mock_tool_results=H.MOCKS,
            )
            reply = resolve(result.get("reply") or "", ctx)
            row = {"text": text, "signal": signal, "trace": list(result.get("trace") or []),
                   "awaiting": result.get("awaiting"), "reply": reply,
                   "offScript": bool(result.get("offScript")), "done": bool(result.get("done")),
                   "status": result.get("status"),
                   "slots": {k: v for k, v in sorted((result.get("slots") or {}).items())}}
            observed.append(row)
            last_hub = row["awaiting"] or (row["trace"][-1] if row["trace"] else None)
            if "node" in expect and expect["node"] != last_hub and expect["node"] not in row["trace"]:
                failures.append(f"turn {index + 1} {text[:40]!r}: expected node {expect['node']}, "
                                f"awaiting={last_hub} trace={row['trace'][-4:]}")
            if "reply" in expect and expect["reply"].lower() not in reply.lower():
                failures.append(f"turn {index + 1} {text[:40]!r}: reply lacks {expect['reply']!r} → {reply[:160]!r}")
            if "done" in expect and bool(expect["done"]) != row["done"]:
                failures.append(f"turn {index + 1} {text[:40]!r}: done={row['done']} expected {expect['done']}")
            if expect.get("offscript") and not row["offScript"]:
                failures.append(f"turn {index + 1} {text[:40]!r}: expected off-script (LLM) turn, got {reply[:120]!r}")
            if row["done"] and index < len(scenario["turns"]) - 1:
                failures.append(f"turn {index + 1} {text[:40]!r}: workflow finished EARLY "
                                f"({len(scenario['turns']) - index - 1} customer turn(s) left)")
                break
        final = observed[-1]["slots"] if observed else {}
        for key, want in (scenario.get("final") or {}).items():
            got = final.get(key)
            if str(got) != str(want) and not (isinstance(want, str) and want.lower() in str(got).lower()):
                failures.append(f"final slot {key}={got!r}, expected {want!r}")
        if not observed[-1]["done"]:
            failures.append("workflow did not finish")
        row = observed[-1]
        row["api_calls"] = [{"tool": c.get("tool"), "args": {k: v for k, v in (c.get("args") or {}).items()
                                                            if k in ("wo_number", "action", "cancel_sub_reason",
                                                                     "required_date_text", "within_5_days",
                                                                     "technician_contacted", "service_required")}}
                            for c in executor.calls]
    return observed, failures


def disposition(slots: dict, ctx: dict) -> dict:
    """The structured object the post-call summary derives (goal_policy.summaryFields)."""
    return {
        "wo_number": slots.get("wo_number") or ctx.get("wo_number"),
        "customer_reached": slots.get("customer_reached"),
        "technician_contacted": slots.get("technician_contacted", "unknown"),
        "reject_reason_confirmed": slots.get("reject_reason") or ctx.get("reject_reason"),
        "service_required": slots.get("service_required"),
        "required_date_text": slots.get("required_date_text"),
        "within_5_days": slots.get("within_5_days", "unknown"),
        "action": slots.get("action"),
        "cancel_sub_reason": slots.get("cancel_sub_reason") if slots.get("action") == "CANCEL" else None,
        "dnc_requested": slots.get("dnc_requested", "no"),
        "hook_status": slots.get("hook_status"),
        "customer_sentiment": "(post-call analyst)",
        "notes": "(post-call analyst)",
    }


def render_md(name: str, scenario: dict, observed: list[dict], failures: list[str]) -> str:
    ctx = scenario["context"]
    lines = [f"### {name}", "",
             f"Context: WO `{ctx['wo_number']}` · {ctx['product_name']} · {ctx['l3_tagging']} · "
             f"reject reason **{ctx['reject_reason']}**" + (f" · expected: {scenario['expect']}" if scenario.get("expect") else ""),
             "", "| # | Customer | Bot (exact text from the flow) | Node |", "|---|---|---|---|"]
    lines.insert(3, f"| 0 | — | {resolve(H.GREETING, ctx)} *(greeting prompt, spoken by the platform)* | greeting |")
    for i, row in enumerate(observed, 1):
        bot = row["reply"] or ("*(off-script → LLM reply from the system prompt; flow stays on this node)*"
                               if row["offScript"] else "*(no text)*")
        node = row["awaiting"] or (row["trace"][-1] if row["trace"] else "")
        if row["done"]:
            node = "end ✔"
        lines.append(f"| {i} | {row['text']} | {bot} | `{node}` |")
    lines += ["", "Disposition object:", "```json",
              json.dumps(disposition(observed[-1]["slots"], ctx), ensure_ascii=False, indent=2), "```"]
    calls = observed[-1].get("api_calls") or []
    if calls:
        lines += ["CRM hooks called (mocked): " + ", ".join(f"`{c['tool']}`" for c in calls)]
    lines += ["", ("**PASS**" if not failures else "**FAIL**\n" + "\n".join(f"- {f}" for f in failures)), ""]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    md_out = None
    if "--md" in argv:
        md_out = argv[argv.index("--md") + 1]
        argv = [a for a in argv if a not in ("--md", md_out)]
    name_filter = argv[0].lower() if argv else ""
    definition = load_definition()
    print(f"definition: {len(definition['nodes'])} nodes / {len(definition['edges'])} edges")
    passed = failed = 0
    md_sections = []
    for scenario in SCENARIOS:
        if name_filter and name_filter not in scenario["name"].lower():
            continue
        observed, failures = asyncio.run(replay(definition, scenario))
        status = "PASS" if not failures else "FAIL"
        passed += not failures; failed += bool(failures)
        print(f"\n[{status}] {scenario['name']}")
        for i, row in enumerate(observed, 1):
            node = "END" if row["done"] else (row["awaiting"] or (row["trace"][-1] if row["trace"] else "?"))
            print(f"  {i}. C: {row['text']}")
            print(f"     B: {(row['reply'] or ('(off-script → LLM)' if row['offScript'] else '(none)'))[:300]}")
            print(f"        node={node} signal={row['signal']} trace={row['trace'][-6:]}")
        print(f"  slots: {json.dumps(observed[-1]['slots'], ensure_ascii=False)[:400]}")
        for f in failures:
            print(f"  !! {f}")
        md_sections.append(render_md(scenario["name"], scenario, observed, failures))
    print(f"\n{passed} passed, {failed} failed")
    if md_out:
        with open(md_out, "w") as f:
            f.write("\n".join(md_sections))
        print(f"markdown written to {md_out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
