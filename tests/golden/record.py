"""Record (or re-record) golden expectations: ``python -m tests.golden.record [--only <bot>]``.

Re-recording is a deliberate act: run it only after a behaviour change has
been reviewed, and commit the case diff together with the code change so the
review shows exactly which tenant turns moved.
"""
from __future__ import annotations

import json
import sys

from tests.golden import harness


def main(argv: list[str]) -> None:
    only = argv[argv.index("--only") + 1] if "--only" in argv else None
    total = 0
    for path in harness.iter_case_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
        if only and doc["bot"] != only and path.stem != only:
            continue
        fixture = harness.load_fixture(doc["bot"])
        for case in doc["cases"]:
            case["expected"] = harness.replay_case_sync(case, fixture)
            total += 1
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{path.name}: {len(doc['cases'])} cases recorded")
    print(f"recorded {total} cases")


if __name__ == "__main__":
    main(sys.argv[1:])
