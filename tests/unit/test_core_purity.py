"""Architecture guard: the shared orchestration core carries no tenant,
domain or caller-language literals and never imports a tenant extension.

Tenant and workflow specifics belong in definitions, language packs, signal
packs or extensions (see docs/architecture/orchestration-layering.md). This
test fails the build when a shortcut lands in core.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
CORE = [
    "shared/orchestration/router.py",
    "shared/orchestration/workflow_engine.py",
    "shared/orchestration/behavior.py",
    "shared/orchestration/workflow_state.py",
    "shared/orchestration/extensions/__init__.py",
    "shared/orchestration/signals/__init__.py",
    "shared/orchestration/lang/__init__.py",
    "shared/orchestration/lang/base.py",
]
INDIC = re.compile(r"[ऀ-ॿ஀-௿ഀ-ൿ]")
TENANT_TOKENS = re.compile(r"\b(zepto|mdnd|mpokket|oyo|frankfinn|honasa|tally|kotak|edas)\b", re.I)
# Node ids / slot names of specific workflows must never be spelled in core.
TENANT_IDS = re.compile(r"\b(n_hub_verify|m_issue_description|n_ask_issue_desc|m_reached_location|m_called_customer|m_handover_recipient)\b")
# Words a core module may legitimately contain as an ENGLISH signal/route name.
ALLOWED = {"question", "affirm", "refusal", "clarify", "complaint", "hold", "callback",
           "agent_request", "hardship", "already_paid", "payment_intent", "wrong_person"}


def _code_strings(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def test_core_has_no_caller_language_literals():
    offenders = []
    for rel in CORE:
        for lineno, value in _code_strings(ROOT / rel):
            if INDIC.search(value):
                offenders.append(f"{rel}:{lineno}: {value[:60]!r}")
    assert not offenders, "caller-language literal in core (belongs in a language pack):\n" + "\n".join(offenders)


def test_core_has_no_tenant_vocabulary_or_ids():
    offenders = []
    for rel in CORE:
        for lineno, value in _code_strings(ROOT / rel):
            if TENANT_IDS.search(value) or (TENANT_TOKENS.search(value) and value.lower() not in ALLOWED):
                offenders.append(f"{rel}:{lineno}: {value[:60]!r}")
    assert not offenders, "tenant literal in core (belongs in a definition or extension):\n" + "\n".join(offenders)


def test_core_never_imports_a_tenant_extension():
    offenders = []
    for rel in CORE:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if re.match(r"shared\.orchestration\.extensions\.(?!manifest$)\w+", name):
                    offenders.append(f"{rel}:{node.lineno}: {name}")
                if re.match(r"shared\.orchestration\.(signals|lang)\.\w+", name) and not rel.startswith(f"shared/orchestration/{name.split('.')[2]}"):
                    offenders.append(f"{rel}:{node.lineno}: {name} (import the registry, not a pack)")
    assert not offenders, "\n".join(offenders)
