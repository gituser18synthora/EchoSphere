"""Step 02 — wire the merchant lookup into the bot workflow.

Reads the bot's current workflow through the platform API, applies
``workflow_upgrade.upgrade_workflow`` (pure, idempotent, see that module for
the exact changes), and saves it as a new approved version.  Dry run prints
the change list and node/edge counts; ``--apply`` saves.

    env/bin/python tenant/Wordline/setup/02_workflow.py            # dry run
    env/bin/python tenant/Wordline/setup/02_workflow.py --apply    # save v+1

Before the save a snapshot of the current definition is written to
``tenant/Wordline/data/workflow_backup_v<N>.json`` for rollback (re-PUT it).
Run step 01 first: the api nodes reference the connection by name.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tenant.Wordline.setup.common import BOT_ID, check, client, parser  # noqa: E402
from tenant.Wordline.setup.workflow_upgrade import (  # noqa: E402
    CONNECTION_NAME, upgrade_workflow)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    args = parser(__doc__.split("\n")[0]).parse_args()
    c = client(args.base)
    current = check(c.get(f"/bots/{BOT_ID}/workflow"), "read workflow")
    print(f"     {current['id']} v{current['version']} {current['status']} "
          f"{len(current['nodes'])} nodes / {len(current['edges'])} edges "
          f"name={current['name']!r}")
    upgraded, changes = upgrade_workflow(current)
    for line in changes:
        print("  •", line)
    if upgraded is current:
        print("nothing to do")
        return
    print(f"     → {len(upgraded['nodes'])} nodes / {len(upgraded['edges'])} edges; "
          f"api nodes call connection {CONNECTION_NAME!r}")
    if not args.apply:
        print("dry run — re-run with --apply to save an approved new version")
        return
    backup = DATA_DIR / f"workflow_backup_v{current['version']}.json"
    backup.write_text(json.dumps(current, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"     backup: {backup}")
    saved = check(c.put(f"/bots/{BOT_ID}/workflow", json={
        "name": current["name"], "nodes": upgraded["nodes"], "edges": upgraded["edges"],
        "status": "approved",
    }), "save workflow")
    issues = saved.get("issues") or []
    print(f"saved {saved['id']} v{saved['version']} {saved['status']} "
          f"{len(saved['nodes'])} nodes / {len(saved['edges'])} edges; issues={len(issues)}")
    for issue in issues[:10]:
        print("  issue:", json.dumps(issue, ensure_ascii=False)[:200])


if __name__ == "__main__":
    main()
