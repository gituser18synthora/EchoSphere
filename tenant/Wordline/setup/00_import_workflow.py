"""Step 00 (optional) — import a saved workflow definition as a new version.

Used to align LOCAL with the definition another environment runs (e.g. the
DEMO v12 snapshot in ``data/``) before applying the upgrade steps, and as the
rollback tool (re-import ``data/workflow_v10_snapshot.json`` or a
``workflow_backup_v<N>.json``).  Dry run by default.

    env/bin/python tenant/Wordline/setup/00_import_workflow.py --file tenant/Wordline/data/workflow_demo_v12_snapshot.json
    … --apply            # save as the next approved version on the target
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tenant.Wordline.setup.common import BOT_ID, check, client, parser  # noqa: E402


def main() -> None:
    p = parser(__doc__.split("\n")[0])
    p.add_argument("--file", required=True, type=Path)
    args = p.parse_args()
    definition = json.loads(args.file.read_text(encoding="utf-8"))
    nodes, edges = definition["nodes"], definition["edges"]
    print(f"file: {args.file} ({definition.get('version')}, {len(nodes)} nodes / {len(edges)} edges)")
    c = client(args.base)
    current = check(c.get(f"/bots/{BOT_ID}/workflow"), "read workflow")
    print(f"     target: {current['id']} v{current['version']} {current['status']} "
          f"{len(current['nodes'])} nodes / {len(current['edges'])} edges")
    if not args.apply:
        print("dry run — re-run with --apply")
        return
    saved = check(c.put(f"/bots/{BOT_ID}/workflow", json={
        "name": definition.get("name") or current["name"], "nodes": nodes, "edges": edges,
        "status": "approved"}), "save workflow")
    print(f"saved v{saved['version']} {saved['status']} {len(saved['nodes'])} nodes / "
          f"{len(saved['edges'])} edges; issues={len(saved.get('issues') or [])}")


if __name__ == "__main__":
    main()
