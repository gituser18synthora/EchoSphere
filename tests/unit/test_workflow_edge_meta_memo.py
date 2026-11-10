"""Edge-label classification is done once per distinct token per graph build.

2026-11 (Haier bot_450a4f7b4832, demo): the first call a process handled
after a restart or a workflow save spent 2.5-3.4 s inside
``build_definition_graph`` ON the event loop — ``_edge_meta`` classified each
of ~51,750 edge-label tokens (only ~2,870 distinct) with the full regex
table. Every call in that process froze for that long: the acknowledgement
cue stopped mid-word, the room sound went silent, and the recording grew by
the same amount (workflow_timing 2464-3424 ms on turn 1 of cv_479df7320d2d,
cv_b4cb8a4eeb66, cv_ba5c1be3c53b, cv_f33c84418bfc, cv_948773909cf9,
cv_4f364a6c43bd).
"""

import time

from langgraph.checkpoint.memory import MemorySaver

from shared.orchestration import workflow_engine as wfe
from shared.orchestration.router import classify_user_signal

ANSWERS = [
    "haan", "ha", "yes", "ji haan", "theek hai", "nahi", "no", "nahin", "abhi nahi",
    "baad mein", "later", "call back", "paisa de diya", "already paid", "galat number",
    "wrong number", "agent se baat", "kya", "kaun", "complaint", "service chahiye",
    "technician aaya", "visit hua", "installation ho gaya", "kal", "parso",
]


def _edges(count: int, per_label: int) -> list[dict]:
    edges = []
    for i in range(count):
        tokens = [ANSWERS[(i * 7 + j * 3) % len(ANSWERS)] for j in range(per_label)]
        label = " / ".join(tokens) if i % 5 else "else"
        edges.append({"from": f"n{i % 40}", "to": f"n{(i + 1) % 40}", "label": label})
    return edges


def _reference(edges):
    """The classification as it was: every token occurrence on its own."""
    meta = []
    for edge in edges:
        tokens = [t for t in wfe._edge_tokens(edge.get("label", "")) if t not in wfe._ELSE_LABELS]
        signals = {s for s in (classify_user_signal(t) for t in tokens) if s}
        meta.append((edge, tokens, signals))
    return meta


def test_memoised_classification_is_identical_token_for_token():
    edges = _edges(400, 30)
    assert wfe._edge_meta(edges) == _reference(edges)
    assert wfe._edge_meta(edges, {}) == _reference(edges)
    # … and edge by edge with a shared memo, as a graph build calls it.
    memo: dict = {}
    assert [m for e in edges for m in wfe._edge_meta([e], memo)] == _reference(edges)


def test_each_distinct_token_is_classified_once_per_graph_build(monkeypatch):
    calls: list[str] = []

    def counting(token, **kwargs):
        calls.append(token)
        return classify_user_signal(token, **kwargs)

    monkeypatch.setattr(wfe, "classify_user_signal", counting)
    nodes = [{"id": "n0", "kind": "start"}] + [
        {"id": f"n{i}", "kind": "intent", "text": "?"} for i in range(1, 40)
    ]
    edges = _edges(400, 30)
    occurrences = sum(
        1 for e in edges for t in wfe._edge_tokens(e["label"]) if t not in wfe._ELSE_LABELS
    )
    wfe.build_definition_graph({"nodes": nodes, "edges": edges}, MemorySaver())
    distinct = {t for e in edges for t in wfe._edge_tokens(e["label"]) if t not in wfe._ELSE_LABELS}
    assert occurrences > 9000 and len(distinct) == len(ANSWERS)
    assert sorted(calls) == sorted(distinct)          # once each, not 9,000+ times


def test_a_large_workflow_builds_without_stalling_the_event_loop():
    """Haier-sized: ~1,000 edges × ~50 tokens. Was ~2 s; the budget here is
    generous for a loaded CI box and still an order of magnitude below it."""
    edges = _edges(1050, 50)
    started = time.perf_counter()
    meta = wfe._edge_meta(edges, {})
    elapsed = time.perf_counter() - started
    assert len(meta) == 1050
    assert elapsed < 0.6, f"_edge_meta took {elapsed * 1000:.0f} ms"


def test_the_memo_does_not_outlive_its_caller():
    """No module-level cache: signal packs registered later are honoured."""
    first: dict = {}
    wfe._edge_meta(_edges(10, 5), first)
    assert first and wfe._edge_meta.__defaults__ == (None,)
