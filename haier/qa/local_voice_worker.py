"""Start a LOCAL voice worker for the QA harness, on its own port.

    env/bin/python -B haier/qa/local_voice_worker.py --port 9103
    env/bin/python -B haier/qa/local_voice_worker.py --port 9103 \
        --turn-detection-from storage/tmp/haier_qa/snapshot_demo.raw

The worker serves the code tree it is started from (run it with that tree as
the working directory) and the bots of the LOCAL database, next to the
developer's own worker on 9002. Point the voice runner at it with
``run_voice_cases.py --env local --port 9103``.

``--turn-detection-from`` takes an ``export_config.py`` snapshot and uses ITS
tenant turn-detection settings (endpointing, barge-in, held-segment,
unheard-reply and background-speech guards, silence prompts) for the calls of
that snapshot's tenant in THIS process only. Audio behaviour depends on those
settings, and local and demo differ (on 2026-11-26: demo ran the
background-speech guard and a 0.6 s endpoint, local did not), so a local run
with local settings says little about demo. Nothing is written to any
database and no other process is affected.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())


def snapshot_turn_detection(path: Path) -> tuple[str | None, dict]:
    raw = path.read_text(encoding="utf-8")
    snapshot, _ = json.JSONDecoder().raw_decode(raw[raw.index("{"):])
    config = snapshot.get("config") or {}
    settings = config.get("turn_detection")
    if not isinstance(settings, dict) or not settings:
        raise SystemExit(f"{path} carries no tenant turn-detection settings")
    return config.get("tenant_id"), settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--turn-detection-from", type=Path)
    opts = parser.parse_args()
    if opts.port in (9001, 9002, 9003, 9011):
        raise SystemExit("Pick a spare port: 9001/9002/9003/9011 belong to the developer's own services")

    import uvicorn
    import voice_runtime.app as worker

    if opts.turn_detection_from:
        tenant, settings = snapshot_turn_detection(opts.turn_detection_from)
        resolve = worker.resolve_bot_config

        async def resolve_with_snapshot_settings(*args, **kwargs):
            config = await resolve(*args, **kwargs)
            if tenant and getattr(config, "tenant_id", None) != tenant:
                return config
            return dataclasses.replace(config, turn_detection=settings)

        worker.resolve_bot_config = resolve_with_snapshot_settings
        telephony = ((settings.get("telephony") or {}).get("turn_detection") or {})
        print(f"turn detection of tenant {tenant} from {opts.turn_detection_from}: "
              f"{json.dumps(telephony, sort_keys=True)}", flush=True)
    uvicorn.run(worker.app, host="127.0.0.1", port=opts.port)


if __name__ == "__main__":
    main()
