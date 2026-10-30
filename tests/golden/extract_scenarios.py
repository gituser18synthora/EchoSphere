"""Dev tool: lift the caller-turn scripts out of the tenant scenario runners.

The runners (``<tenant>/tests/run_*.py``) talk to the local API; this module
executes only their module-level assignments with a stub ``httpx`` and a
forgiving namespace, so the SCENARIOS / SUITES / E2E literals can be read
without any service running. Output: ``tests/golden/cases/<name>.json``
case files consumed by ``tests/golden/harness.py`` (``python -m
tests.golden.extract_scenarios`` regenerates them; the recorded expectations
are added by ``python -m tests.golden.record``).
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
CASES = pathlib.Path(__file__).resolve().parent / "cases"


class _Ph:
    """Placeholder standing in for anything the runner fetched over HTTP."""

    def __init__(self, name="?"):
        self.name = name

    def __getattr__(self, item):
        return _Ph(f"{self.name}.{item}")

    def __call__(self, *a, **k):
        return _Ph(f"{self.name}()")

    def __getitem__(self, item):
        return _Ph(f"{self.name}[{item!r}]")

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return True

    def __repr__(self):
        return f"<ph {self.name}>"

    def get(self, *a, **k):
        return _Ph(f"{self.name}.get")

    def json(self):
        return {"data": []}


class _NS(dict):
    def __missing__(self, key):
        return _Ph(key)


def _stub_httpx():
    mod = types.ModuleType("httpx")
    mod.Client = lambda *a, **k: _Ph("httpx.Client")
    mod.get = mod.post = mod.put = lambda *a, **k: _Ph("httpx.call")
    return mod


def load_runner(path: pathlib.Path) -> dict:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = _stub_httpx()
    ns = _NS(__name__="golden_extract", __file__=str(path))
    try:
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.FunctionDef, ast.Import, ast.ImportFrom,
                                 ast.AnnAssign, ast.AugAssign)):
                code = compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec")
                try:
                    exec(code, ns)
                except Exception:  # noqa: BLE001 — network-shaped statements
                    pass
    finally:
        if saved is not None:
            sys.modules["httpx"] = saved
        else:
            sys.modules.pop("httpx", None)
    return ns


def _turns(items) -> list[dict]:
    out = []
    for item in items:
        if not isinstance(item, (tuple, list)) or not item:
            continue
        text = item[0]
        if not isinstance(text, str):
            continue
        mocks, options = None, None
        rest = list(item[1:])
        # (text, expect) | (text, mocks, expect) | (text, mocks, expect, options)
        if len(rest) >= 2:
            mocks = rest[0]
            if len(rest) >= 3:
                options = rest[2]
        turn = {"text": text}
        if isinstance(mocks, dict) and mocks:
            turn["mocks"] = {k: (v if isinstance(v, dict) else None) for k, v in mocks.items()}
        elif isinstance(mocks, _Ph):
            turn["mocks"] = {"__runner__": mocks.name}
        if isinstance(options, dict):
            turn["options"] = options
        out.append(turn)
    return out


RUNNERS = [
    # (runner path, how to iterate scenarios → (case name, bot key, turns))
    ("zepto/tests/run_single_bot_scenarios.py", "suites"),
    ("zepto/tests/run_ob_deduction_scenarios.py", "scenarios"),
    ("frankfinn/tests/run_chat_scenarios.py", "scenarios"),
    ("honasa/tests/run_chat_scenarios.py", "scenarios"),
    ("oyo/tests/run_chat_scenarios.py", "scenarios_bot"),
]

BOT_KEYS = {
    "zepto/tests/run_single_bot_scenarios.py": {
        "MDND": "bot_59a84478f155", "ONBOARDING": "bot_faf32177a32e",
        "UNIFORM": None, "RTO": None,
    },
    "zepto/tests/run_ob_deduction_scenarios.py": "bot_4b8d6fe95cb1",
    "frankfinn/tests/run_chat_scenarios.py": "bot_059e49443c76",
    "honasa/tests/run_chat_scenarios.py": "bot_71194477c0eb",
}
OYO_BOTS = {"bot_e8cf0b05bb79": "bot_e8cf0b05bb79", "BOT1": "bot_e8cf0b05bb79",
            "BOT2": "bot_99177674902a", "BOT3": "bot_78b6aa83d94a"}


def extract() -> list[dict]:
    cases: list[dict] = []
    for rel, mode in RUNNERS:
        ns = load_runner(ROOT / rel)
        if mode == "suites":
            for key, (bot_ref, scenarios) in (ns.get("SUITES") or {}).items():
                bot = BOT_KEYS[rel].get(key)
                if not bot:
                    continue
                for name, turns in scenarios:
                    cases.append({"runner": rel, "name": name, "bot": bot, "turns": _turns(turns)})
        elif mode == "scenarios":
            for entry in ns.get("SCENARIOS") or []:
                if len(entry) < 2:
                    continue
                name, turns = entry[0], entry[1]
                cases.append({"runner": rel, "name": name, "bot": BOT_KEYS[rel], "turns": _turns(turns)})
        elif mode == "scenarios_bot":
            for entry in ns.get("SCENARIOS") or []:
                if len(entry) < 3:
                    continue
                name, bot_ref, turns = entry[0], entry[1], entry[2]
                bot = bot_ref if isinstance(bot_ref, str) and bot_ref.startswith("bot_") else None
                if isinstance(bot_ref, _Ph):
                    bot = OYO_BOTS.get(bot_ref.name.split("[")[-1].strip("']\""), None)
                if not bot:
                    continue
                cases.append({"runner": rel, "name": name, "bot": bot, "turns": _turns(turns)})
    return [c for c in cases if c["turns"]]


def main() -> None:
    CASES.mkdir(exist_ok=True)
    cases = extract()
    by_bot: dict[str, list] = {}
    for case in cases:
        by_bot.setdefault(case["bot"], []).append(case)
    for bot, items in by_bot.items():
        path = CASES / f"{bot}.json"
        existing = json.loads(path.read_text()) if path.exists() else {}
        recorded = {c["name"]: c.get("expected") for c in existing.get("cases", [])}
        for c in items:
            if recorded.get(c["name"]) is not None:
                c["expected"] = recorded[c["name"]]
        path.write_text(json.dumps({"bot": bot, "cases": items}, ensure_ascii=False, indent=1))
        print(bot, len(items), "cases", sum(len(c["turns"]) for c in items), "turns")


if __name__ == "__main__":
    main()
