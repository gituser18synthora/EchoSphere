"""Build the reason x scenario pass/fail matrix from harness result files.

    env/bin/python -B haier/qa/summarize_results.py \
        --text local=storage/tmp/haier_qa/text_local.json --text demo=storage/tmp/haier_qa/text_demo.json \
        --voice local=storage/tmp/haier_qa/voice_local.json --voice demo=storage/tmp/haier_qa/voice_demo.json \
        [--text baseline=storage/tmp/haier_qa/text_base.json] --out storage/tmp/haier_qa/matrix.md

Cells read "passed/runs" per environment and mode; a case counts once per run.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

REASONS = ["bill_not_available", "customer_refused_visit", "product_not_delivered", "ecom_damaged_delivery",
           "refused_to_pay", "unit_ok_no_defect", "not_haier_product"]
SHORT = {"bill_not_available": "Bill not available", "customer_refused_visit": "Refused visit (will call back)",
         "product_not_delivered": "Product not delivered", "ecom_damaged_delivery": "E-com damaged",
         "refused_to_pay": "Refused to pay", "unit_ok_no_defect": "Unit OK / no defect",
         "not_haier_product": "Not a Haier product"}
SCENARIO = {
    "agree": "Agrees with reason", "dispute": "Disputes reason", "completed": "Disputes reason",
    "no_contact": "No technician contact", "step_budget": "No technician contact",
    "service_no": "Service not needed / changes mind", "change_answer": "Service not needed / changes mind",
    "timing": "Timing / 5-day rule", "consent": "Re-dispatch consent", "misroute": "Disputes reason",
    "identity": "Identity / wrong person / angry", "wrong_person": "Identity / wrong person / angry",
    "angry": "Identity / wrong person / angry", "unclear": "Disputes reason", "split_answer": "Audio & turn-taking",
    "busy": "Busy / agent / off-topic / questions", "agent": "Busy / agent / off-topic / questions",
    "off_topic": "Busy / agent / off-topic / questions", "question": "Busy / agent / off-topic / questions",
    "repeat_request": "Busy / agent / off-topic / questions", "one_word": "Busy / agent / off-topic / questions",
    "language": "Language switch", "pickup_hello": "Audio & turn-taking", "interrupt": "Audio & turn-taking",
    "long_answer": "Audio & turn-taking", "silence": "Audio & turn-taking", "hangup": "Audio & turn-taking",
    "noise": "Audio & turn-taking", "correction_after_close": "Audio & turn-taking", "early_answer": "Audio & turn-taking",
}
COLS = ["Agrees with reason", "Disputes reason", "No technician contact", "Service not needed / changes mind",
        "Timing / 5-day rule", "Re-dispatch consent", "Identity / wrong person / angry",
        "Busy / agent / off-topic / questions", "Language switch", "Audio & turn-taking"]


def load(spec: str):
    label, path = spec.split("=", 1)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return label, data["results"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cases", type=Path, default=Path(__file__).with_name("haier_test_cases.json"))
    p.add_argument("--text", action="append", default=[])
    p.add_argument("--voice", action="append", default=[])
    p.add_argument("--out", type=Path)
    p.add_argument("--reevaluate", action="store_true",
                   help="re-apply the CURRENT expectations of --cases to stored text results")
    a = p.parse_args()
    spec = json.loads(a.cases.read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in spec["cases"]}
    evaluate = None
    if a.reevaluate:
        import sys
        sys.argv = [sys.argv[0]]
        sys.path.insert(0, str(Path(__file__).parent))
        from run_text_cases import evaluate
    cell = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    per_case = defaultdict(dict)
    runs = [("text", s) for s in a.text] + [("voice", s) for s in a.voice]
    labels = []
    for mode, run_spec in runs:
        label, results = load(run_spec)
        key = f"{label}/{mode}"
        labels.append(key)
        for r in results:
            c = cases.get(r["id"])
            if not c:
                continue
            if evaluate is not None and mode == "text" and "rows" in r:
                r["failures"] = evaluate(c, r, spec.get("global_say_none") or [])
                r["pass"] = not r["failures"]
            col = SCENARIO.get(c.get("category"), "Busy / agent / off-topic / questions")
            ok = bool(r.get("pass"))
            cell[(c["reason"], col)][key][0] += int(ok)
            cell[(c["reason"], col)][key][1] += 1
            pc = per_case[r["id"]].setdefault(key, [0, 0, []])
            pc[0] += int(ok)
            pc[1] += 1
            pc[2].append(r.get("outcome"))
    lines = ["| Reason | Scenario | " + " | ".join(labels) + " |", "|---|---|" + "---|" * len(labels)]
    for reason in REASONS:
        for col in COLS:
            if (reason, col) not in cell:
                continue
            vals = []
            for key in labels:
                ok, n = cell[(reason, col)].get(key, [0, 0])
                vals.append("—" if n == 0 else (f"PASS {ok}/{n}" if ok == n else f"FAIL {ok}/{n}"))
            lines.append(f"| {SHORT[reason]} | {col} | " + " | ".join(vals) + " |")
    lines += ["", "| Case | Reason | Title | " + " | ".join(labels) + " |", "|---|---|---|" + "---|" * len(labels)]
    for cid in sorted(per_case):
        c = cases[cid]
        vals = []
        for key in labels:
            ok, n, outs = per_case[cid].get(key, [0, 0, []])
            vals.append("—" if n == 0 else f"{'PASS' if ok == n else 'FAIL'} {ok}/{n}")
        lines.append(f"| {cid} | {SHORT[c['reason']]} | {c['title'][:90]} | " + " | ".join(vals) + " |")
    text = "\n".join(lines) + "\n"
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
