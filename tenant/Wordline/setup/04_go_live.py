"""Step 04 — go-live: knowledge source, regression scenarios, readiness, release.

The release checklist (``backend/routers/releases.py``) needs every readiness
item green: r1 (an indexed knowledge source usable by the bot) and r7
(regression scenarios defined and passing) were the two open ones for the
Wordline bot. This step completes them with real artefacts — no checklist
override is ever used:

  knowledge  - bot-scoped knowledge source "Wordline Merchant Helpdesk FAQ"
               from ``docs/Wordline_Merchant_Helpdesk_FAQ.md`` (the client's
               SOP facts already spoken by the flow), uploaded and indexed by
               the platform's ingestion pipeline (embedded in the API).
  scenarios  - the regression suite mirrored as platform test scenarios, one
               per scenario of ``tests/run_chat_scenarios.py``.  They are only
               marked as run after that suite has ACTUALLY passed against the
               same environment: pass ``--report <json>`` written by
               ``run_chat_scenarios.py --report``; the file must be recent,
               for the same base URL, and fully passing.
  recompute  - POST /bots/{id}/readiness/recompute.
  release    - next patch version (v0.1.4 → v0.1.5): create → review →
               approved → published.  A 422 (checklist incomplete) is reported
               and the release is left in review — never overridden.

Dry run by default; ``--apply`` writes.  Stages: knowledge, scenarios,
recompute, release, all (default all).

    env/bin/python tenant/Wordline/setup/04_go_live.py --report /tmp/wl_chat_report.json
    env/bin/python tenant/Wordline/setup/04_go_live.py --base https://…/api/v1 --report … --apply
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tenant.Wordline.setup.common import BOT_ID, TENANT_ID, check, client, parser  # noqa: E402

KB_NAME = "Wordline Merchant Helpdesk FAQ"
DOC = Path(__file__).resolve().parent.parent / "docs" / "Wordline_Merchant_Helpdesk_FAQ.md"
SUITE = "Merchant lookup regression"
REPORT_MAX_AGE_S = 6 * 3600


def stage_knowledge(c, apply: bool) -> None:
    data = check(c.get("/knowledge", params={"pageSize": 100, "tenantId": TENANT_ID}), "list knowledge")
    rows = data if isinstance(data, list) else (data or {}).get("items", [])
    kb = next((k for k in rows if k.get("name") == KB_NAME and k.get("botId") in (BOT_ID, None)
               and not k.get("isDeleted")), None)
    if kb and kb.get("status") == "indexed":
        print(f"     knowledge source {kb['id']} already indexed")
        return
    print(f"     would {'create' if kb is None else 'reuse ' + kb['id']} bot-scoped KB {KB_NAME!r} "
          f"and upload {DOC.name} ({DOC.stat().st_size} bytes)")
    if not apply:
        return
    if kb is None:
        kb = check(c.post("/knowledge", json={
            "scope": "bot", "botId": BOT_ID, "tenantId": TENANT_ID, "type": "document", "name": KB_NAME,
            "detail": ("Paper roll (SMS, IVR, raised by the helpdesk) and terminal battery "
                       "FAQ — the SOP facts the guided flow speaks."),
        }), f"create KB {KB_NAME!r}")
    with DOC.open("rb") as f:
        doc = check(c.post(f"/knowledge/{kb['id']}/documents",
                           files={"file": (DOC.name, f, "text/markdown")}),
                    f"upload {DOC.name}")
    doc_id = doc["documentId"]
    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(3)
        st = c.get(f"/knowledge/documents/{doc_id}/status").json().get("data", {})
        if st.get("status") == "ready":
            print(f"ok   document indexed ({st.get('chunkCount')} chunks)")
            break
        if st.get("status") == "failed":
            sys.exit(f"FAIL ingestion: {st.get('failureReason')}")
    else:
        sys.exit("FAIL ingestion timed out (is the ingestion worker running / embedded?)")
    kb = check(c.get(f"/knowledge/{kb['id']}"), "kb detail")
    if kb.get("status") != "indexed":
        sys.exit(f"FAIL KB status {kb.get('status')!r}, expected 'indexed'")


def _load_report(path: Path | None, base: str) -> dict:
    if path is None:
        sys.exit("scenarios stage needs --report <json> from run_chat_scenarios.py --report")
    report = json.loads(path.read_text(encoding="utf-8"))
    age = time.time() - float(report.get("finished_at", 0))
    problems = []
    if report.get("base", "").rstrip("/") != base.rstrip("/"):
        problems.append(f"report base {report.get('base')!r} != {base!r}")
    if age > REPORT_MAX_AGE_S:
        problems.append(f"report is {age/3600:.1f} h old")
    failed = [n for n, r in report.get("scenarios", {}).items() if not r.get("passed")]
    if failed or not report.get("scenarios"):
        problems.append(f"failing/empty: {failed}")
    if problems:
        sys.exit("refusing to mark scenarios as passing: " + "; ".join(problems))
    return report


def stage_scenarios(c, apply: bool, report_path: Path | None, base: str) -> None:
    report = _load_report(report_path, base)
    existing = {s["name"]: s for s in (check(c.get(f"/bots/{BOT_ID}/scenarios"), "list scenarios") or [])}
    for name, result in report["scenarios"].items():
        if name in existing:
            continue
        print(f"     would create scenario {name!r} ({result['turns']} turns)")
        if apply:
            check(c.post(f"/bots/{BOT_ID}/scenarios",
                         json={"name": name, "suite": SUITE, "steps": int(result["turns"])}),
                  f"scenario {name}")
    print(f"     chat report: {len(report['scenarios'])} scenarios passed against {report['base']} "
          f"at {time.strftime('%Y-%m-%d %H:%M', time.localtime(report['finished_at']))}")
    if apply:
        res = check(c.post(f"/bots/{BOT_ID}/scenarios/run"), "run regression suite")
        print(f"     suite: {res.get('passed')}/{res.get('total')} passed")


def stage_recompute(c, apply: bool) -> None:
    if apply:
        data = check(c.post(f"/bots/{BOT_ID}/readiness/recompute"), "recompute readiness")
        items = data.get("readiness") if isinstance(data, dict) else data
        print("     readiness:", [(i.get("label"), i.get("done")) for i in (items or [])])
    else:
        bot = check(c.get(f"/bots/{BOT_ID}"), "read bot")
        print("     readiness now:", [(i.get("label"), i.get("done")) for i in (bot.get("readiness") or [])])


def _next_version(c) -> str:
    rels = check(c.get(f"/bots/{BOT_ID}/releases"), "list releases") or []
    versions = [r["version"] for r in rels]
    best = (0, 0, 0)
    for v in versions:
        parts = v.lstrip("v").split(".")
        try:
            tup = tuple(int(x) for x in parts)
        except ValueError:
            continue
        best = max(best, tup + (0,) * (3 - len(tup)))
    if best == (0, 0, 0):
        bot = check(c.get(f"/bots/{BOT_ID}"), "read bot")
        parts = str(bot.get("version") or "v0.1.0").lstrip("v").split(".")
        best = tuple(int(x) for x in parts) if all(x.isdigit() for x in parts) else (0, 1, 0)
    return f"v{best[0]}.{best[1]}.{best[2] + 1}"


def stage_release(c, apply: bool) -> None:
    wf = check(c.get(f"/bots/{BOT_ID}/workflow"), "read workflow")
    rels = check(c.get(f"/bots/{BOT_ID}/releases"), "list releases") or []
    open_rel = next((r for r in rels if r.get("stage") in ("draft", "review", "approved")), None)
    version = open_rel["version"] if open_rel else _next_version(c)
    print(f"     would {'continue' if open_rel else 'create'} release {version} "
          f"({open_rel['stage'] if open_rel else 'new'}) pinning workflow v{wf['version']} ({wf['status']})")
    if not apply:
        return
    rel = open_rel or check(c.post(f"/bots/{BOT_ID}/releases", json={
        "version": version,
        "notes": (f"Merchant lookup API: workflow v{wf['version']} (Terminal ID lookup, pinned 7-8 "
                  "digit capture), connection 'Wordline Merchant Lookup', runtime context without "
                  "merchant fixtures, helpdesk FAQ knowledge source, regression scenarios."),
    }), f"create release {version}")
    blocked = [i["label"] for i in (rel.get("checklist") or []) if not i.get("ok")]
    print("     checklist blocked items:", blocked or "none")
    order = ["draft", "review", "approved", "published"]
    for stage in order[order.index(rel.get("stage", "draft")) + 1:]:
        r = c.patch(f"/releases/{rel['id']}", json={"stage": stage})
        if r.status_code >= 300:
            sys.exit(f"FAIL release {version} -> {stage}: {r.status_code} {r.text[:400]} "
                     "(left as is; no checklist override is used by this script)")
        rel = r.json().get("data") or rel
        print(f"ok   release {version} -> {stage}")
    print("     pinned workflows:", rel.get("pinnedWorkflows"))


def main() -> None:
    p = parser(__doc__.split("\n")[0])
    p.add_argument("stage", nargs="?", default="all",
                   choices=["knowledge", "scenarios", "recompute", "release", "all"])
    p.add_argument("--report", type=Path, default=None, help="run_chat_scenarios.py --report output")
    args = p.parse_args()
    c = client(args.base)
    stages = {
        "knowledge": lambda: stage_knowledge(c, args.apply),
        "scenarios": lambda: stage_scenarios(c, args.apply, args.report, args.base),
        "recompute": lambda: stage_recompute(c, args.apply),
        "release": lambda: stage_release(c, args.apply),
    }
    for name, fn in stages.items():
        if args.stage in ("all", name):
            print(f"=== {name} ===")
            fn()
    if not args.apply:
        print("dry run — re-run with --apply")


if __name__ == "__main__":
    main()
