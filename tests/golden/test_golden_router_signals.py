"""Frozen router classification over the utterance corpus (router_signals.json)."""
import json
import pathlib

import pytest

from tests.golden.build_router_corpus import classify

ROWS = json.loads((pathlib.Path(__file__).resolve().parent / "router_signals.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("row", ROWS, ids=[r["text"][:40] for r in ROWS])
def test_router_classification_is_frozen(row):
    observed = classify(row["text"])
    expected = {k: v for k, v in row.items() if k != "text"}
    assert observed == expected
