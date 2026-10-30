"""Frozen tenant behaviour: every recorded caller turn must replay identically.

Any diff here means a shared-orchestration change altered a tenant's
workflow/router outcome. Either the change is a deliberate, reviewed
behaviour change (re-record with ``python -m tests.golden.record`` and commit
the case diff), or it is a regression.
"""
import json

import pytest

from tests.golden import harness


def _params():
    for path in harness.iter_case_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
        for case in doc["cases"]:
            yield pytest.param(doc["bot"], case, id=f"{path.stem}::{case['name'][:70]}")


@pytest.mark.parametrize("bot, case", list(_params()))
def test_golden_replay(bot, case):
    assert case.get("expected"), "case has no recording — run python -m tests.golden.record"
    observed = harness.replay_case_sync(case, harness.load_fixture(bot))
    problems = harness.diff_turns(case["expected"], observed)
    assert not problems, "\n".join(problems)
