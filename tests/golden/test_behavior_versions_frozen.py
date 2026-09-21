"""Every recorded tenant definition runs behaviour v1 until it opts in."""
import json

from shared.orchestration.behavior import resolve_behavior
from tests.golden import harness


def test_all_fixture_definitions_are_behavior_v1():
    versions = {}
    for path in sorted(harness.FIXTURES.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for wf in doc.get("workflows") or []:
            if wf.get("nodes"):
                versions[f"{path.stem}:{wf['id']}"] = resolve_behavior(wf).version
    assert versions and set(versions.values()) == {1}, versions
