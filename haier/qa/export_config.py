"""READ-ONLY export of the Haier bot's EFFECTIVE runtime configuration.

Writes nothing anywhere. Resolves the published bot exactly as the voice
runtime does (``_load_config_sync(require_published=True)``) and the pinned
workflow revision (``load_workflow_definition``), and prints one JSON
snapshot between ===JSON-BEGIN=== / ===JSON-END=== markers.

Local:
    env/bin/python -B haier/qa/export_config.py local > /tmp/x.out
Demo (script travels on stdin, nothing is copied to the box):
    ssh echosphere-live 'cd /var/www/html/EchoSphere && PYTHONDONTWRITEBYTECODE=1 \
        timeout 100 env/bin/python - demo' < haier/qa/export_config.py > /tmp/x.out

Then ``haier/qa/run_text_cases.py --snapshot`` consumes the extracted JSON
(``haier/qa/run_text_cases.py`` accepts the raw marker output too).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone

sys.path.insert(0, os.getcwd())

from shared.bot_config import _load_config_sync  # noqa: E402
from shared.orchestration import workflow_engine as we  # noqa: E402

BOT = "bot_450a4f7b4832"
WORKFLOW_ID = "wf_1c3e59362c51"


def _md5(obj) -> str:
    return hashlib.md5(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    cfg = _load_config_sync(BOT, require_published=True)
    data = asdict(cfg)
    definition = we.load_workflow_definition(cfg.tenant_id, BOT, WORKFLOW_ID, cfg.workflow_pins)
    if definition is None:
        raise SystemExit("workflow definition not found")
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                                timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        commit = ""
    snapshot = {
        "env": label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "code_commit": commit,
        "bot_id": BOT,
        "config": data,
        "workflow": definition,
        "workflow_md5": _md5({"nodes": definition.get("nodes"), "edges": definition.get("edges")}),
        "system_prompt_md5": hashlib.md5((cfg.system_prompt or "").encode()).hexdigest(),
    }
    print("===JSON-BEGIN===")
    print(json.dumps(snapshot, ensure_ascii=False, default=str))
    print("===JSON-END===")


if __name__ == "__main__":
    main()
