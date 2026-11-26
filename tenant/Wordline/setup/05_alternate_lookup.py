"""Step 05 — Merchant ID and shop-name lookup when the Terminal ID is missing.

Reads the bot's current workflow and published system prompt through the
platform API, applies ``alternate_lookup.upgrade_alternate_lookup`` and
``upgrade_prompt_alternate_lookup`` (pure, idempotent, exact-match; see that
module), and saves an approved workflow version plus a published prompt
version.  Dry run by default.

Order (each step is safe on its own):

    env/bin/python tenant/Wordline/setup/01_api_connection.py --apply   # schema + mappings
    env/bin/python tenant/Wordline/setup/05_alternate_lookup.py --apply # workflow + prompt
    env/bin/python tenant/Wordline/setup/04_go_live.py release --apply \\
        --notes "Merchant ID / shop-name lookup: workflow v{wf}"        # release pins it

The merchant API must already run the 2026-11-26 code (``multiple`` answers,
``spoken_merchant_*`` keys); with older code the new questions' lookups
miss and the flow closes as before.  Before the save the current definition
is written to ``tenant/Wordline/data/workflow_backup_v<N>.json`` (rollback:
re-PUT it, or publish a release pinning the previous version).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tenant.Wordline.setup.alternate_lookup import (  # noqa: E402
    PROMPT_NOTE, upgrade_alternate_lookup, upgrade_prompt_alternate_lookup)
from tenant.Wordline.setup.common import BOT_ID, check, client, parser  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    p = parser(__doc__.split("\n")[0])
    p.add_argument("--show-diff", action="store_true", help="print the prompt diff")
    args = p.parse_args()
    c = client(args.base)

    current = check(c.get(f"/bots/{BOT_ID}/workflow"), "read workflow")
    print(f"     {current['id']} v{current['version']} {current['status']} "
          f"{len(current['nodes'])} nodes / {len(current['edges'])} edges")
    upgraded, changes = upgrade_alternate_lookup(current)
    for line in changes:
        print("  •", line)

    prompts = check(c.get(f"/bots/{BOT_ID}/prompts"), "read prompts") or []
    system = next((x for x in prompts if x.get("type") == "system"), None)
    if system is None:
        raise SystemExit("no system prompt on the bot")
    active = next((v for v in system.get("versions") or []
                   if v.get("version") == system.get("activeVersion")), None)
    text = (active or {}).get("fullPrompt") or ""
    new_text = upgrade_prompt_alternate_lookup(text)
    if new_text is None:
        print(f"     prompt {system['id']} v{system.get('activeVersion')} already current")
    else:
        print(f"     prompt {system['id']} v{system.get('activeVersion')} → new version "
              f"({len(text)} → {len(new_text)} chars)")
        if args.show_diff:
            import difflib
            for line in difflib.unified_diff(text.splitlines(), new_text.splitlines(),
                                             "published", "new", lineterm="", n=0):
                print("     " + line)

    if not args.apply:
        print("dry run — re-run with --apply")
        return
    if upgraded is not current:
        backup = DATA_DIR / f"workflow_backup_v{current['version']}.json"
        backup.write_text(json.dumps(current, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8")
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
    if new_text is not None:
        check(c.post(f"/prompts/{system['id']}/versions",
                     json={"promptMode": "full", "fullPrompt": new_text, "note": PROMPT_NOTE}),
              "new prompt version")
        check(c.patch(f"/prompts/{system['id']}", json={"state": "approved"}), "approve prompt")
        check(c.patch(f"/prompts/{system['id']}", json={"state": "published"}), "publish prompt")
    print("done")


if __name__ == "__main__":
    main()
